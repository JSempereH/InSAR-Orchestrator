import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  adminApi, batchesApi, creditsApi, downloadQueueApi, jobsApi, projectsApi,
  Batch, Project, QueueState,
} from "../api/client";
import { JobTable } from "../components/JobMonitor/JobTable";
import { CreditsWidget } from "../components/JobMonitor/CreditsWidget";
import { MintPyPanel } from "../components/JobMonitor/MintPyPanel";
import { NewProjectWizard } from "../components/ProjectWizard/NewProjectWizard";
import { SLCPanel } from "../components/SLC/SLCPanel";
import { useDialogs } from "../components/Dialogs";
import { RefreshIcon } from "../components/icons";

export function DashboardPage({ onOpenViewer }: { onOpenViewer: (datasetId: string) => void }) {
  const qc = useQueryClient();
  const { confirm, toast } = useDialogs();
  const [highlightBatchId, setHighlightBatchId] = useState<string | null>(null);
  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [expandedBatchIds, setExpandedBatchIds] = useState<Set<string>>(new Set());
  const [creatingProject, setCreatingProject] = useState(false);
  const [autoDownload, setAutoDownload] = useState(false);

  const deleteMut = useMutation({
    mutationFn: projectsApi.delete,
    onSuccess: (_, deletedId) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      if (activeProjectId === deletedId) setActiveProjectId(null);
    },
  });

  const pollMut = useMutation({
    mutationFn: () => Promise.all([adminApi.poll(), creditsApi.refresh()]).then(([poll]) => poll),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["hyp3-credits"] });
    },
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
    // Poll quickly while a batch is still being submitted, so its status
    // flips to "done" (and jobs fill in) without the user refreshing.
    refetchInterval: (query) => (query.state.data?.some((b) => b.status === "submitting") ? 2000 : false),
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

  // A new batch is appended at the bottom of a long list, so bring it into view,
  // expand it and light it up for a few seconds.
  async function revealBatch(batchId: string) {
    await refetchBatches();
    setExpandedBatchIds((prev) => new Set([...prev, batchId]));
    setHighlightBatchId(batchId);
    window.setTimeout(() => {
      document.getElementById(`batch-${batchId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 80);
    window.setTimeout(() => setHighlightBatchId((cur) => (cur === batchId ? null : cur)), 6000);
  }

  const submitMut = useMutation({
    mutationFn: async (projectId: string) => {
      const plan = await jobsApi.plan(projectId, { dry_run: true, max_temporal_neighbors: activeProject?.max_temporal_neighbors ?? 3 });
      const sizeNote = plan.estimated_size_gb != null ? `\n≈ ${plan.estimated_size_gb} GB estimated once downloaded.` : "";
      const ok = await confirm({
        title: `Submit ${plan.total_pairs} pairs to HyP3?`,
        message: `This will consume HyP3 processing credits.${sizeNote}`,
        confirmLabel: "Submit batch",
      });
      if (!ok) throw new Error("cancelled");
      return jobsApi.submit(projectId, {
        dry_run: false,
        max_temporal_neighbors: activeProject?.max_temporal_neighbors ?? 3,
        auto_download: autoDownload,
      });
    },
    onSuccess: (batch) => {
      toast({
        title: "Batch created",
        message: `${batch.label ?? "New batch"}: ${batch.total_pairs} pairs are being sent to HyP3.`,
        actionLabel: "Show batch",
        onAction: () => revealBatch(batch.id),
      });
      revealBatch(batch.id);
    },
  });

  const refreshMut = useMutation({
    mutationFn: async (projectId: string) => {
      const plan = await projectsApi.refresh(projectId, { dry_run: true });
      if (plan.total_pairs === 0) {
        toast({
          tone: "info",
          title: "Already up to date",
          message: `No new Sentinel-1 pairs through ${plan.date_end}, so nothing was submitted.`,
        });
        throw new Error("cancelled");
      }
      const sizeNote = plan.estimated_size_gb != null ? `\n≈ ${plan.estimated_size_gb} GB estimated once downloaded.` : "";
      const ok = await confirm({
        title: `Update to ${plan.date_end}?`,
        message:
          `${plan.total_pairs} new pairs will be submitted to HyP3 as a new batch.\n` +
          `Existing pairs are not reprocessed. New results download automatically and MintPy runs once they are all on disk.\n` +
          `This will consume HyP3 processing credits.${sizeNote}`,
        confirmLabel: `Submit ${plan.total_pairs} pairs`,
      });
      if (!ok) throw new Error("cancelled");
      return projectsApi.refresh(projectId, { dry_run: false, run_mintpy: true });
    },
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      const batch = result.batch;
      if (batch) {
        toast({
          title: "Update started",
          message: `New batch "${batch.label}" with ${batch.total_pairs} pairs, up to ${result.date_end}. It appears at the bottom of the list.`,
          actionLabel: "Show batch",
          onAction: () => revealBatch(batch.id),
        });
        revealBatch(batch.id);
      } else {
        refetchBatches();
      }
    },
  });

  const renameMut = useMutation({
    mutationFn: ({ batchId, label }: { batchId: string; label: string }) => batchesApi.rename(batchId, label),
    onSuccess: () => refetchBatches(),
    onError: (err) => toast({ tone: "error", title: "Could not rename", message: String(err) }),
  });

  const autoDownloadMut = useMutation({
    mutationFn: ({ batchId, value }: { batchId: string; value: boolean }) =>
      batchesApi.setAutoDownload(batchId, value),
    onSuccess: () => refetchBatches(),
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
    <div className="page">
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>ASF</h2>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 16 }}>
          <CreditsWidget />
          <button
            className="btn btn-ghost btn-sm"
            disabled={pollMut.isPending}
            onClick={() => pollMut.mutate()}
            title="Force an immediate HyP3 status sync"
            aria-label="Sync now"
          >
            <RefreshIcon spinning={pollMut.isPending} />
          </button>
        </div>
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
                onClick={async (e) => {
                  e.stopPropagation();
                  const ok = await confirm({
                    title: `Delete "${p.name}"?`,
                    message: "The project, its batches and job history are removed from the app. Files on disk are not touched.",
                    confirmLabel: "Delete project",
                    danger: true,
                  });
                  if (ok) deleteMut.mutate(p.id);
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
                <label
                  title="Automatically queue each track for download as soon as HyP3 finishes processing it"
                  style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-muted)", cursor: "pointer" }}
                >
                  <input
                    type="checkbox"
                    checked={autoDownload}
                    onChange={(e) => setAutoDownload(e.target.checked)}
                    style={{ cursor: "pointer" }}
                  />
                  Auto-download when done
                </label>
                <button
                  className="btn"
                  title="Look for new Sentinel-1 acquisitions up to today and process only the new pairs"
                  disabled={refreshMut.isPending || submitMut.isPending}
                  onClick={() => refreshMut.mutate(activeProjectId!)}
                >
                  {refreshMut.isPending ? "Checking…" : "Update to today"}
                </button>
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

              {refreshMut.isError && String(refreshMut.error) !== "Error: cancelled" && (
                <div style={{ padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>
                  Update failed: {String(refreshMut.error)}
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
                  onAutoDownloadChange={(value) => autoDownloadMut.mutate({ batchId: b.id, value })}
                  onRename={(label) => renameMut.mutate({ batchId: b.id, label })}
                  highlight={highlightBatchId === b.id}
                />
              ))}

              <SLCPanel projectId={activeProjectId!} />

              {!!batches?.length && (
                <MintPyPanel
                  projectId={activeProjectId!}
                  onOpenViewer={onOpenViewer}
                />
              )}
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
      background: "linear-gradient(135deg, rgba(var(--accent-rgb),0.12) 0%, rgba(255,255,255,0.03) 70%), #0b0f0e",
      border: "1px solid rgba(61,255,154,0.3)",
      borderRadius: "var(--radius-lg)",
      boxShadow: "0 0 20px rgba(61,255,154,0.08)",
    }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <div style={{
          width: 8, height: 8, borderRadius: "50%",
          background: "var(--accent)",
          boxShadow: "0 0 8px var(--accent)",
          animation: "pulse 1.4s ease-in-out infinite",
          flexShrink: 0,
        }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "var(--text)" }}>
          Download session in progress
        </span>
        <span style={{
          marginLeft: "auto",
          fontSize: 12,
          color: "var(--text-muted)",
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
          style={{ fontSize: 11, color: "var(--danger)", borderColor: "rgba(248,113,113,0.3)" }}
        >
          {cancelling ? "Cancelling…" : "× Cancel remaining"}
        </button>
      </div>

      {/* Current file */}
      {filename && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, display: "flex", justifyContent: "space-between" }}>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "65%" }} title={filename}>
            {filename}
          </span>
          <span style={{ flexShrink: 0, marginLeft: 8, color: "var(--text-faint)" }}>{label}</span>
        </div>
      )}

      {/* Gloss green bar */}
      {p && (
        <div style={{
          height: 16, background: "rgba(255,255,255,0.05)",
          borderRadius: 8, overflow: "hidden",
          border: "1px solid rgba(61,255,154,0.2)",
          marginBottom: 10,
        }}>
          <div style={{
            height: "100%", width: `${pct}%`, borderRadius: 8,
            transition: "width 0.3s ease",
            background: [
              "linear-gradient(180deg, rgba(255,255,255,0.28) 0%, rgba(255,255,255,0) 55%)",
              "linear-gradient(90deg, var(--accent-2), var(--accent))",
            ].join(", "),
            boxShadow: "0 0 10px rgba(61,255,154,0.5), inset 0 1px 0 rgba(255,255,255,0.15)",
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
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--text-faint)" }}>
        {state.current_job_id && (
          <span style={{ color: "var(--accent)" }}>
            Downloading 1 file
          </span>
        )}
        {state.pending_count > 0 && (
          <span>{state.pending_count} waiting in queue</span>
        )}
        {state.cancelled && (
          <span style={{ color: "var(--danger)" }}>Cancellation requested, finishing current file…</span>
        )}
      </div>
    </div>
  );
}

// ── BatchCard ────────────────────────────────────────────────────────────────

function BatchCard({
  batch, projectId, expanded, queueState, onToggle, onAutoDownloadChange, onRename, highlight,
}: {
  batch: Batch;
  projectId: string;
  expanded: boolean;
  queueState: QueueState | null;
  onToggle: () => void;
  onAutoDownloadChange: (value: boolean) => void;
  onRename: (label: string) => void;
  highlight: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  function startEditing() {
    setDraft(batch.label ?? "");
    setEditing(true);
  }
  function commit() {
    const label = draft.trim();
    setEditing(false);
    if (label && label !== batch.label) onRename(label);
  }

  return (
    <div id={`batch-${batch.id}`} style={{ marginBottom: 12 }}>
      <div
        className="card"
        style={{
          padding: "12px 16px", cursor: "pointer",
          display: "flex", alignItems: "center", gap: 12,
          borderColor: expanded || highlight ? "var(--primary-border)" : "var(--border)",
          boxShadow: highlight ? "0 0 0 1px var(--accent), 0 0 28px rgba(61,255,154,0.35)" : undefined,
          transition: "box-shadow 0.4s",
          borderRadius: expanded ? "var(--radius-lg) var(--radius-lg) 0 0" : undefined,
        }}
        onClick={onToggle}
      >
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {editing ? (
              <input
                className="input"
                autoFocus
                value={draft}
                maxLength={80}
                style={{ height: 26, fontSize: 13, width: 240 }}
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commit}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commit();
                  if (e.key === "Escape") setEditing(false);
                }}
              />
            ) : (
              <>
                <span style={{ fontWeight: 500, fontSize: 13 }}>{batch.label || "Batch"}</span>
                <button
                  className="btn btn-ghost btn-sm"
                  title="Rename this batch"
                  aria-label="Rename batch"
                  style={{ padding: "0 6px", fontSize: 12, color: "var(--text-faint)" }}
                  onClick={(e) => { e.stopPropagation(); startEditing(); }}
                >
                  ✎
                </button>
              </>
            )}
            {batch.status === "submitting" && (
              <span style={{
                display: "inline-flex", alignItems: "center", gap: 5,
                fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.03em",
                color: "var(--warning)", background: "var(--warning-light)",
                padding: "2px 8px", borderRadius: 20,
              }}>
                <span style={{
                  width: 6, height: 6, borderRadius: "50%", background: "var(--warning)",
                  animation: "pulse 1.2s ease-in-out infinite",
                }} />
                Launching…
              </span>
            )}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
            {batch.total_pairs} pairs · submitted {new Date(batch.created_at).toLocaleString()}
          </div>
        </div>
        <label
          title="Automatically queue each track for download as soon as HyP3 finishes processing it"
          style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--text-muted)", cursor: "pointer" }}
          onClick={(e) => e.stopPropagation()}
        >
          <input
            type="checkbox"
            checked={batch.auto_download}
            onChange={(e) => onAutoDownloadChange(e.target.checked)}
            style={{ cursor: "pointer" }}
          />
          Auto-download
        </label>
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
            totalPairs={batch.total_pairs}
            submitting={batch.status === "submitting"}
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
