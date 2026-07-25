import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  adminApi, downloadQueueApi, jobsApi, projectsApi,
  Batch, Project, QueueState,
} from "../api/client";
import { JobTable } from "../components/JobMonitor/JobTable";
import { NewProjectWizard } from "../components/ProjectWizard/NewProjectWizard";

export function DashboardPage() {
  const qc = useQueryClient();
  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [expandedBatchIds, setExpandedBatchIds] = useState<Set<string>>(new Set());
  const [creatingProject, setCreatingProject] = useState(false);

  const deleteMut = useMutation({
    mutationFn: projectsApi.delete,
    onSuccess: (_, deletedId) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      if (activeProjectId === deletedId) setActiveProjectId(null);
    },
  });

  const pollMut = useMutation({
    mutationFn: adminApi.poll,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });

  // Poll download queue state (active when queue is running)
  const { data: queueState } = useQuery({
    queryKey: ["download-queue"],
    queryFn: downloadQueueApi.get,
    refetchInterval: (data) => (data?.state?.data?.active ? 800 : 5000),
  });

  const cancelQueueMut = useMutation({
    mutationFn: downloadQueueApi.cancel,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["download-queue"] }),
  });

  const activeProject = projects?.find((p) => p.id === activeProjectId) ?? null;

  const { data: batches, refetch: refetchBatches } = useQuery({
    queryKey: ["batches", activeProjectId],
    queryFn: () => projectsApi.batches(activeProjectId!),
    enabled: !!activeProjectId,
  });

  useEffect(() => {
    if (batches?.length) {
      const newest = batches[batches.length - 1];
      setExpandedBatchIds((prev) => {
        if (prev.size === 0) return new Set([newest.id]);
        return prev;
      });
    }
  }, [batches]);

  const submitMut = useMutation({
    mutationFn: async (projectId: string) => {
      const plan = await jobsApi.plan(projectId, { dry_run: true, max_temporal_neighbors: activeProject?.max_temporal_neighbors ?? 3 });
      if (!confirm(`Submit ${plan.total_pairs} pairs to HyP3?\nThis will consume HyP3 processing credits.`)) throw new Error("cancelled");
      return jobsApi.submit(projectId, {
        dry_run: false,
        max_temporal_neighbors: activeProject?.max_temporal_neighbors ?? 3,
      });
    },
    onSuccess: (batch) => {
      refetchBatches();
      setExpandedBatchIds((prev) => new Set([...prev, batch.id]));
    },
  });

  // When queue finishes a job, refresh job list so downloaded status updates
  const prevQueueDone = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (queueState?.done !== undefined && prevQueueDone.current !== undefined && queueState.done > prevQueueDone.current) {
      qc.invalidateQueries({ queryKey: ["jobs"] });
    }
    prevQueueDone.current = queueState?.done;
  }, [queueState?.done]);

  function selectProject(p: Project) {
    setActiveProjectId(p.id);
    setExpandedBatchIds(new Set());
    setCreatingProject(false);
  }

  function toggleBatch(batchId: string) {
    setExpandedBatchIds((prev) => {
      const next = new Set(prev);
      if (next.has(batchId)) next.delete(batchId); else next.add(batchId);
      return next;
    });
  }

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>ASF</h2>
        <button
          className="btn btn-ghost btn-sm"
          disabled={pollMut.isPending}
          onClick={() => pollMut.mutate()}
          title="Force an immediate HyP3 status sync"
          style={{ marginLeft: "auto" }}
        >
          {pollMut.isPending ? "Syncing…" : "↻ Sync now"}
        </button>
      </div>
      <p style={{ margin: "0 0 16px", color: "var(--text-muted)", fontSize: 13 }}>
        Monitor and manage your InSAR processing batches.
        {pollMut.data && !pollMut.isPending && (
          <span style={{ marginLeft: 8, color: "var(--success)" }}>
            Synced: {pollMut.data.updated} jobs updated.
          </span>
        )}
      </p>

      {/* ── Download session banner ─────────────────────────────────────── */}
      {queueState && queueState.active && (
        <DownloadSessionBanner
          state={queueState}
          onCancel={() => cancelQueueMut.mutate()}
          cancelling={cancelQueueMut.isPending}
        />
      )}

      <div style={{ display: "grid", gridTemplateColumns: "220px 1fr", gap: 20, alignItems: "start" }}>

        {/* ── Project list ──────────────────────────────────────────────── */}
        <div className="card" style={{ padding: "12px 8px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 10px 10px" }}>
            <span style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--text-muted)" }}>
              Projects
            </span>
            <button
              className="btn btn-ghost btn-sm"
              style={{ fontSize: 11, padding: "2px 6px" }}
              onClick={() => { setCreatingProject(true); setActiveProjectId(null); }}
            >
              + New
            </button>
          </div>
          {!projects?.length && (
            <div style={{ padding: "12px 10px", fontSize: 13, color: "var(--text-muted)" }}>No projects yet.</div>
          )}
          {projects?.map((p) => (
            <div
              key={p.id}
              onClick={() => selectProject(p)}
              style={{
                display: "flex", alignItems: "center", gap: 4,
                padding: "8px 6px 8px 10px", borderRadius: 6,
                background: activeProjectId === p.id ? "var(--primary-light)" : "transparent",
                color: activeProjectId === p.id ? "var(--primary-text)" : "var(--text)",
                fontWeight: activeProjectId === p.id ? 600 : 400,
                fontFamily: "var(--font)", fontSize: 13, cursor: "pointer", marginBottom: 2,
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</div>
                <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 1 }}>Track {p.track_number}</div>
              </div>
              <button
                className="btn btn-ghost btn-sm"
                title="Delete project"
                style={{ fontSize: 11, padding: "2px 6px", flexShrink: 0, opacity: 0.6 }}
                onClick={(e) => {
                  e.stopPropagation();
                  if (confirm(`Delete "${p.name}"?`)) deleteMut.mutate(p.id);
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>

        {/* ── Right panel ───────────────────────────────────────────────── */}
        <div>
          {creatingProject && (
            <NewProjectWizard onDone={() => setCreatingProject(false)} />
          )}

          {!creatingProject && !activeProjectId && (
            <div className="card" style={{ padding: 48, textAlign: "center" }}>
              <div style={{ fontSize: 28, marginBottom: 10 }}>◈</div>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>Select a project</div>
              <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
                Choose a project on the left, or create a new one, to view its batches and job status.
              </div>
            </div>
          )}

          {!creatingProject && activeProject && (
            <>
              <div className="card" style={{ padding: "14px 18px", marginBottom: 16, display: "flex", alignItems: "center", gap: 16 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 700, fontSize: 15 }}>{activeProject.name}</div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                    Track {activeProject.track_number} · {activeProject.flight_direction} · {activeProject.date_start} → {activeProject.date_end}
                  </div>
                </div>
                <button
                  className="btn btn-primary"
                  disabled={submitMut.isPending}
                  onClick={() => submitMut.mutate(activeProjectId!)}
                >
                  {submitMut.isPending ? "Submitting…" : "+ Submit new batch"}
                </button>
              </div>

              {submitMut.isError && String(submitMut.error) !== "Error: cancelled" && (
                <div style={{ padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>
                  Submission failed: {String(submitMut.error)}
                </div>
              )}

              {!batches?.length && (
                <div className="card" style={{ padding: 32, textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
                  No batches submitted yet for this project.
                </div>
              )}

              {batches?.map((b) => (
                <BatchCard
                  key={b.id}
                  batch={b}
                  projectId={activeProjectId!}
                  expanded={expandedBatchIds.has(b.id)}
                  queueState={queueState ?? null}
                  onToggle={() => toggleBatch(b.id)}
                />
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Download Session Banner ──────────────────────────────────────────────────

function DownloadSessionBanner({
  state, onCancel, cancelling,
}: {
  state: QueueState;
  onCancel: () => void;
  cancelling: boolean;
}) {
  const p = state.current_progress;
  const pct = p?.pct ?? 0;
  const filename = p?.filename;
  const speed = p?.speed_bps;
  const eta = p?.eta_s;
  const db = p?.downloaded_bytes ?? 0;
  const tb = p?.total_bytes ?? 0;

  const label = [
    tb ? `${fmtBytes(db)} / ${fmtBytes(tb)}` : null,
    speed ? fmtSpeed(speed) : null,
    eta != null ? `~${fmtEta(eta)} left` : null,
  ].filter(Boolean).join(" · ");

  return (
    <div style={{
      marginBottom: 18,
      padding: "14px 18px",
      background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
      border: "1px solid rgba(34,197,94,0.3)",
      borderRadius: "var(--radius-lg)",
      boxShadow: "0 0 20px rgba(34,197,94,0.08)",
    }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <div style={{
          width: 8, height: 8, borderRadius: "50%",
          background: "#22c55e",
          boxShadow: "0 0 6px #22c55e",
          animation: "pulse 1.4s ease-in-out infinite",
          flexShrink: 0,
        }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "#f1f5f9" }}>
          Download session in progress
        </span>
        <span style={{
          marginLeft: "auto",
          fontSize: 12,
          color: "#94a3b8",
          background: "rgba(255,255,255,0.06)",
          padding: "2px 10px",
          borderRadius: 20,
        }}>
          {state.done} / {state.total} complete
        </span>
        <button
          className="btn btn-ghost btn-sm"
          disabled={cancelling}
          onClick={onCancel}
          style={{ fontSize: 11, color: "#f87171", borderColor: "rgba(248,113,113,0.3)" }}
        >
          {cancelling ? "Cancelling…" : "× Cancel remaining"}
        </button>
      </div>

      {/* Current file */}
      {filename && (
        <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 6, display: "flex", justifyContent: "space-between" }}>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "65%" }} title={filename}>
            {filename}
          </span>
          <span style={{ flexShrink: 0, marginLeft: 8, color: "#64748b" }}>{label}</span>
        </div>
      )}

      {/* Gloss green bar */}
      {p && (
        <div style={{
          height: 16, background: "rgba(255,255,255,0.05)",
          borderRadius: 8, overflow: "hidden",
          border: "1px solid rgba(34,197,94,0.2)",
          marginBottom: 10,
        }}>
          <div style={{
            height: "100%", width: `${pct}%`, borderRadius: 8,
            transition: "width 0.3s ease",
            background: [
              "linear-gradient(180deg, rgba(255,255,255,0.28) 0%, rgba(255,255,255,0) 55%)",
              "linear-gradient(90deg, #15803d, #22c55e)",
            ].join(", "),
            boxShadow: "0 0 10px rgba(34,197,94,0.5), inset 0 1px 0 rgba(255,255,255,0.15)",
            display: "flex", alignItems: "center", justifyContent: "flex-end",
            paddingRight: pct > 8 ? 6 : 0,
          }}>
            {pct > 5 && (
              <span style={{ fontSize: 10, fontWeight: 700, color: "rgba(255,255,255,0.9)" }}>
                {pct.toFixed(0)}%
              </span>
            )}
          </div>
        </div>
      )}

      {/* Queue summary */}
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: "#64748b" }}>
        {state.current_job_id && (
          <span style={{ color: "#22c55e" }}>
            Downloading 1 file
          </span>
        )}
        {state.pending_count > 0 && (
          <span>{state.pending_count} waiting in queue</span>
        )}
        {state.cancelled && (
          <span style={{ color: "#f87171" }}>Cancellation requested, finishing current file…</span>
        )}
      </div>
    </div>
  );
}

// ── BatchCard ────────────────────────────────────────────────────────────────

function BatchCard({
  batch, projectId, expanded, queueState, onToggle,
}: {
  batch: Batch;
  projectId: string;
  expanded: boolean;
  queueState: QueueState | null;
  onToggle: () => void;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div
        className="card"
        style={{
          padding: "12px 16px", cursor: "pointer",
          display: "flex", alignItems: "center", gap: 12,
          borderColor: expanded ? "#93c5fd" : "var(--border)",
          borderRadius: expanded ? "var(--radius-lg) var(--radius-lg) 0 0" : undefined,
        }}
        onClick={onToggle}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 500, fontSize: 13 }}>{batch.label || "Batch"}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
            {batch.total_pairs} pairs · submitted {new Date(batch.created_at).toLocaleString()}
          </div>
        </div>
        <span style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {expanded ? "▲ Hide" : "▼ Show jobs"}
        </span>
      </div>

      {expanded && (
        <div
          className="card"
          style={{
            borderTop: "none",
            borderRadius: "0 0 var(--radius-lg) var(--radius-lg)",
            padding: "16px 20px",
          }}
        >
          <JobTable
            projectId={projectId}
            batchId={batch.id}
            queueState={queueState}
          />
        </div>
      )}
    </div>
  );
}

function fmtBytes(b: number): string {
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(0)} MB`;
  return `${(b / 1e3).toFixed(0)} kB`;
}
function fmtSpeed(bps: number): string {
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} MB/s`;
  return `${(bps / 1e3).toFixed(0)} kB/s`;
}
function fmtEta(s: number): string {
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
