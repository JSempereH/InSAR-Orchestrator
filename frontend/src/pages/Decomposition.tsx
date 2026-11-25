import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { Decomposition, DecompositionBrief, decompositionApi, egmsApi, projectsApi, ValidationResult } from "../api/client";
import { LoadingDots } from "../components/LoadingDots";
import { CalibrationView, Metric, UncertaintyView } from "../components/JobMonitor/ValidationPanel";
import { StatusPill } from "../components/JobMonitor/status";

const muted = { color: "var(--text-muted)" } as const;
const fixed = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined || !Number.isFinite(value) ? "n/a" : value.toFixed(digits);

function errorText(error: unknown): string {
  return String((error as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? error);
}

function Section({ title, children, hint }: { title: string; children: React.ReactNode; hint?: string }) {
  return (
    <div className="card" style={{ padding: "16px 18px", marginTop: 16 }}>
      <div style={{ fontSize: 14, fontWeight: 600 }}>{title}</div>
      {hint && <div style={{ fontSize: 12, marginTop: 2, ...muted }}>{hint}</div>}
      <div style={{ marginTop: 10 }}>{children}</div>
    </div>
  );
}

function L3Component({ label, result }: { label: string; result?: ValidationResult & { error?: string } }) {
  if (!result) return null;
  if (result.error) return <div style={{ fontSize: 12, color: "var(--warning)", marginTop: 8 }}>{label}: {result.error}</div>;
  const stats = result.velocity_rereferenced ?? result.velocity_raw;
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{label}</span>
        <StatusPill status={result.verdict.status} />
        <span style={{ fontSize: 12 }}>{result.verdict.message}</span>
      </div>
      <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginTop: 8 }}>
        <Metric label="RMSE" value={fixed(stats.rmse_mm_yr)} unit="mm/year" note="After removing the common offset" />
        <Metric label="Correlation" value={fixed(stats.pearson_r, 2)} />
        <Metric label="Slope" value={fixed(stats.slope_b_on_a, 2)} note="1.0 means the same amplitude as EGMS" />
        <Metric label="Bias" value={fixed(stats.bias_mm_yr)} unit="mm/year" />
        <Metric label="Cells" value={(result.pixels ?? 0).toLocaleString()} note="100 m cells with a value in both" />
      </div>
      {result.uncertainty && <UncertaintyView u={result.uncertainty} />}
    </div>
  );
}

function Detail({ id, onDeleted }: { id: string; onDeleted: () => void }) {
  const qc = useQueryClient();
  const [upId, setUpId] = useState("");
  const [eastId, setEastId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { data: d } = useQuery({
    queryKey: ["decomposition", id],
    queryFn: () => decompositionApi.get(id),
    refetchInterval: (query) => (query.state.data?.checks.gnss.status === "RUNNING" ? 3_000 : false),
  });
  const { data: downloads } = useQuery({ queryKey: ["egms-downloads"], queryFn: egmsApi.listDownloads });
  const ups = (downloads ?? []).filter((x) => x.level === "L3" && x.product_type === "ORTHO-UP");
  const easts = (downloads ?? []).filter((x) => x.level === "L3" && x.product_type === "ORTHO-EAST");

  const update = (next: Decomposition) => qc.setQueryData(["decomposition", id], next);
  const l3 = useMutation({
    mutationFn: () => decompositionApi.validateL3(id, upId || ups[0]?.id, eastId || easts[0]?.id),
    onSuccess: (next) => { setError(null); update(next); },
    onError: (e) => setError(errorText(e)),
  });
  const gnss = useMutation({
    mutationFn: () => decompositionApi.validateGnss(id),
    onSuccess: (next) => { setError(null); update(next); },
    onError: (e) => setError(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: () => decompositionApi.remove(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["decompositions"] }); onDeleted(); },
  });

  if (!d) return <LoadingDots label="Loading…" />;
  const s = d.summary;
  const l3Result = d.checks.l3.result;
  const gnssResult = d.checks.gnss.result;
  const canCalibrate = {
    vertical: !!gnssResult?.vertical?.applied, east: !!gnssResult?.east?.applied,
    verticalError: !!l3Result?.vertical?.uncertainty, eastError: !!l3Result?.east?.uncertainty,
  };
  const layers: { layer: string; label: string; available: boolean; title: string }[] = [
    { layer: "vertical", label: "Vertical", available: true, title: "Vertical velocity in mm/year, up positive" },
    { layer: "east", label: "East-west", available: true, title: "East-west velocity in mm/year, east positive" },
    { layer: "vertical_std", label: "Vertical error", available: true, title: "Standard error propagated from MintPy's" },
    { layer: "east_std", label: "East-west error", available: true, title: "Standard error propagated from MintPy's" },
    { layer: "vertical_calibrated", label: "Vertical tied to GNSS", available: canCalibrate.vertical, title: "Vertical plus the GNSS shift. Available once the GNSS check confirms it." },
    { layer: "east_calibrated", label: "East-west tied to GNSS", available: canCalibrate.east, title: "Relative to the stations' average. Available once the GNSS check confirms it." },
    { layer: "vertical_std_calibrated", label: "Vertical realistic error", available: canCalibrate.verticalError, title: "Error times the factor measured against EGMS L3" },
    { layer: "east_std_calibrated", label: "East-west realistic error", available: canCalibrate.eastError, title: "Error times the factor measured against EGMS L3" },
  ];

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h3 style={{ margin: 0 }}>{d.name}</h3>
        <button className="btn btn-danger btn-sm" onClick={() => { if (window.confirm("Delete these products? The two projects are not touched.")) remove.mutate(); }}>
          Delete
        </button>
      </div>
      <div style={{ fontSize: 12, marginTop: 4, ...muted }}>
        Ascending: {d.ascending.name ?? "deleted project"} ({s.ascending.start} to {s.ascending.end}). Descending: {d.descending.name ?? "deleted project"} ({s.descending.start} to {s.descending.end}).
      </div>
      {d.stale && (
        <div style={{ marginTop: 8, fontSize: 12, color: "var(--warning)" }}>
          One of the two projects has run MintPy again since these were made. Make them again to use the new result.
        </div>
      )}
      {!d.products_available && <div style={{ marginTop: 8, fontSize: 12, color: "var(--danger)" }}>The files of this decomposition are missing.</div>}

      {d.products_available && (
        <img src={decompositionApi.previewUrl(d.id, d.created_at ?? "")}
             alt="Vertical and east-west velocity maps" style={{ width: "100%", maxWidth: 960, marginTop: 14, borderRadius: 12, border: "1px solid var(--border)" }} />
      )}

      <Section title="How it was made" hint="Every value is relative to one reference point, where the ground is assumed not to move.">
        <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
          <Metric label="Pixels" value={s.pixels.toLocaleString()} note={`${s.masked_for_geometry} more were masked because the two lines of sight were too alike`} />
          <Metric label="Vertical error" value={fixed(s.vertical_std_median_mm_yr)} unit="mm/year" note="Median standard error, propagated from MintPy's. It is optimistic: see the checks below." />
          <Metric label="East-west error" value={fixed(s.east_std_median_mm_yr)} unit="mm/year" />
          <Metric label="Conditioning" value={fixed(s.median_condition_number, 1)} note="Condition number of the 2 x 2 system. About 1 is ideal, above 8 is masked." />
          <Metric label="Reference" value={`${s.reference.lat.toFixed(4)}, ${s.reference.lon.toFixed(4)}`} />
        </div>
        <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 12, ...muted }}>
          {s.assumptions.map((a) => <li key={a}>{a}</li>)}
          <li>
            A difference of 1 mm/year in north motion between a pixel and the reference appears as {fixed(s.north_leakage.into_vertical, 2)} mm/year of vertical
            and {fixed(s.north_leakage.into_east, 2)} of east-west motion.
          </li>
        </ul>
      </Section>

      <Section title="Check against EGMS L3" hint="EGMS publishes the vertical and east-west components too. Download the ORTHO-UP and ORTHO-EAST products of the area from the EGMS page first.">
        {ups.length === 0 || easts.length === 0 ? (
          <div style={{ fontSize: 12, ...muted }}>No EGMS L3 download yet: one ORTHO-UP and one ORTHO-EAST are needed.</div>
        ) : (
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
            <div className="field"><label>Vertical (ORTHO-UP)</label>
              <select className="input" value={upId || ups[0].id} onChange={(e) => setUpId(e.target.value)}>
                {ups.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select></div>
            <div className="field"><label>East-west (ORTHO-EAST)</label>
              <select className="input" value={eastId || easts[0].id} onChange={(e) => setEastId(e.target.value)}>
                {easts.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select></div>
            <button className="btn btn-primary" onClick={() => l3.mutate()} disabled={l3.isPending}>{l3.isPending ? "Comparing…" : "Compare"}</button>
          </div>
        )}
        {l3Result && (
          <>
            <L3Component label="Vertical" result={l3Result.vertical} />
            <L3Component label="East-west" result={l3Result.east} />
            <div style={{ fontSize: 11, marginTop: 10, ...muted }}>{l3Result.window_note}</div>
          </>
        )}
      </Section>

      <Section title="Check against GNSS" hint="Stations of the Nevada Geodetic Laboratory inside the area. The vertical component is absolute, the horizontal one is relative to the stations' average.">
        <button className="btn btn-primary" onClick={() => gnss.mutate()} disabled={gnss.isPending || d.checks.gnss.status === "RUNNING"}>
          {d.checks.gnss.status === "RUNNING" ? "Running…" : "Check against GNSS"}
        </button>
        {d.checks.gnss.status === "RUNNING" && <div style={{ marginTop: 8 }}><LoadingDots label={d.checks.gnss.progress || "Working…"} /></div>}
        {d.checks.gnss.status === "FAILED" && <div style={{ marginTop: 8, fontSize: 12, color: "var(--danger)" }}>{d.checks.gnss.error}</div>}
        {gnssResult && (
          <>
            {gnssResult.vertical && <CalibrationView c={gnssResult.vertical} title="Vertical" />}
            {gnssResult.east && <CalibrationView c={gnssResult.east} title="East-west" />}
            <div style={{ fontSize: 11, marginTop: 10, ...muted }}>
              {gnssResult.stations_found} stations in the area, window {gnssResult.window[0]} to {gnssResult.window[1]}.
              {gnssResult.stations_skipped.length > 0 && ` Skipped, no series: ${gnssResult.stations_skipped.join(", ")}.`}
              <br />{gnssResult.citation}
            </div>
          </>
        )}
      </Section>

      {error && <div style={{ marginTop: 12, fontSize: 12, color: "var(--danger)" }}>{error}</div>}

      <Section title="Export" hint="GeoTIFF in millimetres per year, vertical up and east positive, with the assumptions written inside each file. The tied and realistic-error layers appear once a check justifies them.">
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {layers.filter((l) => l.available).map((l) => (
            <a key={l.layer} className="btn btn-secondary btn-sm" download title={l.title} href={decompositionApi.exportUrl(d.id, l.layer)}>{l.label}</a>
          ))}
        </div>
      </Section>
    </div>
  );
}

/**
 * Vertical and east-west motion from an ascending and a descending project of the same area. One track only measures
 * along its line of sight, which mixes both, and two tracks that look from opposite sides can be told apart.
 */
export function DecompositionPage() {
  const qc = useQueryClient();
  const [first, setFirst] = useState("");
  const [second, setSecond] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const { data: list } = useQuery({ queryKey: ["decompositions"], queryFn: decompositionApi.list });

  const create = useMutation({
    mutationFn: () => decompositionApi.create({ first_project_id: first, second_project_id: second }),
    onSuccess: (made) => {
      setError(null);
      qc.setQueryData(["decomposition", made.id], made);
      qc.invalidateQueries({ queryKey: ["decompositions"] });
      setSelected(made.id);
    },
    onError: (e) => setError(errorText(e)),
  });

  const choices = (projects ?? []).filter((p) => p.engine !== "nisar_gunw");
  const label = (p: { name: string; flight_direction?: string }) => `${p.name} (${(p.flight_direction ?? "direction not set").toLowerCase()})`;

  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <h2 style={{ marginBottom: 4 }}>Vertical and east-west</h2>
      <p style={{ margin: "0 0 20px", color: "var(--text-muted)", fontSize: 13, maxWidth: 720 }}>
        One track measures motion along its line of sight, which mixes vertical and horizontal movement. An ascending and a descending
        project of the same area, both with MintPy finished, can be separated into vertical and east-west velocity.
      </p>

      <div className="card" style={{ padding: "16px 18px" }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>Make a new one</div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginTop: 10 }}>
          <div className="field"><label>Project</label>
            <select className="input" value={first} onChange={(e) => setFirst(e.target.value)}>
              <option value="">Choose…</option>
              {choices.map((p) => <option key={p.id} value={p.id}>{label(p)}</option>)}
            </select></div>
          <div className="field"><label>Project from the opposite direction</label>
            <select className="input" value={second} onChange={(e) => setSecond(e.target.value)}>
              <option value="">Choose…</option>
              {choices.filter((p) => p.id !== first).map((p) => <option key={p.id} value={p.id}>{label(p)}</option>)}
            </select></div>
          <button className="btn btn-primary" disabled={!first || !second || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? "Separating…" : "Separate"}
          </button>
        </div>
        {error && <div style={{ marginTop: 10, fontSize: 12, color: "var(--danger)" }}>{error}</div>}
      </div>

      {list && list.length > 0 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 16 }}>
          {list.map((x: DecompositionBrief) => (
            <button key={x.id} className={x.id === selected ? "btn btn-primary btn-sm" : "btn btn-secondary btn-sm"} onClick={() => setSelected(x.id)}>
              {x.name}
            </button>
          ))}
        </div>
      )}

      {selected && <div style={{ marginTop: 18 }}><Detail key={selected} id={selected} onDeleted={() => setSelected(null)} /></div>}
    </div>
  );
}
