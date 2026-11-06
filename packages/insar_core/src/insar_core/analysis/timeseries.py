"""Statistical context for one InSAR displacement time series (values in mm).

The approach follows common practice in the PSI/SBAS literature:

* Model selection among stable / linear / quadratic / bilinear (a change in
  trend) / step (a discontinuity), in the spirit of Berti et al. (2013, NHESS),
  but ranked with the Bayesian information criterion (BIC) instead of a chain of
  significance tests. A more complex model is only accepted when it beats the
  simpler one by a clear margin (BIC gain of at least 6, "strong" evidence).
* An annual harmonic is fitted jointly with the trend when the record spans at
  least 1.5 years (MintPy's timeseries2velocity offers the same periodic term).
* Velocity from least squares, with an uncertainty widened for lag-1
  autocorrelation of the residuals, plus the robust Theil-Sen slope and the
  Mann-Kendall test for the existence of a monotonic trend.
* Robust outlier flagging (median absolute deviation) and sampling gaps.

Everything is vectorized with numpy: all candidate break dates are fitted in one
batched solve, so a typical series (a few hundred dates) takes a few
milliseconds. numpy is the only dependency.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

import numpy as np

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

BIC_MARGIN = 6.0
MIN_POINTS = 8
_SEASONAL_MIN_YEARS = 1.5
_THEIL_SEN_MAX_POINTS = 600


# ── helpers ──────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> np.datetime64:
    text = str(raw)
    digits = text if len(text) == 8 and text.isdigit() else "".join(ch for ch in text if ch.isdigit())[:8]
    return np.datetime64(f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}", "D")


def _decimal_years(dates: np.ndarray) -> np.ndarray:
    years = dates.astype("datetime64[Y]")
    start = years.astype("datetime64[D]")
    nxt = (years + np.timedelta64(1, "Y")).astype("datetime64[D]")
    return years.astype(int) + 1970 + (dates - start) / (nxt - start)


def _bic(n: int, sse: np.ndarray | float, k: int) -> np.ndarray | float:
    return n * np.log(np.maximum(sse, 1e-12) / n) + k * math.log(n)


def _fit(X: np.ndarray, y: np.ndarray) -> dict[str, np.ndarray]:
    """Least squares for one design (n, p) or a batch of designs (K, n, p).

    One batched normal-equation solve covers every candidate, which is what
    makes the break-date scan cheap. p is at most 7, so conditioning is fine.
    """
    if X.ndim == 2:
        X = X[None]
    p = X.shape[2]
    Xt = X.transpose(0, 2, 1)
    xtx = Xt @ X + 1e-9 * np.eye(p)  # batched matmul runs on BLAS
    xty = Xt @ y
    inv = np.linalg.inv(xtx)
    beta = (inv @ xty[:, :, None])[:, :, 0]
    fitted = (X @ beta[:, :, None])[:, :, 0]
    sse = ((y - fitted) ** 2).sum(axis=1)
    return {"beta": beta, "inv": inv, "fitted": fitted, "sse": sse}


def _first(fit: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """The only design of a single-design batch."""
    return {k: v[0] for k, v in fit.items()}


def _lag1(res: np.ndarray) -> float:
    r = res - res.mean()
    den = float((r ** 2).sum())
    return float((r[1:] * r[:-1]).sum() / den) if den else 0.0


def _theil_sen(t: np.ndarray, y: np.ndarray) -> float:
    n = len(t)
    stride = max(1, math.ceil(n / _THEIL_SEN_MAX_POINTS))
    tt, yy = t[::stride], y[::stride]
    i, j = np.triu_indices(len(tt), 1)
    dt = tt[j] - tt[i]
    ok = dt != 0
    return float(np.median((yy[j] - yy[i])[ok] / dt[ok]))


def _mann_kendall(y: np.ndarray) -> tuple[float, float]:
    n = len(y)
    i, j = np.triu_indices(n, 1)
    s = float(np.sign(y[j] - y[i]).sum())
    var = n * (n - 1) * (2 * n + 5)
    _, counts = np.unique(y, return_counts=True)
    for g in counts[counts > 1]:
        var -= g * (g - 1) * (2 * g + 5)
    var /= 18
    z = (s - float(np.sign(s))) / math.sqrt(var) if var > 0 else 0.0
    return z, math.erfc(abs(z) / math.sqrt(2))


def _describe_change(v1: float, v2: float, thr: float) -> tuple[str, str]:
    """Names the change between an earlier and a later velocity (mm/yr)."""
    big1, big2 = abs(v1) >= thr, abs(v2) >= thr

    def direction(v: float) -> str:
        return "subsidence" if v < 0 else "uplift"

    if big1 and big2 and v1 * v2 < 0:
        return "Trend reversal", f"the motion reverses from {direction(v1)} to {direction(v2)}"
    if not big1 and big2:
        return f"Onset of {direction(v2)}", "the ground was stable before and now moves"
    if big1 and not big2:
        return "Stabilising", f"the earlier {direction(v1)} has stopped"
    if big1 and big2:
        if abs(v2) > abs(v1):
            return f"Accelerating {direction(v2)}", f"{direction(v2)} is speeding up"
        return f"Decelerating {direction(v2)}", f"{direction(v2)} is slowing down"
    return "Non-linear (small)", "the variation is within the noise level"


def _signed(v: float, digits: int = 1) -> str:
    return f"{v:+.{digits}f}"


# ── main entry ───────────────────────────────────────────────────────────

def analyze_time_series(dates: Sequence[str], values_mm: Sequence[float]) -> Optional[dict[str, Any]]:
    """Analyze one displacement series.

    `dates` are "YYYYMMDD" or ISO strings, `values_mm` the cumulative
    displacement. Returns a JSON-ready dict, or None when fewer than 8 valid
    observations are available. Keys: n, start_date, end_date, span_years,
    median_step_days, gaps, max, min, mean, std, velocity, model, seasonal,
    noise, net_change_mm, series (dates, values, fit, trend), break_index,
    narrative.
    """
    raw_values = np.asarray(values_mm, dtype=float)
    parsed = np.array([_parse_date(d) for d in dates], dtype="datetime64[D]")
    keep = np.isfinite(raw_values)
    order = np.argsort(parsed[keep], kind="stable")
    d = parsed[keep][order]
    y = raw_values[keep][order]
    n = len(y)
    if n < MIN_POINTS:
        return None

    ty = _decimal_years(d)
    t = ty - ty[0]
    span = float(t[-1])
    iso = [str(x) for x in d]

    steps = np.diff(t) * 365.25
    med_step = float(np.median(steps))
    gaps = int((steps > 3 * med_step).sum())

    mean = float(y.mean())
    std = float(y.std(ddof=1))
    i_max, i_min = int(np.argmax(y)), int(np.argmin(y))

    one = np.ones(n)
    sin_c, cos_c = np.sin(2 * np.pi * ty), np.cos(2 * np.pi * ty)

    # Seasonal term, only when the record is long enough to separate it from the trend.
    season: list[np.ndarray] = []
    if span >= _SEASONAL_MIN_YEARS and n >= 12:
        plain = _fit(np.column_stack([one, t]), y)
        with_season = _fit(np.column_stack([one, t, sin_c, cos_c]), y)
        if _bic(n, with_season["sse"][0], 4) < _bic(n, plain["sse"][0], 2) - BIC_MARGIN:
            season = [sin_c, cos_c]
    ks = len(season)

    base = np.column_stack([one, *season]) if ks else one[:, None]
    stable = _first(_fit(base, y))
    linear = _first(_fit(np.column_stack([one, t, *season]), y))
    quad = _first(_fit(np.column_stack([one, t, t * t, *season]), y))

    # Scan one break point (trend change or step) over the interior of the record,
    # fitting every candidate in a single batched solve.
    lo = max(4, math.ceil(0.12 * n))
    hi = min(n - 5, math.floor(0.88 * n))
    candidates = np.arange(lo, hi + 1)
    bilinear = step = None
    if len(candidates):
        tb = t[candidates]
        K = len(candidates)

        def stack(extra: np.ndarray) -> np.ndarray:  # extra: (K, n), the column that varies per candidate
            X = np.empty((K, n, 3 + ks))
            X[:, :, 0] = 1.0
            X[:, :, 1] = t
            X[:, :, 2] = extra
            for c, col in enumerate(season):
                X[:, :, 3 + c] = col
            return X

        bi_fit = _fit(stack(np.maximum(0.0, t[None, :] - tb[:, None])), y)
        st_fit = _fit(stack((t[None, :] >= tb[:, None]).astype(float)), y)
        kb, ksx = int(np.argmin(bi_fit["sse"])), int(np.argmin(st_fit["sse"]))
        bilinear = {k: v[kb] for k, v in bi_fit.items()} | {"idx": int(candidates[kb])}
        step = {k: v[ksx] for k, v in st_fit.items()} | {"idx": int(candidates[ksx])}

    def pack(kind: str, fit: dict, k_params: int, extra: int = 0, idx: Optional[int] = None) -> dict[str, Any]:
        sse = float(fit["sse"])
        return {"kind": kind, "beta": fit["beta"], "fitted": fit["fitted"], "bic": float(_bic(n, sse, k_params + extra)), "idx": idx}

    cands = [
        pack("stable", stable, 1 + ks),
        pack("linear", linear, 2 + ks),
        pack("quadratic", quad, 3 + ks),
    ]
    if bilinear is not None and step is not None:
        cands.append(pack("bilinear", bilinear, 3 + ks, extra=1, idx=bilinear["idx"]))
        cands.append(pack("step", step, 3 + ks, extra=1, idx=step["idx"]))

    # Prefer the simplest adequate model: each extra level must win by BIC_MARGIN.
    chosen = cands[0]
    if cands[1]["bic"] < chosen["bic"] - BIC_MARGIN:
        chosen = cands[1]
    complex_best = min(cands[2:], key=lambda c: c["bic"])
    if complex_best["bic"] < chosen["bic"] - BIC_MARGIN:
        chosen = complex_best
    bic_gain = cands[0]["bic"] - chosen["bic"]

    fit_values = chosen["fitted"]
    beta = chosen["beta"]
    if ks:
        trend_values = fit_values - (beta[-2] * sin_c + beta[-1] * cos_c)
    else:
        trend_values = fit_values.copy()

    # Velocity (single straight line, for comparability) with autocorrelation-aware uncertainty.
    lin_res = y - linear["fitted"]
    r1 = max(-0.99, min(0.99, _lag1(lin_res)))
    n_eff = max(3.0, n * ((1 - max(r1, 0.0)) / (1 + max(r1, 0.0))))
    k_lin = 2 + ks
    sigma2 = float(linear["sse"]) / max(1, n - k_lin)
    se_ols = math.sqrt(sigma2 * float(linear["inv"][1][1])) * math.sqrt(n / n_eff)
    v_ols = float(linear["beta"][1])
    mk_z, mk_p = _mann_kendall(y)
    v_ts = _theil_sen(t, y)

    # Seasonal amplitude and peak month (harmonics are on absolute decimal year).
    if ks:
        a, b = float(beta[-2]), float(beta[-1])
        amplitude = math.hypot(a, b)
        phase = (math.atan2(a, b) / (2 * math.pi)) % 1.0
        peak_month = min(11, int(phase * 12))
    else:
        amplitude, peak_month = 0.0, 0

    # Noise and outliers on the chosen model's residuals.
    res = y - fit_values
    rms = float(np.sqrt((res ** 2).mean()))
    mad = float(np.median(np.abs(res - np.median(res)))) * 1.4826
    outliers: list[dict[str, Any]] = []
    if mad > 0:
        for i in np.nonzero(np.abs(res) > 3.5 * mad)[0]:
            outliers.append({"date": iso[i], "value": float(y[i]), "residual": float(res[i])})

    model: dict[str, Any] = {"kind": chosen["kind"], "label": "", "bic_gain": float(bic_gain)}
    net_change = float(trend_values[-1] - trend_values[0])
    narrative: list[str] = []
    thr = max(0.5, 1.96 * se_ols)  # velocities below this count as "no motion" when naming a change

    def direction(v: float) -> str:
        return "subsidence" if v < 0 else "uplift"

    kind = chosen["kind"]
    if kind == "stable":
        model["label"] = "Stable"
        narrative.append(f"No significant trend: the series stays within ±{2 * rms:.1f} mm of its mean.")
    elif kind == "linear":
        model["label"] = f"Steady {direction(v_ols)}"
        narrative.append(
            f"Steady {direction(v_ols)} at {_signed(v_ols)} ± {1.96 * se_ols:.1f} mm/yr (95%), "
            f"about {_signed(net_change)} mm over {span:.1f} years."
        )
    elif kind == "quadratic":
        c1, c2 = float(beta[1]), float(beta[2])
        v_start, v_end = c1, c1 + 2 * c2 * span
        model["acceleration_mm_yr2"] = 2 * c2
        label, text = _describe_change(v_start, v_end, thr)
        model["label"] = label
        narrative.append(
            f"Velocity changes gradually from {_signed(v_start)} to {_signed(v_end)} mm/yr "
            f"(acceleration {_signed(2 * c2)} mm/yr²): {text}."
        )
    elif kind == "bilinear":
        s1 = float(beta[1])
        s2 = s1 + float(beta[2])
        model.update(break_date=iso[chosen["idx"]], slope_before=s1, slope_after=s2)
        label, text = _describe_change(s1, s2, thr)
        model["label"] = label
        narrative.append(
            f"Change in trend around {model['break_date']}: {_signed(s1)} mm/yr before, {_signed(s2)} mm/yr after ({text})."
        )
    else:
        jump, slope = float(beta[2]), float(beta[1])
        model.update(break_date=iso[chosen["idx"]], step_mm=jump, slope_after=slope)
        model["label"] = "Abrupt shift"
        narrative.append(f"Abrupt shift of {_signed(jump)} mm on {model['break_date']}, with a background trend of {_signed(slope)} mm/yr.")

    if mk_p < 0.05 and kind == "stable":
        narrative.append(
            f"The Mann-Kendall test does flag a weak monotonic tendency (p={mk_p:.3f}), but the model comparison does not support a real trend."
        )
    if ks:
        narrative.append(f"Seasonal cycle of about {2 * amplitude:.1f} mm peak to peak, highest around {MONTHS[peak_month]}.")
    elif span < _SEASONAL_MIN_YEARS:
        narrative.append("The record is shorter than 1.5 years, so seasonality cannot be separated from the trend.")
    if outliers:
        narrative.append(
            f"{len(outliers)} possible outlier{'s' if len(outliers) > 1 else ''} (more than 3.5 robust standard deviations from the fit)."
        )
    if gaps:
        narrative.append(f"{gaps} sampling gap{'s' if gaps > 1 else ''} longer than 3 times the usual {med_step:.0f}-day interval.")

    return {
        "n": n,
        "start_date": iso[0],
        "end_date": iso[-1],
        "span_years": span,
        "median_step_days": med_step,
        "gaps": gaps,
        "max": {"value": float(y[i_max]), "date": iso[i_max]},
        "min": {"value": float(y[i_min]), "date": iso[i_min]},
        "mean": mean,
        "std": std,
        "velocity": {
            "ols_mm_yr": v_ols,
            "se_mm_yr": se_ols,
            "ci95": [v_ols - 1.96 * se_ols, v_ols + 1.96 * se_ols],
            "theil_sen_mm_yr": v_ts,
            "mann_kendall_z": mk_z,
            "mann_kendall_p": mk_p,
            "lag1_autocorr": r1,
        },
        "model": model,
        "seasonal": {"present": bool(ks), "amplitude_mm": amplitude, "peak_month": peak_month},
        "noise": {"rms_mm": rms, "outliers": outliers},
        "net_change_mm": net_change,
        "series": {"dates": iso, "values": y.tolist(), "fit": fit_values.tolist(), "trend": trend_values.tolist()},
        "break_index": chosen["idx"],
        "narrative": narrative,
    }
