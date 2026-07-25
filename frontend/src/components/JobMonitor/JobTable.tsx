import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DownloadProgress, Job, QueueState, downloadQueueApi, jobsApi } from "../../api/client";

const BADGE: Record<string, string> = {
  PENDING:   "badge-pending",
  RUNNING:   "badge-running",
  SUCCEEDED: "badge-succeeded",
  FAILED:    "badge-failed",
};

const REFETCH_INTERVAL = 15_000;

interface JobTableProps {
  projectId: string;
  batchId: string;
  queueState: QueueState | null;
}

export function JobTable({ projectId, batchId, queueState }: JobTableProps) {
  const qc = useQueryClient();
  const [countdown, setCountdown] = useState(REFETCH_INTERVAL / 1000);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const allCheckRef = useRef<HTMLInputElement>(null);

  const { data: jobs = [], dataUpdatedAt, isFetching } = useQuery({
    queryKey: ["jobs", batchId],
    queryFn: () => jobsApi.listJobs(projectId, batchId),
    refetchInterval: REFETCH_INTERVAL,
  });

  useEffect(() => {
    setCountdown(REFETCH_INTERVAL / 1000);
    const id = setInterval(() => setCountdown((c) => (c <= 1 ? REFETCH_INTERVAL / 1000 : c - 1)), 1000);
    return () => clearInterval(id);
  }, [dataUpdatedAt]);

  // Refresh downloaded status when a queue job completes
  const prevDone = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (queueState?.done !== undefined && prevDone.current !== undefined && queueState.done > prevDone.current) {
      qc.invalidateQueries({ queryKey: ["jobs", batchId] });
    }
    prevDone.current = queueState?.done;
  }, [queueState?.done]);

  const total     = jobs.length;
  const succeeded = jobs.filter((j) => j.status === "SUCCEEDED").length;
  const failed    = jobs.filter((j) => j.status === "FAILED").length;
  const running   = jobs.filter((j) => j.status === "RUNNING").length;
  const pending   = jobs.filter((j) => j.status === "PENDING").length;
  const terminal  = succeeded + failed;

  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : null;

  const succeededJobs      = jobs.filter((j) => j.status === "SUCCEEDED");
  const notDownloadedJobs  = succeededJobs.filter((j) => !j.downloaded);
  const allSucceeded       = succeededJobs.length > 0 && succeededJobs.every((j) => selectedIds.has(j.id));
  const someSelected       = selectedIds.size > 0;
  const someButNotAll      = someSelected && !allSucceeded && succeededJobs.some((j) => selectedIds.has(j.id));

  useEffect(() => {
    if (allCheckRef.current) allCheckRef.current.indeterminate = someButNotAll;
  }, [someButNotAll]);

  function toggleSelect(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    if (allSucceeded || someButNotAll) setSelectedIds(new Set());
    else setSelectedIds(new Set(succeededJobs.map((j) => j.id)));
  }

  async function handleQueueSelected() {
    const toQueue = succeededJobs
      .filter((j) => selectedIds.has(j.id) && j.hyp3_job_id)
      .map((j) => ({ job_id: j.id, hyp3_job_id: j.hyp3_job_id! }));
    if (!toQueue.length) return;

    const queueRunning = queueState?.active;
    if (queueRunning) {
      if (!confirm(`A download is already in progress. This will replace the pending queue with ${toQueue.length} new jobs. Continue?`)) return;
    }

    await downloadQueueApi.start(toQueue);
    qc.invalidateQueries({ queryKey: ["download-queue"] });
    setSelectedIds(new Set());
  }

  // Current job being downloaded in the queue (for row highlighting)
  const currentDownloadJobId = queueState?.current_job_id ?? null;

  return (
    <div>
      {/* Stats + refresh indicator */}
      <div style={{ display: "flex", gap: 16, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 10, fontSize: 12, flexWrap: "wrap" }}>
          {pending   > 0 && <Pill color="var(--text-muted)"  label={`${pending} pending`} />}
          {running   > 0 && <Pill color="var(--warning)"     label={`${running} running`} />}
          {succeeded > 0 && <Pill color="var(--success)"     label={`${succeeded} succeeded`} />}
          {failed    > 0 && <Pill color="var(--danger)"      label={`${failed} failed`} />}
          {total === 0   && <Pill color="var(--text-faint)"  label="No jobs" />}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: "var(--text-faint)" }}>
          {isFetching && <span style={{ color: "var(--primary)", fontWeight: 600 }}>Refreshing…</span>}
          {lastUpdated && !isFetching && <span>Updated {lastUpdated} · next in {countdown}s</span>}
          <span style={{ fontWeight: 700, color: "var(--text-muted)" }}>{terminal}/{total} done</span>
        </div>
      </div>

      {/* Completion bar */}
      <div style={{ height: 6, background: "var(--border)", borderRadius: 3, marginBottom: 14, overflow: "hidden" }}>
        <div style={{ display: "flex", height: "100%" }}>
          <div style={{
            width: `${total ? Math.round((succeeded / total) * 100) : 0}%`,
            background: "linear-gradient(180deg, rgba(255,255,255,0.2) 0%, rgba(255,255,255,0) 60%), linear-gradient(90deg, var(--primary), var(--success))",
            borderRadius: failed > 0 ? "3px 0 0 3px" : 3,
            transition: "width 0.6s ease",
          }} />
          {failed > 0 && (
            <div style={{
              width: `${Math.round((failed / total) * 100)}%`,
              background: "var(--danger)", opacity: 0.5,
              transition: "width 0.6s ease",
            }} />
          )}
        </div>
      </div>

      {/* Bulk action bar */}
      {succeededJobs.length > 0 && (
        <div style={{
          display: "flex", alignItems: "center", gap: 10, marginBottom: 10,
          padding: "8px 12px",
          background: someSelected ? "var(--primary-light)" : "var(--surface-2)",
          borderRadius: "var(--radius)", fontSize: 12, transition: "background 0.2s",
        }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontWeight: 500 }}>
            <input
              ref={allCheckRef}
              type="checkbox"
              checked={allSucceeded}
              onChange={toggleSelectAll}
              style={{ cursor: "pointer" }}
            />
            {allSucceeded
              ? `All ${succeededJobs.length} succeeded selected`
              : someSelected
              ? `${selectedIds.size} selected`
              : `Select all succeeded (${succeededJobs.length})`}
          </label>

          {/* Shortcut: select only not-yet-downloaded */}
          {notDownloadedJobs.length > 0 && !someSelected && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setSelectedIds(new Set(notDownloadedJobs.map((j) => j.id)))}
              title="Select only SUCCEEDED jobs that haven't been downloaded yet"
              style={{ fontSize: 11, borderStyle: "dashed" }}
            >
              Select not downloaded ({notDownloadedJobs.length})
            </button>
          )}

          {someSelected && (
            <>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleQueueSelected}
                style={{ marginLeft: 4 }}
              >
                ↓ Queue {selectedIds.size} for download
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setSelectedIds(new Set())}
                style={{ fontSize: 11 }}
              >
                Clear
              </button>
            </>
          )}
        </div>
      )}

      {/* Table */}
      <div className="card" style={{ overflow: "hidden" }}>
        <table className="table" style={{ fontSize: 12 }}>
          <thead>
            <tr>
              <th style={{ width: 32 }} />
              <th style={{ width: 90 }}>Ref date</th>
              <th style={{ width: 90 }}>Sec date</th>
              <th style={{ width: 100 }}>Status</th>
              <th>HyP3 Job ID</th>
              <th style={{ width: 60 }}>Credits</th>
              <th style={{ width: 80 }}>Downloaded</th>
              <th style={{ width: 80 }}></th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 && (
              <tr>
                <td colSpan={8} style={{ textAlign: "center", color: "var(--text-muted)", padding: 32 }}>
                  {isFetching ? "Loading…" : "No jobs found"}
                </td>
              </tr>
            )}
            {jobs.map((job) => {
              const isCurrentlyDownloading = job.id === currentDownloadJobId;
              const isInQueue = queueState?.pending_job_ids?.some((q) => typeof q === "object" ? q.job_id === job.id : q === job.id) ?? false;

              return (
                <JobRow
                  key={job.id}
                  job={job}
                  selected={selectedIds.has(job.id)}
                  isCurrentlyDownloading={isCurrentlyDownloading}
                  isInQueue={isInQueue}
                  currentProgress={isCurrentlyDownloading ? (queueState?.current_progress ?? null) : null}
                  onToggleSelect={() => toggleSelect(job.id)}
                  onDownloadSingle={
                    job.status === "SUCCEEDED" && job.hyp3_job_id
                      ? () => downloadQueueApi.start([{ job_id: job.id, hyp3_job_id: job.hyp3_job_id! }])
                          .then(() => qc.invalidateQueries({ queryKey: ["download-queue"] }))
                      : undefined
                  }
                />
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function JobRow({
  job, selected, isCurrentlyDownloading, isInQueue,
  currentProgress, onToggleSelect, onDownloadSingle,
}: {
  job: Job;
  selected: boolean;
  isCurrentlyDownloading: boolean;
  isInQueue: boolean;
  currentProgress: DownloadProgress | null;
  onToggleSelect: () => void;
  onDownloadSingle?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [logContent, setLogContent] = useState<Record<string, string>>({});
  const [logLoading, setLogLoading] = useState<Record<string, boolean>>({});

  const logUrls = extractUrls(job.error_message ?? "");
  const hasError = !!job.error_message;

  const rowBg = isCurrentlyDownloading
    ? "rgba(34,197,94,0.07)"
    : isInQueue
    ? "rgba(99,102,241,0.05)"
    : selected
    ? "var(--primary-light)"
    : undefined;

  async function fetchLog(url: string) {
    setLogLoading((prev) => ({ ...prev, [url]: true }));
    try {
      const res = await fetch(url);
      const text = await res.text();
      setLogContent((prev) => ({ ...prev, [url]: text }));
    } catch {
      setLogContent((prev) => ({ ...prev, [url]: "Failed to fetch log content." }));
    } finally {
      setLogLoading((prev) => ({ ...prev, [url]: false }));
    }
  }

  return (
    <>
      <tr
        style={{ cursor: hasError ? "pointer" : "default", background: rowBg }}
        onClick={() => hasError && setExpanded((e) => !e)}
        title={hasError ? "Click to see error details" : undefined}
      >
        <td style={{ textAlign: "center" }}>
          {job.status === "SUCCEEDED" && (
            <input
              type="checkbox"
              checked={selected}
              onChange={(e) => { e.stopPropagation(); onToggleSelect(); }}
              onClick={(e) => e.stopPropagation()}
              style={{ cursor: "pointer" }}
              disabled={isCurrentlyDownloading || isInQueue}
            />
          )}
        </td>
        <td style={{ fontFamily: "monospace" }}>{job.reference_date ?? "-"}</td>
        <td style={{ fontFamily: "monospace" }}>{job.secondary_date ?? "-"}</td>
        <td>
          <span
            className={`badge ${BADGE[job.status] ?? "badge-pending"}`}
            style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
          >
            {job.status === "RUNNING" && <SpinDot />}
            {job.status}
          </span>
        </td>
        <td style={{ fontFamily: "monospace", color: "var(--text-muted)", fontSize: 11 }}>
          {job.hyp3_job_id
            ? <span title={job.hyp3_job_id}>{job.hyp3_job_id.slice(0, 8)}…</span>
            : "-"}
        </td>
        <td style={{ color: "var(--text-muted)" }}>{job.credit_cost ?? "-"}</td>
        <td>
          {job.downloaded ? (
            <span style={{ color: "var(--success)", fontWeight: 600 }}>✓</span>
          ) : isInQueue ? (
            <span style={{ color: "var(--text-faint)", fontSize: 11 }}>queued</span>
          ) : (
            <span style={{ color: "var(--text-faint)" }}>-</span>
          )}
        </td>
        <td>
          {isCurrentlyDownloading ? (
            <span style={{ fontSize: 11, color: "#22c55e", fontWeight: 600 }}>↓ dl…</span>
          ) : isInQueue ? (
            <span style={{ fontSize: 11, color: "var(--text-faint)" }}>in queue</span>
          ) : job.status === "SUCCEEDED" && onDownloadSingle ? (
            <button
              className="btn btn-secondary btn-sm"
              onClick={(e) => { e.stopPropagation(); onDownloadSingle(); }}
              title={job.downloaded ? "Re-download" : "Download"}
            >
              {job.downloaded ? "↓ Re-dl" : "Download"}
            </button>
          ) : null}
          {hasError && (
            <span style={{ color: "var(--danger)", fontSize: 11, marginLeft: 4 }}>
              {expanded ? "▲" : "▼"} log
            </span>
          )}
        </td>
      </tr>

      {/* Inline progress bar for current download */}
      {isCurrentlyDownloading && currentProgress && currentProgress.status === "running" && (
        <tr>
          <td colSpan={8} style={{ padding: "4px 14px 8px" }}>
            <InlineProgressBar progress={currentProgress} />
          </td>
        </tr>
      )}

      {/* Error log */}
      {expanded && hasError && (
        <tr>
          <td colSpan={8} style={{ background: "var(--danger-light)", padding: "12px 16px" }}>
            {logUrls.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {logUrls.map((url) => (
                  <div key={url}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                      <a href={url} target="_blank" rel="noreferrer"
                        style={{ fontSize: 11, color: "var(--primary)", wordBreak: "break-all" }}
                        onClick={(e) => e.stopPropagation()}>
                        {url}
                      </a>
                      {!logContent[url] && (
                        <button
                          className="btn btn-ghost btn-sm"
                          style={{ flexShrink: 0, fontSize: 11 }}
                          disabled={logLoading[url]}
                          onClick={(e) => { e.stopPropagation(); fetchLog(url); }}
                        >
                          {logLoading[url] ? "Loading…" : "Load log"}
                        </button>
                      )}
                    </div>
                    {logContent[url] && (
                      <pre style={{
                        margin: 0, fontSize: 11, color: "var(--text)", background: "#1e293b",
                        padding: "10px 12px", borderRadius: "var(--radius)",
                        whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 400, overflowY: "auto",
                      }}>
                        {logContent[url]}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <pre style={{ margin: 0, fontSize: 11, color: "var(--danger)", whiteSpace: "pre-wrap" }}>
                {job.error_message}
              </pre>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function InlineProgressBar({ progress }: { progress: DownloadProgress }) {
  const pct = progress.pct ?? 0;
  return (
    <div style={{
      height: 14, background: "rgba(0,0,0,0.1)", borderRadius: 7, overflow: "hidden",
      border: "1px solid rgba(34,197,94,0.2)",
    }}>
      <div style={{
        height: "100%", width: `${pct}%`, borderRadius: 7, transition: "width 0.3s ease",
        background: [
          "linear-gradient(180deg, rgba(255,255,255,0.28) 0%, rgba(255,255,255,0) 55%)",
          "linear-gradient(90deg, #15803d, #22c55e)",
        ].join(", "),
        boxShadow: "0 0 6px rgba(34,197,94,0.4)",
        display: "flex", alignItems: "center", justifyContent: "flex-end",
        paddingRight: pct > 12 ? 5 : 0,
      }}>
        {pct > 8 && (
          <span style={{ fontSize: 9, fontWeight: 700, color: "rgba(255,255,255,0.9)" }}>
            {pct.toFixed(0)}%
          </span>
        )}
      </div>
    </div>
  );
}

const URL_RE = /https?:\/\/[^\s]+/g;
function extractUrls(text: string): string[] { return text.match(URL_RE) ?? []; }

function Pill({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontWeight: 600, color }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: color, display: "inline-block" }} />
      {label}
    </span>
  );
}

function SpinDot() {
  return (
    <span style={{
      width: 6, height: 6, borderRadius: "50%", background: "currentColor", display: "inline-block",
      animation: "pulse 1.2s ease-in-out infinite",
    }} />
  );
}
