import { useQueries, useQuery } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { useEffect, useMemo, useRef, useState } from "react";
import { egmsApi, mintpyApi, projectsApi, MintPyStatus } from "../api/client";
import { LoadingDots } from "../components/LoadingDots";
import { DownloadFootprint, DownloadsMap } from "../components/Map/DownloadsMap";
import { DetachedWindow } from "../components/TimeSeries/DetachedWindow";
import { FloatingPanel } from "../components/TimeSeries/FloatingPanel";
import { DEFAULT_PANEL, type PanelConfig } from "../components/TimeSeries/panelConfig";
import { TimeSeriesAnalysisView } from "../components/TimeSeries/TimeSeriesAnalysisView";
import { usePersistentState } from "../components/TimeSeries/usePersistentState";

// Time series are only stored for EGMS L2A/L2B products (L3 is velocity-only).
const TIME_SERIES_LEVELS = new Set(["L2A", "L2B"]);

type Dataset = {
  kind: "mintpy" | "egms";
  id: string;
  name: string;
  detail: string;
  geometry: GeoJSON.Geometry;
  ready: boolean;
  level?: string;
};

type Picked = { lat: number; lon: number; pointId?: string };

interface ViewerPageProps {
  initialId?: string | null;
  onGoTo: (page: "dashboard" | "ground-motion") => void;
}

interface ViewSettings {
  aoiOpacity: number;
  pointScale: number;
  detached: boolean;
}
const DEFAULT_VIEW: ViewSettings = { aoiOpacity: 1, pointScale: 1, detached: false };

// Percentage of the track to paint in accent green (read by the slider CSS through --fill).
const fillOf = (value: number, min: number, max: number): React.CSSProperties =>
  ({ "--fill": `${((value - min) / (max - min)) * 100}%` }) as React.CSSProperties;

function errorText(error: unknown): string {
  return String((error as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? error);
}

export function ViewerPage({ initialId = null, onGoTo }: ViewerPageProps) {
  const [selectedId, setSelectedId] = useState<string | null>(initialId);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [latInput, setLatInput] = useState("");
  const [lonInput, setLonInput] = useState("");

  // Visual preferences and the time series window keep their settings between points and visits.
  const [view, setView] = usePersistentState<ViewSettings>("viewer.view", DEFAULT_VIEW);
  const [panel, setPanel] = usePersistentState<PanelConfig>("viewer.tsPanel", DEFAULT_PANEL);
  const [panelOpen, setPanelOpen] = useState(false);

  const stageRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);

  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const { data: egmsDownloads } = useQuery({ queryKey: ["egms-downloads"], queryFn: egmsApi.listDownloads });

  // Same query key as the MintPy panel on the ASF page, so the cache is shared.
  const statuses = useQueries({
    queries: (projects ?? []).map((p) => ({
      queryKey: ["mintpy-status", p.id],
      queryFn: () => mintpyApi.status(p.id),
      staleTime: 10_000,
    })),
  });
  const readyKey = statuses.map((s) => (s.data as MintPyStatus | undefined)?.status ?? "-").join(",");

  const datasets: Dataset[] = useMemo(() => {
    const ready = readyKey.split(",");
    const fromProjects = (projects ?? []).map((p, i): Dataset => ({
      kind: "mintpy", id: p.id, name: p.name, geometry: p.geometry,
      detail: `HyP3 + MintPy · Track ${p.track_number} ${p.flight_direction?.toLowerCase() ?? ""}`.trim(),
      ready: ready[i] === "SUCCEEDED",
    }));
    const fromEgms = (egmsDownloads ?? []).map((d): Dataset => ({
      kind: "egms", id: d.id, name: d.name, geometry: d.geometry, level: d.level,
      detail: `EGMS ${d.level} · ${d.release}`, ready: true,
    }));
    return [...fromProjects, ...fromEgms];
  }, [projects, egmsDownloads, readyKey]);

  const selected = datasets.find((d) => d.id === selectedId) ?? null;
  const canLoad = !!selected && selected.ready;

  const pointsQuery = useQuery({
    queryKey: ["viewer-points", selected?.kind, selected?.id],
    queryFn: () => (selected!.kind === "egms" ? egmsApi.getPoints(selected!.id) : mintpyApi.getPoints(selected!.id)),
    enabled: canLoad,
  });

  const hasSeries = selected?.kind === "mintpy" || (!!selected?.level && TIME_SERIES_LEVELS.has(selected.level));
  const seriesQuery = useQuery({
    queryKey: ["viewer-series", selected?.kind, selected?.id, picked?.pointId, picked?.lat, picked?.lon],
    queryFn: () =>
      selected!.kind === "egms"
        ? egmsApi.getTimeSeries(selected!.id, picked!.pointId!)
        : mintpyApi.getTimeSeries(selected!.id, picked!.lat, picked!.lon),
    enabled: canLoad && hasSeries && !!picked && (selected!.kind === "mintpy" || !!picked.pointId),
  });

  const footprints: DownloadFootprint[] = useMemo(
    () => datasets.map((d) => ({ id: d.id, source: d.kind === "egms" ? "egms" : "asf", label: d.name, geometry: d.geometry })),
    [datasets],
  );

  // Follow the browser's fullscreen state (also changes when the user presses Esc).
  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === stageRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  function toggleFullscreen() {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void stageRef.current?.requestFullscreen?.();
  }

  function select(id: string | null) {
    setSelectedId(id);
    setPicked(null);
    setPanelOpen(false);
    setLatInput("");
    setLonInput("");
  }

  function pick(next: Picked) {
    setPicked(next);
    setPanelOpen(true); // every new point opens the window with its saved size, position and mode
  }

  function onPointClick(feature: GeoJSON.Feature) {
    if (feature.geometry.type !== "Point") return;
    const [lon, lat] = feature.geometry.coordinates;
    setLatInput(lat.toFixed(6));
    setLonInput(lon.toFixed(6));
    pick({ lat, lon, pointId: feature.properties?.point_id });
  }

  const loading = canLoad && pointsQuery.isFetching;
  const panelTitle = picked
    ? picked.pointId ? `Point ${picked.pointId}` : `Point (${picked.lat.toFixed(5)}, ${picked.lon.toFixed(5)})`
    : "Time series";

  const seriesContent = !hasSeries ? (
    <div style={{ fontSize: 13, color: "var(--warning)" }}>
      Time series is only available for L2A/L2B downloads. This one is {selected?.level}, which only has velocity.
    </div>
  ) : seriesQuery.isError ? (
    <div style={{ fontSize: 12, color: "var(--danger)" }}>{errorText(seriesQuery.error)}</div>
  ) : seriesQuery.data ? (
    <TimeSeriesAnalysisView series={seriesQuery.data} />
  ) : (
    <LoadingDots label="Reading time series…" />
  );

  const showPanel = panelOpen && !!picked && canLoad;

  return (
    <div className="page">
      <h2 style={{ marginBottom: 4 }}>Viewer</h2>
      <p style={{ margin: "0 0 18px", color: "var(--text-muted)", fontSize: 13 }}>
        Explore ground motion from your processed projects and EGMS downloads. Pick a dataset, then click a point to analyse its displacement over time.
      </p>

      <div style={{ display: "flex", gap: 18, alignItems: "flex-start", flexWrap: "wrap" }}>
        {/* Dataset picker */}
        <div className="card" style={{ width: 290, flexShrink: 0, padding: 14 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 10 }}>
            Datasets
          </div>
          {datasets.length === 0 && (
            <div style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.5 }}>
              Nothing to show yet. Create a project in <b>ASF</b> and run MintPy, or download a product in <b>EGMS</b>.
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {datasets.map((d) => {
              const active = d.id === selectedId;
              return (
                <button
                  key={d.id}
                  onClick={() => select(d.id)}
                  style={{
                    textAlign: "left", cursor: "pointer", padding: "10px 12px", borderRadius: "var(--radius)",
                    border: `1px solid ${active ? "var(--primary-border)" : "var(--border)"}`,
                    background: active ? "var(--primary-light)" : "var(--panel)",
                    boxShadow: active ? "0 0 18px rgba(61,255,154,0.12)" : "none",
                    color: "var(--text)", fontFamily: "var(--font)", opacity: d.ready ? 1 : 0.65,
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                      background: d.kind === "egms" ? "var(--warning)" : "var(--accent)",
                      boxShadow: `0 0 8px ${d.kind === "egms" ? "var(--warning)" : "var(--accent)"}`,
                    }} />
                    <span style={{ fontWeight: 600, fontSize: 13, color: active ? "var(--primary-text)" : "var(--text)" }}>{d.name}</span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3, marginLeft: 16 }}>{d.detail}</div>
                  {!d.ready && (
                    <div style={{ fontSize: 11, color: "var(--warning)", marginTop: 3, marginLeft: 16 }}>No MintPy results yet</div>
                  )}
                </button>
              );
            })}
          </div>
        </div>

        {/* Map stage: also the fullscreen element, so every control and the time series window go with it */}
        <div style={{ flex: 1, minWidth: 340, display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card" style={{ padding: fullscreen ? 0 : 14 }}>
            <div
              ref={stageRef}
              style={{
                position: "relative", height: fullscreen ? "100vh" : 580, background: "var(--bg)",
                borderRadius: fullscreen ? 0 : "var(--radius-lg)", overflow: "hidden",
              }}
            >
              <DownloadsMap
                footprints={footprints}
                points={canLoad ? (pointsQuery.data ?? null) : null}
                selectedId={selectedId}
                onSelect={(id) => { if (id && id !== selectedId) select(id); }}
                onPointClick={onPointClick}
                height="100%"
                aoiOpacity={view.aoiOpacity}
                pointScale={view.pointScale}
                highlight={picked ? [picked.lon, picked.lat] : null}
              />

              {/* Appearance controls */}
              <div style={controlBar}>
                <button style={barButton} onClick={toggleFullscreen} aria-label={fullscreen ? "Exit full screen" : "Full screen"} title={fullscreen ? "Exit full screen (Esc)" : "Full screen"}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d={fullscreen ? "M9 4v5H4m16 0h-5V4M4 15h5v5m11-5h-5v5" : "M4 9V4h5m6 0h5v5M4 15v5h5m11-5v5h-5"} />
                  </svg>
                </button>
                {fullscreen && (
                  <select
                    className="input" aria-label="Dataset" value={selectedId ?? ""} style={{ width: 190, padding: "4px 8px", fontSize: 12 }}
                    onChange={(e) => select(e.target.value || null)}
                  >
                    <option value="">Select a dataset…</option>
                    {datasets.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                  </select>
                )}
                <label style={sliderLabel} title="Opacity of the project and download areas">
                  Area
                  <input type="range" min={0} max={1} step={0.05} value={view.aoiOpacity} aria-label="Area opacity" style={{ ...fillOf(view.aoiOpacity, 0, 1), width: 96 }}
                    onChange={(e) => setView((v) => ({ ...v, aoiOpacity: parseFloat(e.target.value) }))} />
                </label>
                <label style={sliderLabel} title="Size of the velocity points">
                  Points
                  <input type="range" min={0.5} max={2.5} step={0.1} value={view.pointScale} aria-label="Point size" style={{ ...fillOf(view.pointScale, 0.5, 2.5), width: 96 }}
                    onChange={(e) => setView((v) => ({ ...v, pointScale: parseFloat(e.target.value) }))} />
                </label>
                {picked && canLoad && !showPanel && !view.detached && (
                  <button className="btn btn-primary btn-sm" onClick={() => setPanelOpen(true)}>Show time series</button>
                )}
              </div>

              {!selected && datasets.length > 0 && (
                <div style={overlayStyle}>Select a dataset on the left, or click an area on the map.</div>
              )}
              {loading && (
                <div style={overlayStyle}>
                  <LoadingDots label="Reading points…" />
                  <span style={{ color: "var(--text-muted)" }}>Large EGMS tiles can take a minute.</span>
                </div>
              )}

              {showPanel && !view.detached && (
                <FloatingPanel
                  title={panelTitle} config={panel} onChange={setPanel} boundsRef={stageRef}
                  onClose={() => setPanelOpen(false)}
                  onDetach={() => setView((v) => ({ ...v, detached: true }))}
                >
                  {seriesContent}
                </FloatingPanel>
              )}
            </div>
            {!fullscreen && (
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 10, fontSize: 12, color: "var(--text-muted)" }}>
                <LegendDot color="var(--accent)" label="HyP3 + MintPy project area" />
                <LegendDot color="var(--warning)" label="EGMS download area" />
              </div>
            )}
          </div>

          {showPanel && view.detached && (
            <DetachedWindow title={`${panelTitle} · InSAR Orchestrator`} onClosed={() => setView((v) => ({ ...v, detached: false }))}>
              <div style={{ minHeight: "100vh", background: "var(--bg)", padding: 14, boxSizing: "border-box", display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ flex: 1, fontFamily: "var(--font-display)", fontWeight: 600 }}>{panelTitle}</div>
                  <button className="btn btn-secondary btn-sm" onClick={() => setView((v) => ({ ...v, detached: false }))}>Bring back to the viewer</button>
                </div>
                <div style={{ flex: 1, minHeight: 0 }}>{seriesContent}</div>
              </div>
            </DetachedWindow>
          )}

          {selected && !selected.ready && (
            <div className="card" style={{ padding: 16, display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
              <div style={{ flex: 1, minWidth: 220, fontSize: 13 }}>
                <b>{selected.name}</b> has no MintPy results yet. Download its interferograms and run MintPy from the ASF page to see it here.
              </div>
              <button className="btn btn-primary btn-sm" onClick={() => onGoTo("dashboard")}>Go to ASF</button>
            </div>
          )}

          {canLoad && pointsQuery.isError && (
            <div style={{ padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13 }}>
              {errorText(pointsQuery.error)}
            </div>
          )}

          {canLoad && pointsQuery.data && selected?.kind === "mintpy" && (
            <div className="card" style={{ padding: 14, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: "var(--text-muted)", marginRight: 4 }}>Go to coordinates</span>
              <input
                className="input" style={{ width: 120 }} type="number" step="any"
                placeholder="Latitude" value={latInput} onChange={(e) => setLatInput(e.target.value)}
              />
              <input
                className="input" style={{ width: 120 }} type="number" step="any"
                placeholder="Longitude" value={lonInput} onChange={(e) => setLonInput(e.target.value)}
              />
              <button
                className="btn btn-secondary btn-sm"
                disabled={!latInput || !lonInput}
                onClick={() => pick({ lat: parseFloat(latInput), lon: parseFloat(lonInput) })}
              >
                Get time series
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const controlBar: React.CSSProperties = {
  position: "absolute", top: 10, left: 10, zIndex: 25, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
  padding: "6px 10px", borderRadius: 12, background: "rgba(10,14,13,0.88)", backdropFilter: "blur(10px)",
  border: "1px solid var(--border)", maxWidth: "calc(100% - 70px)",
};
const barButton: React.CSSProperties = {
  display: "grid", placeItems: "center", width: 28, height: 28, borderRadius: 8, border: "1px solid var(--border)",
  background: "var(--panel)", color: "var(--text)", cursor: "pointer", padding: 0,
};
const sliderLabel: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: "var(--text-muted)",
};
const overlayStyle: React.CSSProperties = {
  position: "absolute", bottom: 14, left: "50%", transform: "translateX(-50%)", zIndex: 20,
  display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", fontSize: 12,
  background: "rgba(10,14,13,0.88)", backdropFilter: "blur(8px)", border: "1px solid var(--border)",
  borderRadius: 999, color: "var(--text)", pointerEvents: "none", whiteSpace: "nowrap",
};

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, boxShadow: `0 0 8px ${color}` }} />
      {label}
    </span>
  );
}
