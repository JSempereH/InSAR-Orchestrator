import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { nisarApi, NisarStack } from "../api/client";
import { LoadingDots } from "./LoadingDots";
import { StatusPill } from "./JobMonitor/status";

const muted = { color: "var(--text-muted)" } as const;
const mono = { fontFamily: "var(--font-mono)", fontSize: 11 } as const;

const gb = (bytes: number | null) => (bytes == null ? "?" : (bytes / 1e9).toFixed(2));
const label = (s: NisarStack) =>
  `Track ${s.track_number}, frame ${s.frame}, ${s.flight_direction.toLowerCase()}, ${s.polarization}, ${s.range_bandwidth} MHz, ${s.frame_coverage.toLowerCase()} frame (${s.granules.length} interferograms)`;

/**
 * NISAR GUNW interferograms over the project's area. NASA already produced them, so there is nothing to
 * submit and nothing to pay: pick one stack (one track, frame and polarization), download it, and MintPy
 * reads it from the card below.
 */
export function NisarPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [chosen, setChosen] = useState(0);
  const { data: stacks, isFetching, error, refetch } = useQuery({
    queryKey: ["nisar-stacks", projectId],
    queryFn: () => nisarApi.stacks(projectId),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const { data: queue } = useQuery({
    queryKey: ["nisar-queue"],
    queryFn: nisarApi.queue,
    refetchInterval: (q) => (q.state.data?.active ? 1_000 : 5_000),
  });

  // when the downloads finish, look again so the table shows what is on disk
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && queue && !queue.active) {
      refetch();
      qc.invalidateQueries({ queryKey: ["mintpy-status", projectId] });
    }
    wasActive.current = !!queue?.active;
  }, [queue?.active]);  // eslint-disable-line react-hooks/exhaustive-deps

  const stack = stacks?.[Math.min(chosen, (stacks?.length ?? 1) - 1)];
  const missing = stack?.granules.filter((g) => !g.downloaded) ?? [];
  const missingBytes = missing.reduce((sum, g) => sum + (g.size_bytes ?? 0), 0);

  const download = useMutation({
    mutationFn: () => nisarApi.download(projectId, missing.map((g) => g.file_name)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nisar-queue"] }),
  });
  const cancel = useMutation({ mutationFn: nisarApi.cancel, onSuccess: () => qc.invalidateQueries({ queryKey: ["nisar-queue"] }) });
  const problem = (e: unknown) => String((e as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? e);

  return (
    <div className="card" style={{ padding: "14px 18px", marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>NISAR interferograms</div>
          <div style={{ fontSize: 12, ...muted, marginTop: 2, lineHeight: 1.5 }}>
            NASA's ready-made L-band interferograms. They cost no credits, and the pairs are the ones NASA produced.
            Downloading needs your Earthdata login (Settings). Each file is about 2 GB.
          </div>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => refetch()} disabled={isFetching}>
          {isFetching ? "Searching…" : "Search again"}
        </button>
      </div>

      {isFetching && !stacks && <div style={{ marginTop: 12 }}><LoadingDots label="Searching the ASF archive…" /></div>}
      {error && <div style={{ marginTop: 10, fontSize: 12, color: "var(--danger)" }}>{problem(error)}</div>}
      {stacks && stacks.length === 0 && (
        <div style={{ marginTop: 12, fontSize: 13, ...muted }}>
          No NISAR GUNW product covers this area in these dates yet. NISAR public data starts in mid 2026 and covers each
          place every 12 days.
        </div>
      )}

      {stack && stacks && (
        <>
          <div style={{ marginTop: 12 }}>
            <label htmlFor="nisar-stack" style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 3 }}>Stack to use</label>
            <select id="nisar-stack" value={chosen} onChange={(e) => setChosen(Number(e.target.value))}
                    style={{ width: "100%", padding: "6px 8px", fontSize: 12, borderRadius: 8, color: "var(--text)", background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)" }}>
              {stacks.map((s, i) => <option key={i} value={i}>{label(s)}</option>)}
            </select>
            <div style={{ fontSize: 11, ...muted, marginTop: 4, lineHeight: 1.4 }}>
              One track, frame, polarization and bandwidth form a stack. A time series needs one stack. Full frames cover all of your area; partial ones may not.
            </div>
          </div>

          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
            <StatusPill status={stack.connected ? "ok" : "fail"} text={stack.connected ? "Pairs chain together" : "Pairs do not connect"} />
            {stack.frame_coverage.toUpperCase() !== "FULL" && <StatusPill status="warn" text="Partial frame" />}
            <span style={{ fontSize: 12, ...muted }}>{stack.dates.length} dates, {stack.dates[0]} to {stack.dates[stack.dates.length - 1]}</span>
          </div>
          {!stack.connected && (
            <div style={{ marginTop: 6, fontSize: 12, color: "var(--danger)" }}>
              Some dates are not tied to the others by any interferogram, so they cannot form one series.
            </div>
          )}

          <div style={{ maxHeight: 260, overflowY: "auto", marginTop: 10, border: "1px solid var(--border)", borderRadius: 10 }}>
            <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 11 }}>
              <thead>
                <tr style={{ ...muted, textAlign: "left", position: "sticky", top: 0, background: "var(--surface)" }}>
                  <th style={{ padding: "6px 10px", fontWeight: 600 }}>Reference</th><th style={{ fontWeight: 600 }}>Secondary</th>
                  <th style={{ fontWeight: 600 }}>Days</th><th style={{ fontWeight: 600 }}>GB</th><th style={{ fontWeight: 600 }}>Processed</th><th style={{ fontWeight: 600 }}>On disk</th>
                </tr>
              </thead>
              <tbody>
                {stack.granules.map((g) => (
                  <tr key={g.file_name} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "5px 10px", ...mono }}>{g.reference_date}</td><td style={mono}>{g.secondary_date}</td>
                    <td style={mono}>{g.temporal_days}</td><td style={mono}>{gb(g.size_bytes)}</td><td style={mono}>{g.processing_date}</td>
                    <td style={{ ...mono, color: g.downloaded ? "var(--success)" : "var(--text-faint)" }}>{g.downloaded ? "✓" : "not yet"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
            {queue?.active ? (
              <button className="btn btn-ghost btn-sm" onClick={() => cancel.mutate()} disabled={cancel.isPending}>Cancel the remaining downloads</button>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={missing.length === 0 || download.isPending} onClick={() => download.mutate()}>
                {missing.length === 0 ? "All downloaded" : `Download ${missing.length} missing (${(missingBytes / 1e9).toFixed(1)} GB)`}
              </button>
            )}
            {download.isError && <span style={{ fontSize: 12, color: "var(--danger)" }}>{problem(download.error)}</span>}
          </div>

          {queue?.active && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 11, ...muted, display: "flex", justifyContent: "space-between" }}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "70%" }}>{queue.current_filename}</span>
                <span>{queue.done} of {queue.total} done{queue.progress?.pct != null ? ` · ${queue.progress.pct.toFixed(0)}%` : ""}</span>
              </div>
              <div style={{ height: 10, borderRadius: 6, background: "rgba(255,255,255,0.05)", border: "1px solid rgba(61,255,154,0.2)", overflow: "hidden", marginTop: 4 }}>
                <div style={{ height: "100%", width: `${queue.progress?.pct ?? 0}%`, background: "linear-gradient(90deg, var(--accent-2), var(--accent))", boxShadow: "0 0 8px rgba(61,255,154,0.5)", transition: "width 0.3s" }} />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
