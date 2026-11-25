import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { egmsApi, GnssCalibration, UncertaintyCheck, ValidationBand, ValidationEntry, ValidationResult, validationApi } from "../../api/client";
import { LoadingDots } from "../LoadingDots";
import { StatusPill } from "./status";

const mono = { fontFamily: "var(--font-mono)", fontSize: 11 } as const;
const muted = { color: "var(--text-muted)" } as const;

const fixed = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined || !Number.isFinite(value) ? "n/a" : value.toFixed(digits);

export function Metric({ label, value, unit, note }: { label: string; value: string; unit?: string; note?: string }) {
  return (
    <div style={{ minWidth: 110 }} title={note}>
      <div style={{ fontSize: 11, ...muted }}>{label}</div>
      <div style={{ fontFamily: "var(--font-display)", fontSize: 18, fontWeight: 700 }}>
        {value}<span style={{ fontSize: 11, fontWeight: 400, ...muted }}>{unit ? ` ${unit}` : ""}</span>
      </div>
    </div>
  );
}

/** Each point is a pixel: the other source's velocity on x, MintPy's on y, both without the common offset. */
function Scatter({ other, mintpy }: { other: number[]; mintpy: number[] }) {
  const size = 240, pad = 34;
  const all = [...other, ...mintpy];
  const lo = Math.floor(Math.min(...all) / 10) * 10, hi = Math.ceil(Math.max(...all) / 10) * 10;
  const scale = (v: number) => pad + ((v - lo) / (hi - lo || 1)) * (size - pad - 8);
  const flip = (v: number) => size - scale(v) + pad - 8;
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <svg width={size} height={size} role="img" aria-label="MintPy velocity against the other source's velocity, per pixel"
         style={{ background: "rgba(0,0,0,0.25)", borderRadius: 12, border: "1px solid var(--border)", flexShrink: 0 }}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={scale(t)} x2={scale(t)} y1={8} y2={size - pad + 8} stroke="rgba(255,255,255,0.06)" />
          <line x1={pad} x2={size - 8} y1={flip(t)} y2={flip(t)} stroke="rgba(255,255,255,0.06)" />
          <text x={scale(t)} y={size - 14} fill="#8a9490" fontSize="9" textAnchor="middle" fontFamily="JetBrains Mono, monospace">{t}</text>
          <text x={pad - 5} y={flip(t) + 3} fill="#8a9490" fontSize="9" textAnchor="end" fontFamily="JetBrains Mono, monospace">{t}</text>
        </g>
      ))}
      <line x1={scale(lo)} y1={flip(lo)} x2={scale(hi)} y2={flip(hi)} stroke="#ffffff" strokeDasharray="4 4" strokeOpacity="0.55" />
      {other.map((x, i) => (
        <circle key={i} cx={scale(x)} cy={flip(mintpy[i])} r={1.6} fill="#3dff9a" fillOpacity="0.45" />
      ))}
      <text x={size / 2} y={size - 2} fill="#8a9490" fontSize="9" textAnchor="middle">other source, mm/year</text>
      <text x={9} y={size / 2} fill="#8a9490" fontSize="9" textAnchor="middle" transform={`rotate(-90 9 ${size / 2})`}>MintPy, mm/year</text>
    </svg>
  );
}

function bandLabel(band: ValidationBand) {
  if (band.from === null) return `below ${band.to}`;
  if (band.to === null) return `above ${band.from}`;
  return `${band.from} to ${band.to}`;
}

/** Is the error MintPy states for each velocity believable? Measured against the other source. */
export function UncertaintyView({ u }: { u: UncertaintyCheck }) {
  const tooSmall = u.scale > 1.2;
  return (
    <div style={{ marginTop: 14, padding: "10px 12px", border: "1px solid var(--border)", borderRadius: 12, background: "rgba(0,0,0,0.2)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>Stated error</span>
        <StatusPill status={tooSmall ? "warn" : "ok"} />
      </div>
      <div style={{ fontSize: 12, marginTop: 6 }}>{u.message}</div>
      <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginTop: 10 }}>
        <Metric label="Stated error" value={fixed(u.median_stated_sigma_mm_yr)} unit="mm/year" note="Median standard error MintPy reports for the velocity" />
        <Metric label="Observed spread" value={fixed(u.median_observed_spread_mm_yr)} unit="mm/year" note="Robust spread of the difference with the other source" />
        <Metric label="Within 1 sigma" value={`${fixed(u.coverage_1sigma * 100, 0)}%`} note="68% would be expected if the stated error were right" />
        <Metric label="Within 2 sigma" value={`${fixed(u.coverage_2sigma * 100, 0)}%`} note="95% would be expected if the stated error were right" />
        <Metric label="Scale" value={`x${fixed(u.recommended_scale, 2)}`} note="Factor the calibrated error layer multiplies the stated error by (never below 1)" />
      </div>
      <div style={{ fontSize: 11, ...muted, marginTop: 6 }}>
        The other source also has errors that are not subtracted when it states none, so the factor can be a little high.
      </div>
    </div>
  );
}

/** The shift that ties the velocity map to the GNSS stations, and the test on stations it had not seen. */
export function CalibrationView({ c, title = "Calibration to GNSS" }: { c: GnssCalibration; title?: string }) {
  if (c.error) {
    return <div style={{ marginTop: 14, fontSize: 12, color: "var(--warning)" }}>{title}: {c.error}</div>;
  }
  return (
    <div style={{ marginTop: 14, padding: "10px 12px", border: "1px solid var(--border)", borderRadius: 12, background: "rgba(0,0,0,0.2)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>{title}</span>
        {c.verdict && <StatusPill status={c.verdict.status} />}
        <span style={{ fontSize: 11, ...muted }}>{c.applied ? "applied to the calibrated layer" : "not applied"}</span>
      </div>
      {c.verdict && <div style={{ fontSize: 12, marginTop: 6 }}>{c.verdict.message}</div>}
      <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginTop: 10 }}>
        <Metric label="Shift" value={`${(c.offset_mm_yr ?? 0) >= 0 ? "+" : ""}${fixed(c.offset_mm_yr)}`} unit="mm/year"
                note="Added to the map so that it agrees with the stations" />
        <Metric label="Uncertainty" value={fixed(c.offset_sigma_mm_yr)} unit="mm/year" note="Of the shift, from the stations' errors and how much they disagree" />
        <Metric label="Error before" value={fixed(c.rmse_before_mm_yr)} unit="mm/year" note="RMSE against the stations without the shift" />
        <Metric label="Error after" value={c.loo_rmse_after_mm_yr == null ? "n/a" : fixed(c.loo_rmse_after_mm_yr)} unit="mm/year"
                note="RMSE against each station when the shift is estimated from the others, so it has not seen it" />
        <Metric label="Stations" value={String(c.n)} />
      </div>
      {c.stations && c.stations.length > 0 && (
        <table style={{ borderCollapse: "collapse", fontSize: 11, width: "100%", marginTop: 10 }}>
          <thead>
            <tr style={{ ...muted, textAlign: "right" }}>
              <th style={{ textAlign: "left", fontWeight: 600 }}>Station</th><th style={{ fontWeight: 600 }}>GNSS</th>
              <th style={{ fontWeight: 600 }}>Map</th><th style={{ fontWeight: 600 }}>Residual</th><th style={{ fontWeight: 600 }}>Left out</th>
              <th style={{ fontWeight: 600 }}>Estimator</th>
            </tr>
          </thead>
          <tbody>
            {c.stations.map((st) => (
              <tr key={st.name} style={{ textAlign: "right", borderTop: "1px solid var(--border)" }}>
                <td style={{ textAlign: "left", ...mono }}>{st.name}</td><td style={mono}>{fixed(st.gnss_mm_yr)}</td>
                <td style={mono}>{fixed(st.insar_mm_yr)}</td><td style={mono}>{fixed(st.residual_mm_yr)}</td>
                <td style={mono}>{st.left_out_residual_mm_yr === undefined ? "n/a" : fixed(st.left_out_residual_mm_yr)}</td>
                <td style={mono} title={st.method === "midas"
                  ? "MIDAS: the median of the slopes between positions one year apart, which the seasons and a missed jump do not move"
                  : "A straight line by least squares: under two years of data, so the seasons are in it, as they are in the InSAR velocity"}>
                  {st.method === "midas" ? "MIDAS" : st.method === "least_squares" ? "line" : "n/a"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {c.frame_removed_mm_yr && c.frame === "plate" && c.plate && (
        <div style={{ fontSize: 11, ...muted, marginTop: 6 }}>
          The motion of the {c.plate.name} plate ({fixed(c.frame_removed_mm_yr.east)} mm/year east, {fixed(c.frame_removed_mm_yr.north)} north,
          ITRF2014 plate motion model) was removed from the stations first, so the calibrated map is relative to the rigid plate.
          {c.plate.misfit_mm_yr != null && ` The stations move ${fixed(c.plate.misfit_mm_yr)} mm/year relative to it.`}
        </div>
      )}
      {c.frame_removed_mm_yr && c.frame !== "plate" && (
        <div style={{ fontSize: 11, ...muted, marginTop: 6 }}>
          The motion the stations share ({fixed(c.frame_removed_mm_yr.east)} mm/year east, {fixed(c.frame_removed_mm_yr.north)} north) was removed
          first, so horizontal values are relative to the stations' average, not to a plate.
        </div>
      )}
    </div>
  );
}

function ResultView({ entry }: { entry: ValidationEntry }) {
  const r = entry.result as ValidationResult;
  const stats = r.velocity_rereferenced ?? r.velocity_raw;
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <StatusPill status={r.verdict.status} />
        <span style={{ fontSize: 12 }}>{r.verdict.message}</span>
      </div>
      {entry.stale && (
        <div style={{ marginTop: 8, fontSize: 12, color: "var(--warning)" }}>
          MintPy has run again since this was computed. Run the validation again to check the new result.
        </div>
      )}
      {r.warnings?.map((w) => <div key={w} style={{ marginTop: 6, fontSize: 12, color: "var(--warning)" }}>{w}</div>)}

      <div style={{ display: "flex", gap: 22, flexWrap: "wrap", marginTop: 12 }}>
        <Metric label="RMSE" value={fixed(stats.rmse_mm_yr)} unit="mm/year" note="Root mean square difference of the velocities, after removing the common offset" />
        <Metric label="Correlation" value={fixed(stats.pearson_r, 2)} note="Pearson r between the two velocity maps" />
        <Metric label="Slope" value={fixed(stats.slope_orthogonal ?? stats.slope_b_on_a, 2)}
                note="1.0 means MintPy has the same amplitude as the other source. Fitted allowing for noise in both (orthogonal regression), so the noise of the other source does not pull it below 1" />
        <Metric label="Bias" value={fixed(stats.bias_mm_yr)} unit="mm/year" note="Mean MintPy minus the other source, after removing the common offset" />
        <Metric label="Offset removed" value={fixed(r.velocity_raw.bias_mm_yr)} unit="mm/year" note="Raw mean difference: where each source puts its zero" />
        {r.timeseries_rmse_mm && <Metric label="Series error" value={fixed(r.timeseries_rmse_mm.median)} unit="mm" note="Median over pixels of the RMSE of the displacement series" />}
        {r.noise_mm_yr_within_pixel !== undefined && <Metric label="Source noise" value={fixed(r.noise_mm_yr_within_pixel)} unit="mm/year" note="How much the points inside one pixel disagree with each other: the floor for any comparison" />}
      </div>
      <div style={{ fontSize: 11, ...muted, marginTop: 8 }}>
        {r.pixels !== undefined && <>{r.pixels.toLocaleString()} pixels from {r.points?.toLocaleString()} points (median {fixed(r.points_per_pixel_median, 0)} per pixel), </>}
        {r.dates !== undefined && <>{r.dates} dates {r.window?.[0]} to {r.window?.[1]}. </>}
        {r.source_name}{r.release ? `, release ${r.release}` : ""}.
      </div>

      {r.uncertainty && <UncertaintyView u={r.uncertainty} />}
      {r.calibration && <CalibrationView c={r.calibration} />}

      {r.scatter && r.scatter.other.length > 0 && (
        <div style={{ display: "flex", gap: 18, flexWrap: "wrap", marginTop: 12, alignItems: "flex-start" }}>
          <Scatter other={r.scatter.other} mintpy={r.scatter.mintpy} />
          {r.by_velocity && r.by_velocity.length > 0 && (
            <div style={{ flex: 1, minWidth: 260 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Error by how fast the ground moves</div>
              <table style={{ borderCollapse: "collapse", fontSize: 11, width: "100%" }}>
                <thead>
                  <tr style={{ ...muted, textAlign: "right" }}>
                    <th style={{ textAlign: "left", fontWeight: 600 }}>Other source (mm/yr)</th>
                    <th style={{ fontWeight: 600 }}>Pixels</th><th style={{ fontWeight: 600 }}>Other</th>
                    <th style={{ fontWeight: 600 }}>MintPy</th><th style={{ fontWeight: 600 }}>Bias</th><th style={{ fontWeight: 600 }}>RMSE</th>
                  </tr>
                </thead>
                <tbody>
                  {r.by_velocity.map((band) => (
                    <tr key={bandLabel(band)} style={{ textAlign: "right", borderTop: "1px solid var(--border)" }}>
                      <td style={{ textAlign: "left", ...mono }}>{bandLabel(band)}</td>
                      <td style={mono}>{band.n}</td><td style={mono}>{fixed(band.other_mm_yr)}</td>
                      <td style={mono}>{fixed(band.mintpy_mm_yr)}</td>
                      <td style={{ ...mono, color: Math.abs(band.bias_mm_yr) > 5 ? "var(--warning)" : undefined }}>{fixed(band.bias_mm_yr)}</td>
                      <td style={mono}>{fixed(band.rmse_mm_yr)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div style={{ fontSize: 11, ...muted, marginTop: 4 }}>
                A bias that grows with the speed means fast motion is being lost, often to unwrapping errors.
              </div>
            </div>
          )}
        </div>
      )}

      {r.stations && (
        <table style={{ borderCollapse: "collapse", fontSize: 11, width: "100%", marginTop: 12 }}>
          <thead>
            <tr style={{ ...muted, textAlign: "right" }}>
              <th style={{ textAlign: "left", fontWeight: 600 }}>Station</th><th style={{ fontWeight: 600 }}>Dates</th>
              <th style={{ fontWeight: 600 }}>GNSS (mm/yr)</th><th style={{ fontWeight: 600 }}>InSAR (mm/yr)</th>
              <th style={{ fontWeight: 600 }}>Difference</th><th style={{ fontWeight: 600 }}>Series RMSE (mm)</th>
            </tr>
          </thead>
          <tbody>
            {r.stations.map((s) => (
              <tr key={s.name} style={{ textAlign: "right", borderTop: "1px solid var(--border)" }}>
                <td style={{ textAlign: "left", ...mono }}>{s.name}</td><td style={mono}>{s.dates}</td>
                <td style={mono}>{fixed(s.gnss_mm_yr)}</td><td style={mono}>{fixed(s.insar_mm_yr)}</td>
                <td style={mono}>{fixed(s.difference_rereferenced_mm_yr)}</td><td style={mono}>{fixed(s.series_rmse_mm)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {r.stations && (
        <div style={{ fontSize: 11, ...muted, marginTop: 6 }}>
          {r.plate
            ? `The motion of the ${r.plate.name} plate (ITRF2014 plate motion model) was removed from the stations, so the offset removed is the motion of MintPy's reference point. `
            : "GNSS stations include the motion of the plate, so a common offset between GNSS and InSAR is expected and removed. "}
          {r.stations.some((s) => s.steps?.some((x) => x.action !== "kept")) &&
            `Jumps of equipment changes taken out of the GNSS series: ${r.stations.flatMap((s) => (s.steps ?? [])
              .filter((x) => x.action !== "kept")
              .map((x) => `${s.name} ${x.date}${x.jump_mm ? ` (${fixed(x.jump_mm[2])} mm up)` : " (too close to the edge, that side dropped)"}`)).join(", ")}. `}
          {r.stations_skipped && r.stations_skipped.length > 0 && ` Skipped, no series: ${r.stations_skipped.join(", ")}.`}
          {r.citation && <><br />{r.citation}</>}
        </div>
      )}
    </div>
  );
}

function EntryView({ entry }: { entry: ValidationEntry | undefined }) {
  if (!entry || entry.status === "NOT_RUN") return null;
  if (entry.status === "RUNNING") return <div style={{ marginTop: 10 }}><LoadingDots label={entry.progress || "Working…"} /></div>;
  if (entry.status === "FAILED") return <div style={{ marginTop: 10, fontSize: 12, color: "var(--danger)" }}>{entry.error}</div>;
  return <ResultView entry={entry} />;
}

function errorText(error: unknown): string {
  return String((error as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? error);
}

/**
 * Does the result match an independent source? EGMS (a Copernicus product from the same satellite) or
 * GNSS stations. A MintPy series always comes out, correct or not, and this is what says which.
 */
export function ValidationPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [downloadId, setDownloadId] = useState("");
  const { data: status } = useQuery({
    queryKey: ["validation", projectId],
    queryFn: () => validationApi.status(projectId),
    refetchInterval: (query) =>
      query.state.data?.egms.status === "RUNNING" || query.state.data?.gnss.status === "RUNNING" ? 3_000 : false,
  });
  const { data: downloads } = useQuery({ queryKey: ["egms-downloads"], queryFn: egmsApi.listDownloads, staleTime: 30_000 });
  const usable = (downloads ?? []).filter((d) => d.level === "L2A" || d.level === "L2B");
  const chosen = downloadId || usable[0]?.id || "";

  const refresh = () => qc.invalidateQueries({ queryKey: ["validation", projectId] });
  const egms = useMutation({ mutationFn: () => validationApi.startEgms(projectId, chosen), onSuccess: refresh });
  const gnss = useMutation({ mutationFn: () => validationApi.startGnss(projectId), onSuccess: refresh });
  const running = (e?: ValidationEntry) => e?.status === "RUNNING";

  return (
    <details style={{ marginTop: 14 }}>
      <summary style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 13, fontWeight: 600 }}>
        Validate against EGMS or GNSS
        {(["egms", "gnss"] as const).map((kind) => {
          const entry = status?.[kind];
          return entry?.status === "SUCCEEDED" && entry.result && !entry.stale
            ? <StatusPill key={kind} status={entry.result.verdict.status} text={`${kind === "egms" ? "EGMS" : "GNSS"}: ${entry.result.verdict.status === "ok" ? "agrees" : entry.result.verdict.status === "warn" ? "check this" : "does not agree"}`} />
            : null;
        })}
      </summary>
      <div style={{ fontSize: 12, ...muted, marginTop: 6, lineHeight: 1.5 }}>
        Compares this result with a source that did not use HyP3 or MintPy, on the dates both have. EGMS must be an L2a or L2b
        download of the same track and direction as your interferograms. GNSS uses nearby stations of the Nevada Geodetic Laboratory
        and needs internet access.
      </div>

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
        <select aria-label="EGMS download" value={chosen} onChange={(e) => setDownloadId(e.target.value)}
                disabled={usable.length === 0}
                style={{ padding: "6px 8px", fontSize: 12, borderRadius: 8, color: "var(--text)", background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", maxWidth: 320 }}>
          {usable.length === 0 && <option value="">No EGMS L2a or L2b download yet</option>}
          {usable.map((d) => <option key={d.id} value={d.id}>{d.name} ({d.level}{d.direction ? `, ${d.direction}` : ""})</option>)}
        </select>
        <button className="btn btn-primary btn-sm" disabled={!chosen || egms.isPending || running(status?.egms)} onClick={() => egms.mutate()}>
          {running(status?.egms) ? "Comparing…" : "Validate against EGMS"}
        </button>
        <button className="btn btn-secondary btn-sm" disabled={gnss.isPending || running(status?.gnss)} onClick={() => gnss.mutate()}>
          {running(status?.gnss) ? "Comparing…" : "Validate against GNSS"}
        </button>
      </div>
      {usable.length === 0 && (
        <div style={{ fontSize: 11, ...muted, marginTop: 6 }}>Download EGMS L2a or L2b for this area on the Ground Motion page to enable the EGMS comparison.</div>
      )}
      {egms.isError && <div style={{ marginTop: 8, fontSize: 12, color: "var(--danger)" }}>{errorText(egms.error)}</div>}
      {gnss.isError && <div style={{ marginTop: 8, fontSize: 12, color: "var(--danger)" }}>{errorText(gnss.error)}</div>}

      {status?.egms.status !== "NOT_RUN" && <div style={{ marginTop: 14, fontSize: 12, fontWeight: 600 }}>EGMS</div>}
      <EntryView entry={status?.egms} />
      {status?.gnss.status !== "NOT_RUN" && <div style={{ marginTop: 14, fontSize: 12, fontWeight: 600 }}>GNSS</div>}
      <EntryView entry={status?.gnss} />
    </details>
  );
}
