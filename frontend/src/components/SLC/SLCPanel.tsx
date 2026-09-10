import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectsApi, storageApi, Scene, SLCDownload, SLCQueueState, slcApi } from "../../api/client";

interface SLCPanelProps {
  projectId: string;
}

/**
 * Raw Sentinel-1 SLC discovery + download for a project.
 *
 * Separate from batches/jobs: these are the original SLC products, not HyP3
 * output. They're the input an external PS-InSAR pipeline needs (this app
 * doesn't do coregistration / persistent-scatterer processing itself).
 */
export function SLCPanel({ projectId }: SLCPanelProps) {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const scenesQuery = useQuery({
    queryKey: ["slc-scenes", projectId],
    queryFn: () => slcApi.scenes(projectId),
    enabled: expanded,
  });

  const { data: queueState } = useQuery({
    queryKey: ["slc-download-queue"],
    queryFn: slcApi.getQueue,
    refetchInterval: (q) => (q.state.data?.active ? 1500 : 8000),
  });

  const { data: downloads, refetch: refetchDownloads } = useQuery({
    queryKey: ["slc-downloads", projectId],
    queryFn: () => slcApi.listDownloads(projectId),
    enabled: expanded,
  });

  const { data: summary } = useQuery({
    queryKey: ["project-download-summary", projectId],
    queryFn: () => projectsApi.downloadSummary(projectId),
    enabled: expanded,
  });

  const { data: diskUsage } = useQuery({
    queryKey: ["storage-usage", summary?.storage_path],
    queryFn: () => storageApi.usage(summary!.storage_path!),
    enabled: expanded && !!summary?.storage_path,
  });

  const scenes = scenesQuery.data ?? [];
  const downloadable = useMemo(() => scenes.filter((s) => !s.already_downloaded), [scenes]);
  const selectedScenes = useMemo(() => scenes.filter((s) => selected.has(s.granule_name)), [scenes, selected]);
  const totalSizeGb = selectedScenes.reduce((sum, s) => sum + (s.size_mb ?? 0), 0) / 1000;
  const notEnoughSpace = diskUsage != null && totalSizeGb > diskUsage.free_gb;

  const queueMut = useMutation({
    mutationFn: () => slcApi.queueDownload(projectId, selectedScenes),
    onSuccess: () => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["slc-download-queue"] });
      refetchDownloads();
      scenesQuery.refetch();
    },
  });

  const cancelMut = useMutation({
    mutationFn: slcApi.cancelQueue,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["slc-download-queue"] }),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => slcApi.deleteDownload(id, true),
    onSuccess: () => {
      refetchDownloads();
      scenesQuery.refetch();
      qc.invalidateQueries({ queryKey: ["storage-usage"] });
    },
  });

  function toggle(granuleName: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(granuleName)) next.delete(granuleName); else next.add(granuleName);
      return next;
    });
  }

  function selectAllDownloadable() {
    setSelected(new Set(downloadable.map((s) => s.granule_name)));
  }

  return (
    <div className="card" style={{ marginTop: 20 }}>
      <div
        style={{ padding: "12px 16px", cursor: "pointer", display: "flex", alignItems: "center", gap: 12 }}
        onClick={() => setExpanded((e) => !e)}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 500, fontSize: 13 }}>Raw SLC downloads</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
            Original Sentinel-1 SLC products for this project's AOI/track/dates - input for external PS-InSAR pipelines (not HyP3 processing).
          </div>
        </div>
        <span style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {expanded ? "▲ Hide" : "▼ Show"}
        </span>
      </div>

      {expanded && (
        <div style={{ padding: "0 16px 16px", borderTop: "1px solid var(--border)" }}>
          {queueState && queueState.active && (
            <SLCDownloadBanner
              state={queueState}
              onCancel={() => cancelMut.mutate()}
              cancelling={cancelMut.isPending}
            />
          )}

          <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "14px 0" }}>
            <button
              className="btn btn-secondary btn-sm"
              disabled={scenesQuery.isFetching}
              onClick={() => scenesQuery.refetch()}
            >
              {scenesQuery.isFetching ? "Searching…" : "↻ Search available SLCs"}
            </button>
            {scenesQuery.data && (
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {scenes.length} scenes · {downloadable.length} not yet downloaded
              </span>
            )}
          </div>

          {scenesQuery.isError && (
            <div style={{ padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>
              Search failed: {String(scenesQuery.error)}
            </div>
          )}

          {scenes.length > 0 && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                <button className="btn btn-ghost btn-sm" onClick={selectAllDownloadable} disabled={!downloadable.length}>
                  Select all not-downloaded ({downloadable.length})
                </button>
                {selected.size > 0 && (
                  <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set())}>Clear</button>
                )}
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 280, overflowY: "auto", marginBottom: 14 }}>
                {scenes.map((s) => (
                  <SceneRow key={s.granule_name} scene={s} checked={selected.has(s.granule_name)} onToggle={() => toggle(s.granule_name)} />
                ))}
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                <button
                  className="btn btn-primary"
                  disabled={selectedScenes.length === 0 || queueMut.isPending}
                  onClick={() => queueMut.mutate()}
                >
                  {queueMut.isPending ? "Queuing…" : `Download ${selectedScenes.length} selected`}
                </button>
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  {selectedScenes.length} selected · ~{totalSizeGb.toFixed(1)} GB
                  {diskUsage != null && ` · ${diskUsage.free_gb.toFixed(0)} GB free`}
                </span>
              </div>

              {notEnoughSpace && (
                <div style={{ marginTop: 10, padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13 }}>
                  ⚠ Not enough free space: selection needs ~{totalSizeGb.toFixed(1)} GB but only {diskUsage!.free_gb.toFixed(1)} GB is free on this disk.
                </div>
              )}

              {queueMut.isError && (
                <div style={{ marginTop: 10, color: "var(--danger)", fontSize: 13 }}>
                  Failed to queue download. {String(queueMut.error)}
                </div>
              )}
            </>
          )}

          {downloads && downloads.length > 0 && (
            <div style={{ marginTop: 18 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 6 }}>
                Download history
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {downloads.map((d) => (
                  <DownloadHistoryRow
                    key={d.id}
                    download={d}
                    onDelete={() => {
                      if (!confirm(`Delete ${d.filenames.length} SLC file(s) downloaded on ${new Date(d.created_at).toLocaleString()}?\n\nThis permanently removes them from disk.`)) return;
                      deleteMut.mutate(d.id);
                    }}
                    deleting={deleteMut.isPending}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DownloadHistoryRow({ download, onDelete, deleting }: { download: SLCDownload; onDelete: () => void; deleting: boolean }) {
  return (
    <div style={{ fontSize: 11, color: "var(--text-faint)", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {download.filenames.length} scene(s) → {download.destination_path}
      </span>
      <span style={{ flexShrink: 0, display: "flex", alignItems: "center", gap: 8 }}>
        {new Date(download.created_at).toLocaleString()}
        <button
          className="btn btn-ghost btn-sm"
          style={{ fontSize: 10, color: "var(--danger)" }}
          disabled={deleting}
          title="Delete these SLC files from disk"
          onClick={onDelete}
        >
          🗑 Delete
        </button>
      </span>
    </div>
  );
}

function SceneRow({ scene, checked, onToggle }: { scene: Scene; checked: boolean; onToggle: () => void }) {
  return (
    <label
      style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "6px 10px", borderRadius: "var(--radius)",
        border: "1px solid var(--border)", cursor: scene.already_downloaded ? "default" : "pointer",
        background: scene.already_downloaded ? "var(--surface-2)" : checked ? "var(--primary-light)" : "white",
        opacity: scene.already_downloaded ? 0.6 : 1,
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        disabled={scene.already_downloaded}
      />
      <span style={{ flex: 1, fontSize: 12, fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={scene.granule_name}>
        {scene.acquisition_date} · {scene.granule_name}
      </span>
      {scene.already_downloaded && (
        <span style={{ fontSize: 10, color: "var(--success)", fontWeight: 600 }}>✓ downloaded</span>
      )}
      <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
        {scene.size_mb ? `${(scene.size_mb / 1000).toFixed(1)} GB` : "-"}
      </span>
    </label>
  );
}

function SLCDownloadBanner({ state, onCancel, cancelling }: { state: SLCQueueState; onCancel: () => void; cancelling: boolean }) {
  const p = state.current_progress;
  const pct = p?.pct ?? 0;

  return (
    <div style={{
      marginTop: 14, padding: "14px 18px",
      background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
      border: "1px solid rgba(34,197,94,0.3)", borderRadius: "var(--radius-lg)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#22c55e", boxShadow: "0 0 6px #22c55e" }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "#f1f5f9" }}>Downloading SLCs</span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "#94a3b8", background: "rgba(255,255,255,0.06)", padding: "2px 10px", borderRadius: 20 }}>
          {state.done} / {state.total} complete
        </span>
        <button className="btn btn-ghost btn-sm" disabled={cancelling} onClick={onCancel} style={{ fontSize: 11, color: "#f87171", borderColor: "rgba(248,113,113,0.3)" }}>
          {cancelling ? "Cancelling…" : "× Cancel remaining"}
        </button>
      </div>

      {state.current_filename && (
        <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 4 }} title={state.current_filename}>
          {state.current_filename}
        </div>
      )}

      {state.destination && (
        <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6 }}>
          💾 {state.destination}
        </div>
      )}

      <div style={{ height: 14, background: "rgba(255,255,255,0.05)", borderRadius: 8, overflow: "hidden", border: "1px solid rgba(34,197,94,0.2)" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 8, transition: "width 0.3s ease",
          background: "linear-gradient(90deg, #15803d, #22c55e)",
        }} />
      </div>
    </div>
  );
}
