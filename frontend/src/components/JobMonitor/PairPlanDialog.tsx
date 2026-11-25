import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { Batch, BatchPlan, jobsApi, PairSettings, PlannedPair } from "../../api/client";
import { LoadingDots } from "../LoadingDots";
import { StatusPill } from "./status";

const muted = { color: "var(--text-muted)" } as const;
const mono = { fontFamily: "var(--font-mono)", fontSize: 11 } as const;
const field = {
  width: "100%", padding: "6px 8px", fontSize: 12, borderRadius: 8, color: "var(--text)",
  background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", fontFamily: "var(--font)",
} as const;
const label = { fontSize: 12, fontWeight: 600, display: "block", marginBottom: 3 } as const;
const hint = { fontSize: 11, ...muted, marginTop: 3, lineHeight: 1.4 } as const;

const key = (p: { reference: string; secondary: string }) => `${p.reference}|${p.secondary}`;

interface Form {
  strategy: "neighbors" | "baseline";
  neighbors: string;
  pbMax: string;
  minDegree: string;
  maxDegree: string;
  dtMax: string;
  targets: string;
  annual: boolean;
  longPairs: boolean;
  calibrate: boolean;
}

const DEFAULT_FORM: Form = {
  strategy: "neighbors", neighbors: "3", pbMax: "150", minDegree: "2", maxDegree: "5", dtMax: "", targets: "", annual: false, longPairs: false, calibrate: false,
};

function toSettings(form: Form, excluded: Set<string>): PairSettings {
  const number = (text: string) => (text.trim() === "" || !Number.isFinite(Number(text)) ? undefined : Number(text));
  const settings: PairSettings = {
    strategy: form.strategy,
    max_temporal_neighbors: number(form.neighbors) ?? 3,
    exclude: [...excluded].map((k) => k.split("|") as [string, string]),
  };
  if (form.strategy === "neighbors") {
    settings.long_pairs_days = form.longPairs ? [90, 365] : [];
    settings.calibration_pair = form.calibrate;
  }
  if (form.strategy === "baseline") {
    settings.pb_max = form.pbMax.trim() === "" ? null : number(form.pbMax) ?? 150;
    settings.min_degree = number(form.minDegree) ?? 2;
    settings.max_degree = number(form.maxDegree) ?? 5;
    settings.dt_max = number(form.dtMax) ?? null;
    settings.include_annual = form.annual;
    const targets = form.targets.split(",").map((t) => Number(t.trim())).filter((n) => Number.isFinite(n) && n > 0);
    settings.dt_targets = targets.length > 0 ? targets : null;
  }
  return settings;
}

/** Acquisition dates on x, perpendicular baseline on y; each line is one interferogram. Click a line to leave it out. */
function Network({ plan, onToggle }: { plan: BatchPlan; onToggle: (pair: PlannedPair) => void }) {
  const width = 560, height = 300, left = 44, right = 12, top = 12, bottom = 30;
  const times = plan.scenes.map((s) => Date.parse(s.date));
  const t0 = Math.min(...times), t1 = Math.max(...times);
  const values = plan.scenes.map((s) => s.perpendicular_m ?? 0);
  const lo = Math.min(...values, -50), hi = Math.max(...values, 50);
  const x = (date: string) => left + ((Date.parse(date) - t0) / (t1 - t0 || 1)) * (width - left - right);
  const y = (v: number) => top + (1 - (v - lo) / (hi - lo || 1)) * (height - top - bottom);
  const point = new Map(plan.scenes.map((s) => [s.date, y(s.perpendicular_m ?? 0)]));
  const colour = (p: PlannedPair) =>
    p.state === "excluded" ? "rgba(255,255,255,0.18)" : p.state === "submitted" ? "#5d6663" : p.over_limit ? "#ffb020" : "#3dff9a";
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Network of interferograms"
         style={{ background: "rgba(0,0,0,0.25)", borderRadius: 12, border: "1px solid var(--border)" }}>
      {[lo, 0, hi].map((v) => (
        <g key={v}>
          <line x1={left} x2={width - right} y1={y(v)} y2={y(v)} stroke="rgba(255,255,255,0.06)" />
          <text x={left - 6} y={y(v) + 3} textAnchor="end" fill="#8a9490" fontSize="9" fontFamily="JetBrains Mono, monospace">{Math.round(v)}</text>
        </g>
      ))}
      <text x={left} y={height - 8} fill="#8a9490" fontSize="9" fontFamily="JetBrains Mono, monospace">{plan.scenes[0]?.date}</text>
      <text x={width - right} y={height - 8} textAnchor="end" fill="#8a9490" fontSize="9" fontFamily="JetBrains Mono, monospace">{plan.scenes[plan.scenes.length - 1]?.date}</text>
      <text x={10} y={height / 2} fill="#8a9490" fontSize="9" textAnchor="middle" transform={`rotate(-90 10 ${height / 2})`}>perpendicular baseline, m</text>
      {plan.pairs.map((p) => (
        <g key={key(p)} style={{ cursor: p.state === "submitted" ? "default" : "pointer" }} onClick={() => p.state !== "submitted" && onToggle(p)}>
          <title>{`${p.reference_date} to ${p.secondary_date}: ${p.temporal_days} days, ${p.perpendicular_m === null ? "baseline unknown" : `${Math.round(p.perpendicular_m)} m`}. ${p.state === "submitted" ? "Already submitted." : p.state === "excluded" ? "Left out. Click to include." : "Click to leave out."}`}</title>
          <line x1={x(p.reference_date)} y1={point.get(p.reference_date)} x2={x(p.secondary_date)} y2={point.get(p.secondary_date)}
                stroke={colour(p)} strokeWidth={1.4} strokeOpacity={p.state === "excluded" ? 0.7 : 0.8} strokeDasharray={p.state === "excluded" ? "3 3" : undefined} />
          <line x1={x(p.reference_date)} y1={point.get(p.reference_date)} x2={x(p.secondary_date)} y2={point.get(p.secondary_date)}
                stroke="transparent" strokeWidth={9} />
        </g>
      ))}
      {plan.scenes.map((s) => (
        <circle key={s.date} cx={x(s.date)} cy={y(s.perpendicular_m ?? 0)} r={3} fill={s.perpendicular_m === null ? "#8a9490" : "#eef2f0"} />
      ))}
    </svg>
  );
}

/**
 * Choose how the interferograms are paired, see the whole network and what it costs, and leave out
 * the pairs you do not want before anything is sent to HyP3.
 */
export function PairPlanDialog({
  projectId, projectNeighbors, autoDownload, onClose, onSubmitted,
}: {
  projectId: string;
  projectNeighbors: number;
  autoDownload: boolean;
  onClose: () => void;
  onSubmitted: (batch: Batch) => void;
}) {
  const [form, setForm] = useState<Form>({ ...DEFAULT_FORM, neighbors: String(projectNeighbors) });
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // wait for a pause in typing before asking for a new plan
  const [settled, setSettled] = useState(form);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(form), 400);
    return () => window.clearTimeout(timer);
  }, [form]);
  const settings = useMemo(() => toSettings(settled, excluded), [settled, excluded]);

  const { data: plan, isFetching, error: planError } = useQuery({
    queryKey: ["pair-plan", projectId, settings],
    queryFn: () => jobsApi.plan(projectId, { ...settings, dry_run: true }),
    placeholderData: (previous) => previous,
    staleTime: 60_000,
    retry: false,
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !submitting && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting]);

  const toggle = (pair: PlannedPair) =>
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(key(pair))) next.delete(key(pair)); else next.add(key(pair));
      return next;
    });

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const batch = await jobsApi.submit(projectId, { ...toSettings(form, excluded), dry_run: false, auto_download: autoDownload });
      onSubmitted(batch);
      onClose();
    } catch (e) {
      setError(String((e as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? e));
      setSubmitting(false);
    }
  }

  const set = <K extends keyof Form>(name: K, value: Form[K]) => setForm((prev) => ({ ...prev, [name]: value }));
  const planProblem = planError ? String((planError as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? planError) : null;
  const pending = form !== settled || isFetching;

  return (
    <div role="dialog" aria-modal="true" aria-label="Plan the interferogram pairs"
         onMouseDown={(e) => e.target === e.currentTarget && !submitting && onClose()}
         style={{ position: "fixed", inset: 0, zIndex: 50, background: "rgba(0,0,0,0.65)", display: "flex", alignItems: "flex-start", justifyContent: "center", overflowY: "auto", padding: "40px 16px" }}>
      <div className="card" style={{ width: "min(1020px, 100%)", padding: "18px 22px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ fontFamily: "var(--font-display)", fontWeight: 700, fontSize: 17, flex: 1 }}>Plan the pairs</div>
          <button className="btn btn-ghost btn-sm" onClick={onClose} disabled={submitting}>Close</button>
        </div>
        <div style={{ fontSize: 12, ...muted, marginTop: 4 }}>
          Every pair costs {plan?.credits_per_pair ?? 10} HyP3 credit{(plan?.credits_per_pair ?? 10) === 1 ? "" : "s"}. Each line below is one interferogram; click a line to leave it out.
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "minmax(230px, 280px) 1fr", gap: 22, marginTop: 14 }}>
          <div>
            <label htmlFor="pair-strategy" style={label}>How to choose pairs</label>
            <select id="pair-strategy" style={field} value={form.strategy} onChange={(e) => set("strategy", e.target.value as Form["strategy"])}>
              <option value="neighbors">Next N acquisitions</option>
              <option value="baseline">By time spacing and baseline</option>
            </select>
            {form.strategy === "neighbors" ? (
              <div style={{ marginTop: 10 }}>
                <label htmlFor="pair-neighbors" style={label}>Neighbours per date</label>
                <input id="pair-neighbors" style={field} value={form.neighbors} inputMode="numeric" onChange={(e) => set("neighbors", e.target.value)} />
                <div style={hint}>Each date is paired with the next N dates. Pairs beyond the baseline limit below are only counted, not removed.</div>
                <label style={{ ...label, marginTop: 10, display: "flex", gap: 6, alignItems: "center", fontWeight: 500 }}>
                  <input type="checkbox" checked={form.longPairs} onChange={(e) => set("longPairs", e.target.checked)} />
                  Add long pairs (about 3 months and a year)
                </label>
                <div style={hint}>Over soil and vegetation they reduce the false motion that short pairs add up (fading bias), but over ground that moves fast, such as a subsidence bowl, they lose that motion. Off unless the area is slow.</div>
                <label style={{ ...label, marginTop: 10, display: "flex", gap: 6, alignItems: "center", fontWeight: 500 }}>
                  <input type="checkbox" checked={form.calibrate} onChange={(e) => set("calibrate", e.target.checked)} />
                  Add one pair over the whole period
                </label>
                <div style={hint}>Lets MintPy options correct the fading bias of the short pairs (off by default there). The pair only calibrates; it is not used in the inversion.</div>
                <label htmlFor="pair-pb" style={{ ...label, marginTop: 10 }}>Baseline limit to flag (m)</label>
                <input id="pair-pb" style={field} value={form.pbMax} placeholder="No limit" inputMode="decimal" onChange={(e) => set("pbMax", e.target.value)} />
              </div>
            ) : (
              <div style={{ marginTop: 10, display: "grid", gap: 10 }}>
                <div>
                  <label htmlFor="pair-pb" style={label}>Largest perpendicular baseline (m)</label>
                  <input id="pair-pb" style={field} value={form.pbMax} placeholder="No limit" inputMode="decimal" onChange={(e) => set("pbMax", e.target.value)} />
                  <div style={hint}>A larger baseline amplifies DEM errors. Empty means no limit.</div>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <div style={{ flex: 1 }}>
                    <label htmlFor="pair-min" style={label}>Fewest per date</label>
                    <input id="pair-min" style={field} value={form.minDegree} inputMode="numeric" onChange={(e) => set("minDegree", e.target.value)} />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label htmlFor="pair-max" style={label}>Most per date</label>
                    <input id="pair-max" style={field} value={form.maxDegree} inputMode="numeric" onChange={(e) => set("maxDegree", e.target.value)} />
                  </div>
                </div>
                <div>
                  <label htmlFor="pair-targets" style={label}>Spacings to use (days)</label>
                  <input id="pair-targets" style={field} value={form.targets} placeholder="Automatic: 1 to 4 revisits" onChange={(e) => set("targets", e.target.value)} />
                  <div style={hint}>For example 12, 24, 36, 48. Automatic follows the revisit interval of the data.</div>
                </div>
                <div>
                  <label htmlFor="pair-dtmax" style={label}>Longest spacing (days)</label>
                  <input id="pair-dtmax" style={field} value={form.dtMax} placeholder="Automatic" inputMode="numeric" onChange={(e) => set("dtMax", e.target.value)} />
                </div>
                <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, cursor: "pointer" }}>
                  <input type="checkbox" checked={form.annual} onChange={(e) => set("annual", e.target.checked)} />
                  Also pair dates about a year apart
                </label>
                <div style={hint}>Long pairs help against the bias that very short ones accumulate. Over ground that moves fast (a subsidence bowl) they lose that motion: leave them out there.</div>
              </div>
            )}
            {excluded.size > 0 && (
              <button className="btn btn-ghost btn-sm" style={{ marginTop: 12 }} onClick={() => setExcluded(new Set())}>
                Include the {excluded.size} pair{excluded.size === 1 ? "" : "s"} left out
              </button>
            )}
          </div>

          <div style={{ minWidth: 0 }}>
            {planProblem && <div style={{ color: "var(--danger)", fontSize: 12, marginBottom: 8 }}>{planProblem}</div>}
            {!plan && !planProblem && <LoadingDots label="Searching ASF and planning…" />}
            {plan && plan.scenes.length > 0 && <Network plan={plan} onToggle={toggle} />}
            {plan && plan.scenes.length === 0 && <div style={{ fontSize: 13, ...muted }}>No Sentinel-1 acquisitions were found for this project.</div>}
            {plan && (
              <div style={{ marginTop: 12, opacity: pending ? 0.6 : 1 }}>
                <div style={{ display: "flex", gap: 22, flexWrap: "wrap", alignItems: "flex-end" }}>
                  <div><div style={{ fontSize: 11, ...muted }}>Pairs to submit</div><div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 700 }}>{plan.total_pairs}</div></div>
                  <div><div style={{ fontSize: 11, ...muted }}>HyP3 credits</div><div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 700 }}>{plan.credits}</div></div>
                  <div><div style={{ fontSize: 11, ...muted }}>Dates</div><div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 700 }}>{plan.scene_count}</div></div>
                  {plan.estimated_size_gb != null && <div><div style={{ fontSize: 11, ...muted }}>Download</div><div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 700 }}>{plan.estimated_size_gb}<span style={{ fontSize: 11, fontWeight: 400, ...muted }}> GB</span></div></div>}
                  <StatusPill status={plan.connected ? "ok" : "fail"} text={plan.connected ? "Network connected" : `${plan.components} separate groups`} />
                  {plan.over_limit > 0 && <StatusPill status="warn" text={`${plan.over_limit} over the baseline limit`} />}
                </div>
                {plan.warnings.map((w) => <div key={w} style={{ marginTop: 6, fontSize: 12, color: "var(--warning)" }}>{w}</div>)}
                <div style={{ marginTop: 8, fontSize: 11, ...muted }}>
                  <span style={{ color: "var(--accent)" }}>Green</span> new, <span style={{ color: "var(--warning)" }}>amber</span> over the baseline limit,
                  grey already submitted, dashed left out. Dots are dates at their perpendicular baseline{plan.scenes.some((s) => s.perpendicular_m === null) ? " (grey dots: baseline unknown)" : ""}.
                  <span style={mono}> {plan.pairs.length} pairs in the network.</span>
                </div>
              </div>
            )}
          </div>
        </div>

        {error && <div style={{ marginTop: 12, color: "var(--danger)", fontSize: 12 }}>{error}</div>}
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 16 }}>
          <button className="btn btn-ghost" onClick={onClose} disabled={submitting}>Cancel</button>
          <button className="btn btn-primary" disabled={!plan || plan.total_pairs === 0 || pending || submitting || !!planProblem} onClick={submit}>
            {submitting ? "Submitting…" : plan ? `Submit ${plan.total_pairs} pairs (${plan.credits} credits)` : "Submit"}
          </button>
        </div>
      </div>
    </div>
  );
}
