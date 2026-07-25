import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { egmsApi, projectsApi, EGMSDownloadRecord, Project } from "../api/client";
import { DownloadFootprint, DownloadsMap } from "../components/Map/DownloadsMap";

type Row =
  | { kind: "asf"; id: string; project: Project }
  | { kind: "egms"; id: string; download: EGMSDownloadRecord };

export function DownloadsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pointsForId, setPointsForId] = useState<string | null>(null);

  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const { data: egmsDownloads } = useQuery({ queryKey: ["egms-downloads"], queryFn: egmsApi.listDownloads });

  // Per-project job download counts (small N - one call each, cached by react-query)
  const summaries = useQueries({
    queries: (projects ?? []).map((p) => ({
      queryKey: ["project-download-summary", p.id],
      queryFn: () => projectsApi.downloadSummary(p.id),
      enabled: !!projects,
    })),
  });

  const pointsQuery = useQuery({
    queryKey: ["egms-points", pointsForId],
    queryFn: () => egmsApi.getPoints(pointsForId!),
    enabled: !!pointsForId,
  });

  const deleteEgmsMut = useMutation({
    mutationFn: egmsApi.deleteDownload,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["egms-downloads"] }),
  });

  const rows: Row[] = useMemo(() => [
    ...(projects ?? []).map((project): Row => ({ kind: "asf", id: project.id, project })),
    ...(egmsDownloads ?? []).map((download): Row => ({ kind: "egms", id: download.id, download })),
  ], [projects, egmsDownloads]);

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

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <h2 style={{ marginBottom: 4 }}>Downloads</h2>
      <p style={{ margin: "0 0 20px", color: "var(--text-muted)", fontSize: 13 }}>
        Everything downloaded so far, from both ASF/HyP3 projects and EGMS ground-motion products:
        what it is, where it lives on disk, and where it sits on the map.
      </p>

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
              <th>Path</th>
              <th style={{ width: 60 }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: "center", color: "var(--text-muted)", padding: 32 }}>Nothing downloaded yet.</td></tr>
            )}
            {rows.map((row) => {
              if (row.kind === "asf") {
                const idx = (projects ?? []).findIndex((p) => p.id === row.project.id);
                const summary = summaries[idx]?.data;
                return (
                  <tr
                    key={row.id}
                    onClick={() => setSelectedId(row.id)}
                    style={{ cursor: "pointer", background: selectedId === row.id ? "var(--primary-light)" : undefined }}
                  >
                    <td><SourceBadge source="asf" /></td>
                    <td style={{ fontWeight: 500 }}>{row.project.name}</td>
                    <td style={{ color: "var(--text-muted)" }}>
                      Track {row.project.track_number} · {row.project.flight_direction}
                    </td>
                    <td>
                      {summary ? `${summary.downloaded_jobs}/${summary.total_jobs}` : "…"}
                    </td>
                    <td style={{ fontFamily: "monospace", fontSize: 11, color: "var(--text-muted)" }} title={summary?.storage_path ?? undefined}>
                      {summary?.storage_path ?? "app default"}
                    </td>
                    <td />
                  </tr>
                );
              }
              const d = row.download;
              return (
                <tr
                  key={row.id}
                  onClick={() => setSelectedId(row.id)}
                  style={{ cursor: "pointer", background: selectedId === row.id ? "var(--primary-light)" : undefined }}
                >
                  <td><SourceBadge source="egms" /></td>
                  <td style={{ fontWeight: 500 }}>{d.name}</td>
                  <td style={{ color: "var(--text-muted)" }}>{d.level} · {d.release}</td>
                  <td>{d.filenames.length} file(s)</td>
                  <td style={{ fontFamily: "monospace", fontSize: 11, color: "var(--text-muted)" }} title={d.destination_path}>
                    {d.destination_path}
                  </td>
                  <td>
                    <button
                      className="btn btn-ghost btn-sm"
                      title="Remove from this list (does not delete the files)"
                      onClick={(e) => { e.stopPropagation(); deleteEgmsMut.mutate(d.id); }}
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
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
