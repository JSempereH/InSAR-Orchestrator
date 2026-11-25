"""A repeatable validation of the processing chain against independent data.

What a result is worth is only known by comparing it with something else: EGMS points, GNSS
stations, the areas that should not move. Until now that was done by hand with scripts that were
not kept, on results made by different versions of the chain, so nothing said whether a change
made the library better or worse. This runs the same comparisons on a fixed list of cases
(`benchmark/cases.json`), writes the numbers in one place, and checks them against a saved baseline,
so that a change that moves a result shows up before it is trusted.

    python -m app.benchmark                       # run every case, print the table
    python -m app.benchmark --only lorca-desc-2023
    python -m app.benchmark --baseline benchmark/baseline.json          # exit 1 on a regression
    python -m app.benchmark --update-baseline benchmark/baseline.json   # accept today's numbers

A case is a finished MintPy folder plus whatever independent data exists for it. What each case
measures:

* against EGMS: the spread of the difference in velocity (RMS and robust MAD), the correlation, the
  slope of one on the other, and the mean difference in bands of velocity. The band around zero is
  the noise of the chain over ground that does not move; the fast bands say what it loses.
* against GNSS: the velocity at each station that recorded over the period, projected on the line of sight.
* the quality checks of the run, the precision it states against distance, and what the chain did
  (which corrections ran, how many interferograms were used).

It reads the data the way the app does, so a number here is the number the app shows. It needs
the real folders on this machine; the logic that decides pass or fail is tested without them.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent   # the paths of a manifest are relative to this folder

STABLE_BAND = (-3.0, 3.0)      # mm/year: what EGMS says does not move
_POINTS: dict = {}             # EGMS points already read in this run

# How far a number may move before it counts as a change. In the unit of the number.
TOLERANCES = {
    "rmse_mm_yr": 0.30, "mad_mm_yr": 0.25, "pearson_r": 0.02, "slope_b_on_a": 0.05,
    "stable_rmse_mm_yr": 0.30, "band_bias_mm_yr": 0.60, "gnss_insar_mm_yr": 3.0,
    "sigma_10km_mm_yr": 0.30,
}


class BenchmarkError(ValueError):
    pass


def load_cases(path) -> list[dict]:
    """The cases of a manifest, checked."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"The manifest {path} cannot be read: {exc}") from exc
    cases = data.get("cases") if isinstance(data, dict) else None
    if not cases:
        raise BenchmarkError(f"The manifest {path} has no cases.")
    seen = set()
    for case in cases:
        for key in ("id", "work_dir"):
            if not case.get(key):
                raise BenchmarkError(f"A case of {path} has no '{key}'.")
        if case["id"] in seen:
            raise BenchmarkError(f"The case id '{case['id']}' is used twice.")
        seen.add(case["id"])
        egms = case.get("egms")
        if egms and not (egms.get("destination") and egms.get("files")):
            raise BenchmarkError(f"The EGMS entry of '{case['id']}' needs 'destination' and 'files'.")
    return cases


def _resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BACKEND_DIR / candidate


# ── what is measured ─────────────────────────────────────────────────────────

def egms_metrics(comparison: dict) -> dict:
    """The numbers kept from `validation.compare_with_points`."""
    velocity = comparison["velocity_rereferenced"]
    bands = comparison.get("by_velocity", [])
    stable = [b for b in bands if b["from"] is not None and b["from"] >= STABLE_BAND[0] and b["to"] is not None and b["to"] <= STABLE_BAND[1]]
    return {
        "n": int(velocity["n"]), "rmse_mm_yr": round(velocity["rmse_mm_yr"], 3), "mad_mm_yr": round(velocity["mad_mm_yr"], 3),
        "pearson_r": round(velocity["pearson_r"], 4), "slope_b_on_a": round(velocity["slope_b_on_a"], 4),
        "stable_rmse_mm_yr": round(stable[0]["rmse_mm_yr"], 3) if stable else None,
        "series_rmse_median_mm": round(comparison["timeseries_rmse_mm"]["median"], 3),
        "bands": [{"from": b["from"], "to": b["to"], "n": int(b["n"]), "bias_mm_yr": round(b["bias_mm_yr"], 2),
                   "rmse_mm_yr": round(b["rmse_mm_yr"], 2)} for b in bands],
    }


def gnss_metrics(result: dict) -> dict:
    return {"stations": [
        {"name": s["name"], "gnss_mm_yr": round(s["gnss_mm_yr"], 2), "insar_mm_yr": round(s["insar_mm_yr"], 2),
         "dates": int(s["dates"]), "coverage": round(s.get("coverage", 1.0), 2)}
        for s in result.get("stations", [])]}


def qc_metrics(qc: dict) -> dict:
    return {"status": qc["status"],
            "checks": {c["id"]: {"status": c["status"], "value": c["value"]} for c in qc["checks"]}}


def process_metrics(work_dir: Path) -> dict:
    """What the chain did to this result, from the accounts it leaves in the folder."""
    import h5py

    from insar_core.analysis import fading_correction, unwrap_correction
    from insar_core.pipeline import mask_refinement

    out: dict = {}
    try:
        with h5py.File(work_dir / "inputs" / "ifgramStack.h5", "r") as handle:
            pairs = [(a.decode(), b.decode()) for a, b in handle["date"][:]]
            used = handle["dropIfgram"][:].astype(bool)
        spans = [(datetime.strptime(b, "%Y%m%d") - datetime.strptime(a, "%Y%m%d")).days for a, b in pairs]
        out["interferograms"] = {"in_stack": len(pairs), "used": int(used.sum()),
                                 "longest_used_days": max((d for d, u in zip(spans, used, strict=False) if u), default=None)}
    except (OSError, KeyError):
        out["interferograms"] = None
    out["unwrap_correction"] = bool(unwrap_correction.read_summary(work_dir))
    out["fading_correction"] = bool(fading_correction.read_summary(work_dir))
    out["mask_refinement"] = bool(mask_refinement.read_summary(work_dir))
    return out


def run_case(case: dict, with_gnss: bool = True) -> dict:
    """Every measure of one case. A measure that cannot be made is reported with its reason, not left out."""
    from insar_core.analysis import spatial_uncertainty, validation
    from insar_core.pipeline import mintpy_qc

    from app.services import validation_service

    work_dir = _resolve(case["work_dir"])
    result: dict = {"id": case["id"], "setting": case.get("setting", ""), "work_dir": str(case["work_dir"]),
                    "measured_at": datetime.now(timezone.utc).isoformat()}
    try:
        mintpy = validation_service._read_mintpy(work_dir)
    except (OSError, FileNotFoundError, KeyError) as exc:
        return {**result, "error": f"The MintPy result cannot be read: {exc}"}
    result["direction"], result["track"], result["dates"] = mintpy["direction"], mintpy["track"], len(mintpy["dates"])
    result["process"] = process_metrics(work_dir)
    result["qc"] = qc_metrics(mintpy_qc.compute_qc(work_dir))
    try:
        sigma = spatial_uncertainty.estimate(work_dir)
        at = {round(m["distance_km"]): m for m in sigma["at"]}
        result["precision"] = {"sigma_10km_mm_yr": at[10]["sigma_mm_yr"] if 10 in at else None,
                               "ratio_to_stated": round(sigma["ratio_to_stated"], 2)}
    except (spatial_uncertainty.UncertaintyError, OSError, KeyError) as exc:
        result["precision"] = {"error": str(exc)}

    egms = case.get("egms")
    if egms:
        try:
            destination = _resolve(egms["destination"])
            bbox = validation_service._grid_bbox(mintpy["geo"])
            # Reading the EGMS tiles is most of the time of a run; cases over the same area and dates share it.
            key = (str(destination), tuple(egms["files"]), tuple(round(v, 4) for v in bbox), tuple(mintpy["dates"]))
            if key not in _POINTS:
                _POINTS[key] = validation_service.read_egms_points(destination, list(egms["files"]), bbox, set(mintpy["dates"]))
            points = _POINTS[key]
            comparison = validation.compare_with_points(
                geo=mintpy["geo"], mintpy_dates=mintpy["dates"], mintpy_series_mm=mintpy["series_mm"], valid=mintpy["valid"],
                point_lat=points["lat"], point_lon=points["lon"], point_dates=points["dates"],
                point_series_mm=points["series_mm"], mintpy_velocity_std_mm=mintpy["velocity_std_mm"])
            result["egms"] = egms_metrics(comparison)
        except (ValueError, OSError) as exc:
            result["egms"] = {"error": str(exc)}
    if case.get("gnss") and with_gnss:
        result["gnss"] = _gnss(work_dir, case["id"])
    return result


def _gnss(work_dir: Path, label: str) -> dict:
    """The GNSS comparison, run on a link to the folder so that nothing is written into it."""
    from app.services import validation_service

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "mintpy").symlink_to(work_dir.resolve(), target_is_directory=True)
        key = f"benchmark-{label}:gnss"
        validation_service._runs[key] = validation_service.ValidationState(project_id=f"benchmark-{label}", kind="gnss")
        validation_service._run_gnss(f"benchmark-{label}", Path(tmp))
        state = validation_service._runs.pop(key)
    if state.status != "SUCCEEDED" or not state.result:
        return {"error": state.error or "the comparison did not finish"}
    return gnss_metrics(state.result)


# ── baseline ─────────────────────────────────────────────────────────────────

def _flat(result: dict) -> dict[str, float]:
    """The numbers of one case's result that are compared with the baseline, by name."""
    flat: dict[str, float] = {}
    egms = result.get("egms") or {}
    for key in ("rmse_mm_yr", "mad_mm_yr", "pearson_r", "slope_b_on_a", "stable_rmse_mm_yr"):
        if egms.get(key) is not None:
            flat[f"egms.{key}"] = egms[key]
    for band in egms.get("bands", []):
        flat[f"egms.band[{band['from']},{band['to']}].band_bias_mm_yr"] = band["bias_mm_yr"]
    for station in (result.get("gnss") or {}).get("stations", []):
        flat[f"gnss.{station['name']}.gnss_insar_mm_yr"] = station["insar_mm_yr"]
    sigma = (result.get("precision") or {}).get("sigma_10km_mm_yr")
    if sigma is not None:
        flat["precision.sigma_10km_mm_yr"] = sigma
    return flat


def _tolerance(name: str) -> float:
    for key, value in TOLERANCES.items():
        if name.endswith(key):
            return value
    return float("inf")


def compare_with_baseline(results: list[dict], baseline: dict) -> list[dict]:
    """What moved more than its tolerance since the baseline, and what a case lost or gained.

    Returns a list of {case, what, was, now, change, tolerance}. Quality-check statuses must be equal."""
    moved = []
    for result in results:
        reference = baseline.get(result["id"])
        if reference is None:
            moved.append({"case": result["id"], "what": "case", "was": None, "now": "present", "note": "not in the baseline"})
            continue
        if result.get("error"):
            moved.append({"case": result["id"], "what": "case", "was": "ok", "now": result["error"], "note": "could not be measured"})
            continue
        now, was = _flat(result), reference.get("numbers", {})
        for name in sorted(set(now) | set(was)):
            if name not in now or name not in was:
                moved.append({"case": result["id"], "what": name, "was": was.get(name), "now": now.get(name),
                              "note": "measured now and not before" if name in now else "measured before and not now"})
                continue
            change, limit = now[name] - was[name], _tolerance(name)
            if abs(change) > limit:
                moved.append({"case": result["id"], "what": name, "was": was[name], "now": now[name],
                              "change": round(change, 3), "tolerance": limit})
        for check, state in (result.get("qc", {}).get("checks") or {}).items():
            before = (reference.get("qc") or {}).get(check)
            if before is not None and before != state["status"]:
                moved.append({"case": result["id"], "what": f"qc.{check}", "was": before, "now": state["status"],
                              "note": "quality check changed status"})
    return moved


def to_baseline(results: list[dict]) -> dict:
    """What is kept of a run to compare later runs with."""
    return {r["id"]: {"numbers": _flat(r), "qc": {c: s["status"] for c, s in (r.get("qc", {}).get("checks") or {}).items()},
                      "process": r.get("process"), "recorded_at": r["measured_at"]}
            for r in results if not r.get("error")}


# ── report ───────────────────────────────────────────────────────────────────

def format_report(results: list[dict]) -> str:
    lines = []
    for r in results:
        lines.append(f"\n== {r['id']}: {r.get('setting', '')}")
        if r.get("error"):
            lines.append(f"   {r['error']}")
            continue
        process = r.get("process") or {}
        ifg = process.get("interferograms") or {}
        lines.append(f"   {r['direction'].lower()} track {r['track']}, {r['dates']} dates, {ifg.get('used')}/{ifg.get('in_stack')} "
                     f"interferograms used (longest {ifg.get('longest_used_days')} d); unwrap correction "
                     f"{'on' if process.get('unwrap_correction') else 'off'}, fading correction "
                     f"{'on' if process.get('fading_correction') else 'off'}, stricter mask {'on' if process.get('mask_refinement') else 'off'}")
        egms = r.get("egms")
        if egms and "error" in egms:
            lines.append(f"   EGMS: {egms['error']}")
        elif egms:
            lines.append(f"   EGMS ({egms['n']} pixels): rmse {egms['rmse_mm_yr']} mm/yr, mad {egms['mad_mm_yr']}, r {egms['pearson_r']}, "
                         f"slope {egms['slope_b_on_a']}, stable-band rmse {egms['stable_rmse_mm_yr']}")
            lines.append("      bias by band: " + "  ".join(f"[{b['from']}..{b['to']}] {b['bias_mm_yr']:+.1f} (n={b['n']})" for b in egms["bands"]))
        gnss = r.get("gnss")
        if gnss and "error" in gnss:
            lines.append(f"   GNSS: {gnss['error']}")
        elif gnss:
            lines.append("   GNSS: " + "; ".join(f"{s['name']} gnss {s['gnss_mm_yr']:+.1f} insar {s['insar_mm_yr']:+.1f} ({s['dates']} dates)" for s in gnss["stations"]))
        precision = r.get("precision") or {}
        if "error" not in precision:
            lines.append(f"   precision at 10 km {precision.get('sigma_10km_mm_yr')} mm/yr; the typical difference between pixels "
                         f"(from the reference) is {precision.get('ratio_to_stated')} times the error MintPy states")
        flagged = [f"{c} ({s['status']})" for c, s in r["qc"]["checks"].items() if s["status"] != "ok"]
        lines.append(f"   quality checks: {r['qc']['status']}" + (f"; to look at: {', '.join(flagged)}" if flagged else ""))
    return "\n".join(lines)


def format_changes(moved: list[dict]) -> str:
    if not moved:
        return "\nNothing moved beyond its tolerance."
    lines = [f"\n{len(moved)} change(s) against the baseline:"]
    for m in moved:
        detail = m.get("note") or f"{m['was']} -> {m['now']} (change {m['change']:+}, tolerance {m['tolerance']})"
        if m.get("note") and m.get("was") is not None:
            detail = f"{m['was']} -> {m['now']} ({m['note']})"
        lines.append(f"  {m['case']}: {m['what']}: {detail}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the processing chain on the benchmark cases.")
    parser.add_argument("--cases", default="benchmark/cases.json", help="the manifest of cases")
    parser.add_argument("--only", action="append", help="run only this case id (may be repeated)")
    parser.add_argument("--no-gnss", action="store_true", help="skip the GNSS comparison (it downloads station series)")
    parser.add_argument("--baseline", help="compare with this baseline; the exit code is 1 if something moved")
    parser.add_argument("--update-baseline", help="write today's numbers to this file")
    parser.add_argument("--output", help="write the full results as JSON to this file")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.only:
        unknown = set(args.only) - {c["id"] for c in cases}
        if unknown:
            print(f"No such case: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in args.only]
    results = [run_case(case, with_gnss=not args.no_gnss) for case in cases]
    print(format_report(results))
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if args.update_baseline:
        previous = {}
        if Path(args.update_baseline).exists():
            previous = json.loads(Path(args.update_baseline).read_text(encoding="utf-8"))
        Path(args.update_baseline).write_text(json.dumps({**previous, **to_baseline(results)}, indent=1), encoding="utf-8")
        print(f"\nBaseline written to {args.update_baseline}")
    if args.baseline:
        moved = compare_with_baseline(results, json.loads(Path(args.baseline).read_text(encoding="utf-8")))
        print(format_changes(moved))
        return 1 if moved else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
