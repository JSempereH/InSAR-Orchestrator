import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  egmsApi, projectsApi, storageApi,
  EGMSDownloadRecord, PathUsage, Project, StorageTarget,
} from "../api/client";
import { DownloadFootprint, DownloadsMap } from "../components/Map/DownloadsMap";

type Row =
  | { kind: "asf"; id: string; project: Project; path: string | null }
  | { kind: "egms"; id: string; download: EGMSDownloadRecord; path: string };

export function DownloadsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pointsForId, setPointsForId] = useState<string | null>(null);
  const [movingRowId, setMovingRowId] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<string>("");

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

  const pointsQuery = useQuery({
    queryKey: ["egms-points", pointsForId],
    queryFn: () => egmsApi.getPoints(pointsForId!),
    enabled: !!pointsForId,
  });

  const deleteEgmsMut = useMutation({
    mutationFn: ({ id, deleteFiles }: { id: string; deleteFiles: boolean }) => egmsApi.deleteDownload(id, deleteFiles),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["egms-downloads"] });
      qc.invalidateQueries({ queryKey: ["storage-usage"] });
    },
  });

  const deleteProjectDataMut = useMutation({
    mutationFn: projectsApi.deleteDownloadData,
    onSuccess: () => {
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

  const footprints: DownloadFootprint[] = useMemo(() => [
    ...(projects ?? []).map((p): DownloadFootprint => ({
      id: p.id, source: "asf", label: p.name, geometry: p.geometry,
    })),
    ...(egmsDownloads ?? []).map((d): DownloadFootprint => ({
      id: d.id, source: "egms", label: d.name, geometry: d.geometry,
    })),
  ], [projects, egmsDownloads]);

  const selectedEgms = egmsDownloads?.find((d) => d.id === selectedId);
  const showingPointsForSelected = !!selectedEgms && pointsForId === selectedEgms.id;
  const moveBusy = !!moveState?.active;

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <h2 style={{ marginBottom: 4 }}>Downloads</h2>
      <p style={{ margin: "0 0 20px", color: "var(--text-muted)", fontSize: 13 }}>
        Everything downloaded so far, from both ASF/HyP3 projects and EGMS ground-motion products:
        what it is, how much space it uses, where it lives on disk, and where it sits on the map.
      </p>

      <StorageOverview targets={storageTargets ?? []} />

      {moveState?.active && (
        <MoveBanner state={moveState} />
      )}
      {moveState?.status === "error" && !moveState.active && (
        <div style={{ marginBottom: 16, padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13 }}>
          Last move failed: {moveState.error}
        </div>
      )}

      <div className="card" style={{ padding: 16, marginBottom: 20 }}>
        <DownloadsMap
          footprints={footprints}
          points={showingPointsForSelected ? (pointsQuery.data ?? null) : null}
          selectedId={selectedId}
          onSelect={setSelectedId}
          height={440}
        />
      </div>

      {selectedEgms && (
        <div className="card" style={{ padding: 16, marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{selectedEgms.name}</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {selectedEgms.level} · {selectedEgms.release} · {selectedEgms.filenames.length} file(s)
            </div>
          </div>
          {selectedEgms.level === "L3" ? (
            <button
              className="btn btn-primary btn-sm"
              disabled={pointsQuery.isFetching && pointsForId === selectedEgms.id}
              onClick={() => setPointsForId(showingPointsForSelected ? null : selectedEgms.id)}
            >
              {showingPointsForSelected ? "Hide velocity points" : pointsQuery.isFetching ? "Loading…" : "Show velocity points"}
            </button>
          ) : (
            <span style={{ fontSize: 12, color: "var(--text-faint)" }}>Point visualization available for L3 only</span>
          )}
        </div>
      )}

      {pointsQuery.isError && pointsForId === selectedEgms?.id && (
        <div style={{ marginBottom: 20, padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13 }}>
          Failed to parse points from the downloaded file. {String(pointsQuery.error)}
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
                    selected={selectedId === row.id}
                    onSelect={() => setSelectedId(row.id)}
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
                    deleteLabel="🗑 Delete downloaded interferograms"
                    deleteTitle="Deletes the HyP3 output files for this project (does not touch raw SLC downloads or the batch/job history - jobs can be re-downloaded later)"
                    onDelete={() => {
                      if (!confirm(`Delete downloaded interferogram files for "${row.project.name}"?\n\nThis frees ~${usage?.used_gb ?? "?"} GB but keeps the processing history - you can re-download from HyP3 later. Raw SLC downloads are not affected.`)) return;
                      deleteProjectDataMut.mutate(row.id);
                    }}
                    deletePending={deleteProjectDataMut.isPending}
                  />
                );
              }

              const d = row.download;
              return (
                <RowShell
                  key={row.id}
                  selected={selectedId === row.id}
                  onSelect={() => setSelectedId(row.id)}
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
                  onDelete={() => {
                    const alsoFiles = confirm(
                      `Delete "${d.name}"?\n\nOK = also delete the files on disk (~${usage?.used_gb ?? "?"} GB, cannot be undone)\nCancel = keep files, just remove from this list`
                    );
                    deleteEgmsMut.mutate({ id: d.id, deleteFiles: alsoFiles });
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

function RowShell({
  selected, onSelect, badge, name, detail, downloaded, usage, path,
  isMoving, moveBusy, storageTargets, moveTarget, onMoveTargetChange,
  onStartMoveClick, onCancelMove, onConfirmMove, movePending,
  deleteLabel, deleteTitle, onDelete, deletePending,
}: {
  selected: boolean;
  onSelect: () => void;
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
}) {
  return (
    <>
      <tr
        onClick={onSelect}
        style={{ cursor: "pointer", background: selected ? "var(--primary-light)" : undefined }}
      >
        <td>{badge}</td>
        <td style={{ fontWeight: 500 }}>{name}</td>
        <td style={{ color: "var(--text-muted)" }}>{detail}</td>
        <td>{downloaded}</td>
        <td style={{ color: "var(--text-muted)" }}>
          {usage ? `${usage.used_gb.toFixed(1)} GB` : "…"}
        </td>
        <td style={{ fontFamily: "monospace", fontSize: 11, color: "var(--text-muted)" }} title={path}>
          {path}
        </td>
        <td onClick={(e) => e.stopPropagation()}>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <button
              className="btn btn-ghost btn-sm"
              style={{ fontSize: 11 }}
              disabled={moveBusy}
              title="Move to a different disk"
              onClick={onStartMoveClick}
            >
              ↔ Move
            </button>
            <button
              className="btn btn-ghost btn-sm"
              style={{ fontSize: 11, color: "var(--danger)" }}
              disabled={deletePending}
              title={deleteTitle}
              onClick={onDelete}
            >
              {deleteLabel}
            </button>
          </div>
        </td>
      </tr>
      {isMoving && (
        <tr onClick={(e) => e.stopPropagation()}>
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
      background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
      border: "1px solid rgba(99,102,241,0.3)", borderRadius: "var(--radius-lg)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#818cf8", boxShadow: "0 0 6px #818cf8" }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "#f1f5f9" }}>Moving data…</span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "#94a3b8" }}>{state.pct.toFixed(0)}%</span>
      </div>
      {state.src && state.dst && (
        <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 8 }}>
          {state.src} → {state.dst}
        </div>
      )}
      <div style={{ height: 10, background: "rgba(255,255,255,0.05)", borderRadius: 6, overflow: "hidden", border: "1px solid rgba(99,102,241,0.2)" }}>
        <div style={{
          height: "100%", width: `${state.pct}%`, borderRadius: 6, transition: "width 0.3s ease",
          background: "linear-gradient(90deg, #4338ca, #818cf8)",
        }} />
      </div>
    </div>
  );
}

function SourceBadge({ source }: { source: "asf" | "egms" }) {
  const style = source === "asf"
    ? { background: "#eff6ff", color: "#1e40af", border: "1px solid #bfdbfe" }
    : { background: "#fffbeb", color: "#92400e", border: "1px solid #fed7aa" };
  return (
    <span style={{ ...style, padding: "2px 8px", borderRadius: 9999, fontSize: 11, fontWeight: 600, whiteSpace: "nowrap" }}>
      {source === "asf" ? "ASF/HyP3" : "EGMS"}
    </span>
  );
}
