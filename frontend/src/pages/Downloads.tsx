import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  backupApi, egmsApi, projectsApi, storageApi,
  EGMSDownloadRecord, PathUsage, Project, StorageTarget,
} from "../api/client";
import { useDialogs } from "../components/Dialogs";

type Row =
  | { kind: "asf"; id: string; project: Project; path: string | null }
  | { kind: "egms"; id: string; download: EGMSDownloadRecord; path: string };

export function DownloadsPage({ onOpenViewer }: { onOpenViewer: (datasetId: string) => void }) {
  const qc = useQueryClient();
  const { ask, confirm, toast } = useDialogs();
  const [movingRowId, setMovingRowId] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<string>("");
  const [archivingRowId, setArchivingRowId] = useState<string | null>(null);
  const [archiveDestination, setArchiveDestination] = useState("");

  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const { data: egmsDownloads } = useQuery({ queryKey: ["egms-downloads"], queryFn: egmsApi.listDownloads });
  const { data: storageTargets } = useQuery({ queryKey: ["storage-targets"], queryFn: storageApi.targets });

  // Per-project job download counts (small N - one call each, cached by react-query)
  const summaries = useQueries({
    queries: (projects ?? []).map((p) => ({
      queryKey: ["project-download-summary", p.id],
      queryFn: () => projectsApi.downloadSummary(p.id),
      enabled: !!projects,
    })),
  });

  const rows: Row[] = useMemo(() => [
    ...(projects ?? []).map((project, i): Row => ({
      kind: "asf", id: project.id, project,
      path: summaries[i]?.data?.storage_path ?? null,
    })),
    ...(egmsDownloads ?? []).map((download): Row => ({
      kind: "egms", id: download.id, download, path: download.destination_path,
    })),
  ], [projects, egmsDownloads, summaries]);

  // Size-on-disk per row, fetched lazily once each path is known.
  const usageQueries = useQueries({
    queries: rows.map((row) => ({
      queryKey: ["storage-usage", row.path],
      queryFn: () => storageApi.usage(row.path!),
      enabled: !!row.path,
      staleTime: 30_000,
    })),
  });

  // Global move progress - only one move runs at a time backend-side.
  const { data: moveState } = useQuery({
    queryKey: ["storage-move"],
    queryFn: storageApi.moveState,
    refetchInterval: (q) => (q.state.data?.active ? 1500 : 6000),
  });

  // Archive / restore progress: one operation at a time backend-side.
  const { data: archiveState } = useQuery({
    queryKey: ["archive-state"],
    queryFn: projectsApi.archiveState,
    refetchInterval: (q) => (q.state.data?.active ? 1500 : 6000),
  });
  const { data: backupOverview } = useQuery({ queryKey: ["backup"], queryFn: backupApi.overview });

  const archiveMut = useMutation({
    mutationFn: ({ id, destination }: { id: string; destination: string }) => projectsApi.archive(id, destination),
    onSuccess: () => {
      setArchivingRowId(null);
      qc.invalidateQueries({ queryKey: ["archive-state"] });
    },
  });
  const restoreMut = useMutation({
    mutationFn: (id: string) => projectsApi.restore(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["archive-state"] }),
  });

  // When an operation finishes, refresh everything that depends on where the files are.
  const lastSeenStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (archiveState?.status && lastSeenStatus.current === "running" && archiveState.status !== "running") {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["storage-usage"] });
      qc.invalidateQueries({ queryKey: ["project-download-summary"] });
    }
    lastSeenStatus.current = archiveState?.status;
  }, [archiveState?.status]);

  const deleteEgmsMut = useMutation({
    mutationFn: ({ id, deleteFiles }: { id: string; deleteFiles: boolean }) => egmsApi.deleteDownload(id, deleteFiles),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["egms-downloads"] });
      qc.invalidateQueries({ queryKey: ["storage-usage"] });
    },
  });

  const deleteProjectDataMut = useMutation({
    mutationFn: projectsApi.deleteDownloadData,
    onError: (err) => toast({ title: "Could not delete the data", message: errorText(err), tone: "error" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mintpy-status"] });
      qc.invalidateQueries({ queryKey: ["project-download-summary"] });
      qc.invalidateQueries({ queryKey: ["storage-usage"] });
    },
  });

  const moveMut = useMutation({
    mutationFn: ({ row, mountpoint }: { row: Row; mountpoint: string | null }) =>
      row.kind === "asf"
        ? projectsApi.moveStorage(row.id, mountpoint)
        : egmsApi.moveDownload(row.id, mountpoint),
    onSuccess: () => {
      setMovingRowId(null);
      qc.invalidateQueries({ queryKey: ["storage-move"] });
    },
  });

  function startMove(row: Row) {
    const mountpoint = moveTarget || null;
    moveMut.mutate({ row, mountpoint });
  }

  const moveBusy = !!moveState?.active;

  return (
    <div className="page">
      <h2 style={{ marginBottom: 16 }}>Downloads</h2>

      <StorageOverview targets={storageTargets ?? []} />

      {moveState?.active && (
        <MoveBanner state={moveState} />
      )}
      {archiveState?.active && (
        <div style={{ marginBottom: 16, padding: "10px 14px", background: "var(--surface-2)", borderRadius: "var(--radius)", fontSize: 13 }}>
          {archiveState.kind === "restore" ? "Restoring" : "Archiving"} project: {archiveState.step ?? "working"} ({Math.round(archiveState.pct)}%).
          Local files are only deleted after the copy has been verified.
        </div>
      )}
      {archiveState?.status === "error" && !archiveState.active && (
        <div style={{ marginBottom: 16, padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13, whiteSpace: "pre-wrap" }}>
          Last {archiveState.kind} failed (local files were not deleted): {archiveState.error}
        </div>
      )}
      {moveState?.status === "error" && !moveState.active && (
        <div style={{ marginBottom: 16, padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13 }}>
          Last move failed: {moveState.error}
        </div>
      )}

      <div className="card" style={{ overflow: "hidden" }}>
        <table className="table" style={{ fontSize: 12 }}>
          <thead>
            <tr>
              <th style={{ width: 92 }}>Source</th>
              <th>Name / AOI</th>
              <th style={{ width: 130 }}>Detail</th>
              <th style={{ width: 90 }}>Downloaded</th>
              <th style={{ width: 80 }}>Size</th>
              <th>Path</th>
              <th style={{ width: 210 }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={7} style={{ textAlign: "center", color: "var(--text-muted)", padding: 32 }}>Nothing downloaded yet.</td></tr>
            )}
            {rows.map((row, i) => {
              const usage: PathUsage | undefined = usageQueries[i]?.data;
              const isMoving = movingRowId === row.id;

              if (row.kind === "asf") {
                const idx = (projects ?? []).findIndex((p) => p.id === row.project.id);
                const summary = summaries[idx]?.data;
                return (
                  <RowShell
                    key={row.id}
                    onView={() => onOpenViewer(row.id)}
                    badge={<SourceBadge source="asf" />}
                    name={row.project.name}
                    detail={`Track ${row.project.track_number} · ${row.project.flight_direction}`}
                    downloaded={summary ? `${summary.downloaded_jobs}/${summary.total_jobs}` : "…"}
                    usage={usage}
                    path={summary?.storage_path ?? "app default"}
                    isMoving={isMoving}
                    moveBusy={moveBusy}
                    storageTargets={storageTargets ?? []}
                    moveTarget={moveTarget}
                    onMoveTargetChange={setMoveTarget}
                    onStartMoveClick={() => { setMovingRowId(row.id); setMoveTarget(""); }}
                    onCancelMove={() => setMovingRowId(null)}
                    onConfirmMove={() => startMove(row)}
                    movePending={moveMut.isPending}
                    archive={{
                      archivedRemote: row.project.archived_remote ?? null,
                      busy: !!archiveState?.active,
                      isOpen: archivingRowId === row.id,
                      destination: archiveDestination,
                      onDestinationChange: setArchiveDestination,
                      onOpen: () => {
                        setArchivingRowId(row.id);
                        setArchiveDestination(backupOverview?.config.destination ?? "");
                      },
                      onCancel: () => setArchivingRowId(null),
                      onConfirm: () => archiveMut.mutate({ id: row.id, destination: archiveDestination.trim() }),
                      onRestore: () => restoreMut.mutate(row.id),
                      pending: archiveMut.isPending || restoreMut.isPending,
                      error: archiveMut.isError ? errorText(archiveMut.error) : restoreMut.isError ? errorText(restoreMut.error) : null,
                    }}
                    deleteLabel="🗑 Delete downloaded data"
                    deleteTitle="Deletes the HyP3 output files and the MintPy results for this project (does not touch raw SLC downloads or the batch/job history - jobs can be downloaded again only until they expire on HyP3)"
                    onDelete={async () => {
                      const ok = await confirm({
                        title: `Delete downloaded data for "${row.project.name}"?`,
                        message:
                          `This frees about ${usage?.used_gb ?? "?"} GB: the interferograms, the MintPy results and the processing record (provenance.json). The batch and job history stays.\n` +
                          `You can download again from HyP3 only until the results expire there. After that the pairs would have to be processed again, using credits. Raw SLC downloads are not affected.`,
                        confirmLabel: "Delete files",
                        danger: true,
                      });
                      if (ok) deleteProjectDataMut.mutate(row.id);
                    }}
                    deletePending={deleteProjectDataMut.isPending}
                  />
                );
              }

              const d = row.download;
              return (
                <RowShell
                  key={row.id}
                  onView={() => onOpenViewer(row.id)}
                  badge={<SourceBadge source="egms" />}
                  name={d.name}
                  detail={`${d.level} · ${d.release}`}
                  downloaded={`${d.filenames.length} file(s)`}
                  usage={usage}
                  path={d.destination_path}
                  isMoving={isMoving}
                  moveBusy={moveBusy}
                  storageTargets={storageTargets ?? []}
                  moveTarget={moveTarget}
                  onMoveTargetChange={setMoveTarget}
                  onStartMoveClick={() => { setMovingRowId(row.id); setMoveTarget(""); }}
                  onCancelMove={() => setMovingRowId(null)}
                  onConfirmMove={() => startMove(row)}
                  movePending={moveMut.isPending}
                  deleteLabel="✕ Remove"
                  deleteTitle="Remove from this list, or also delete the files"
                  onDelete={async () => {
                    const choice = await ask<"files" | "list" | "cancel">({
                      title: `Remove "${d.name}"?`,
                      message: `Removing it from the list keeps the files. Deleting the files frees about ${usage?.used_gb ?? "?"} GB and cannot be undone. EGMS products can be downloaded again.`,
                      dismissValue: "cancel",
                      buttons: [
                        { label: "Cancel", value: "cancel", variant: "ghost" },
                        { label: "Remove from list only", value: "list", variant: "primary" },
                        { label: "Delete files too", value: "files", variant: "danger" },
                      ],
                    });
                    if (choice === "cancel") return;
                    deleteEgmsMut.mutate({ id: d.id, deleteFiles: choice === "files" });
                  }}
                  deletePending={deleteEgmsMut.isPending}
                />
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Row ──────────────────────────────────────────────────────────────────────

interface ArchiveControls {
  archivedRemote: string | null;
  busy: boolean;
  isOpen: boolean;
  destination: string;
  onDestinationChange: (v: string) => void;
  onOpen: () => void;
  onCancel: () => void;
  onConfirm: () => void;
  onRestore: () => void;
  pending: boolean;
  error: string | null;
}

function errorText(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail ?? (err instanceof Error ? err.message : "Request failed");
}

function RowShell({
  onView, badge, name, detail, downloaded, usage, path,
  isMoving, moveBusy, storageTargets, moveTarget, onMoveTargetChange,
  onStartMoveClick, onCancelMove, onConfirmMove, movePending,
  deleteLabel, deleteTitle, onDelete, deletePending, archive,
}: {
  onView: () => void;
  badge: React.ReactNode;
  name: string;
  detail: string;
  downloaded: string;
  usage: PathUsage | undefined;
  path: string;
  isMoving: boolean;
  moveBusy: boolean;
  storageTargets: StorageTarget[];
  moveTarget: string;
  onMoveTargetChange: (v: string) => void;
  onStartMoveClick: () => void;
  onCancelMove: () => void;
  onConfirmMove: () => void;
  movePending: boolean;
  deleteLabel: string;
  deleteTitle: string;
  onDelete: () => void;
  deletePending: boolean;
  archive?: ArchiveControls;
}) {
  const archived = !!archive?.archivedRemote;
  return (
    <>
      <tr>
        <td>{badge}</td>
        <td style={{ fontWeight: 500 }}>{name}</td>
        <td style={{ color: "var(--text-muted)" }}>{detail}</td>
        <td>{downloaded}</td>
        <td style={{ color: "var(--text-muted)" }}>
          {usage ? `${usage.used_gb.toFixed(1)} GB` : "…"}
        </td>
        <td style={{ fontFamily: "monospace", fontSize: 11, color: "var(--text-muted)" }} title={archived ? archive!.archivedRemote! : path}>
          {archived ? `archived: ${archive!.archivedRemote}` : path}
        </td>
        <td>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <button
              className="btn btn-secondary btn-sm"
              style={{ fontSize: 11 }}
              title={archived ? "Restore the project to view it" : "Open this dataset in the viewer"}
              disabled={archived}
              onClick={onView}
            >
              View on map
            </button>
            <button
              className="btn btn-ghost btn-sm"
              style={{ fontSize: 11 }}
              disabled={moveBusy || archived}
              title="Move to a different disk"
              onClick={onStartMoveClick}
            >
              ↔ Move
            </button>
            {archive && !archived && (
              <button
                className="btn btn-ghost btn-sm"
                style={{ fontSize: 11 }}
                disabled={archive.busy || moveBusy}
                title="Copy to S3, a disk or another computer through rclone, verify it, then free the local disk"
                onClick={archive.onOpen}
              >
                ⇪ Archive
              </button>
            )}
            {archive && archived && (
              <button
                className="btn btn-secondary btn-sm"
                style={{ fontSize: 11 }}
                disabled={archive.busy || archive.pending}
                title="Copy the files back from the archive and verify them"
                onClick={archive.onRestore}
              >
                ⇩ Restore
              </button>
            )}
            <button
              className="btn btn-ghost btn-sm"
              style={{ fontSize: 11, color: "var(--danger)" }}
              disabled={deletePending || archived}
              title={deleteTitle}
              onClick={onDelete}
            >
              {deleteLabel}
            </button>
          </div>
        </td>
      </tr>
      {archive?.isOpen && (
        <tr>
          <td colSpan={7} style={{ background: "var(--surface-2)", padding: "10px 14px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12, flexWrap: "wrap" }}>
              <span style={{ color: "var(--text-muted)" }}>Archive to:</span>
              <input
                className="input"
                style={{ width: 380 }}
                value={archive.destination}
                onChange={(e) => archive.onDestinationChange(e.target.value)}
                placeholder="/mnt/archive   or   remotename:bucket/insar"
              />
              <button className="btn btn-primary btn-sm" disabled={archive.pending || !archive.destination.trim()} onClick={archive.onConfirm}>
                {archive.pending ? "Starting…" : "Archive"}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={archive.onCancel}>Cancel</button>
            </div>
            <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
              Everything in the project folder is copied, including MintPy results and SLC scenes. The local folder is deleted
              only after the copy verifies. An archived project cannot be viewed, processed or updated until it is restored.
            </div>
            {archive.error && <div style={{ marginTop: 6, fontSize: 12, color: "var(--danger)" }}>{archive.error}</div>}
          </td>
        </tr>
      )}
      {isMoving && (
        <tr>
          <td colSpan={7} style={{ background: "var(--surface-2)", padding: "10px 14px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12 }}>
              <span style={{ color: "var(--text-muted)" }}>Move to:</span>
              <select className="input" style={{ width: 320 }} value={moveTarget} onChange={(e) => onMoveTargetChange(e.target.value)}>
                {storageTargets.map((t) => (
                  <option key={t.mountpoint ?? "default"} value={t.mountpoint ?? ""} disabled={!t.writable}>
                    {t.mountpoint ? `${t.mountpoint} (${t.device})` : "App default"}
                    {" · "}{t.free_gb} GB free{!t.writable ? " · not writable" : ""}
                  </option>
                ))}
              </select>
              <button className="btn btn-primary btn-sm" disabled={movePending} onClick={onConfirmMove}>
                {movePending ? "Starting…" : "Move"}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={onCancelMove}>Cancel</button>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ── Storage overview ──────────────────────────────────────────────────────────

function StorageOverview({ targets }: { targets: StorageTarget[] }) {
  if (!targets.length) return null;
  return (
    <div className="card" style={{ padding: 16, marginBottom: 20 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.03em" }}>
        Disk space
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
        {targets.map((t) => {
          const usedGb = t.total_gb - t.free_gb;
          const pct = t.total_gb ? Math.round((usedGb / t.total_gb) * 100) : 0;
          return (
            <div key={t.mountpoint ?? "default"}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                <span style={{ fontWeight: 500 }}>{t.mountpoint ?? "App default"}</span>
                <span style={{ color: "var(--text-muted)" }}>{t.free_gb.toFixed(0)} GB free / {t.total_gb.toFixed(0)} GB</span>
              </div>
              <div style={{ height: 8, background: "var(--border)", borderRadius: 4, overflow: "hidden" }}>
                <div style={{
                  height: "100%", width: `${pct}%`, borderRadius: 4,
                  background: pct > 90 ? "var(--danger)" : pct > 75 ? "var(--warning)" : "var(--primary)",
                  transition: "width 0.4s ease",
                }} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MoveBanner({ state }: { state: { pct: number; src: string | null; dst: string | null } }) {
  return (
    <div style={{
      marginBottom: 18, padding: "14px 18px",
      background: "linear-gradient(135deg, rgba(var(--accent-rgb),0.12) 0%, rgba(255,255,255,0.03) 70%), #0b0f0e",
      border: "1px solid rgba(61,255,154,0.3)", borderRadius: "var(--radius-lg)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--accent)", boxShadow: "0 0 8px var(--accent)" }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "var(--text)" }}>Moving data…</span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-muted)" }}>{state.pct.toFixed(0)}%</span>
      </div>
      {state.src && state.dst && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
          {state.src} → {state.dst}
        </div>
      )}
      <div style={{ height: 10, background: "rgba(255,255,255,0.05)", borderRadius: 6, overflow: "hidden", border: "1px solid rgba(61,255,154,0.2)" }}>
        <div style={{
          height: "100%", width: `${state.pct}%`, borderRadius: 6, transition: "width 0.3s ease",
          background: "linear-gradient(90deg, var(--accent-2), var(--accent))",
        }} />
      </div>
    </div>
  );
}

function SourceBadge({ source }: { source: "asf" | "egms" }) {
  const style = source === "asf"
    ? { background: "var(--primary-light)", color: "var(--primary-text)", border: "1px solid var(--primary-border)" }
    : { background: "var(--warning-light)", color: "var(--warning)", border: "1px solid var(--warning-border)" };
  return (
    <span style={{ ...style, padding: "2px 8px", borderRadius: 9999, fontSize: 11, fontWeight: 600, whiteSpace: "nowrap" }}>
      {source === "asf" ? "ASF/HyP3" : "EGMS"}
    </span>
  );
}
