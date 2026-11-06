import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

// OpenFreeMap dark style: free, no API key, OpenStreetMap data (attribution is
// included by the style itself).
const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/dark";

export interface DownloadFootprint {
  id: string;
  source: "asf" | "egms";
  label: string;
  geometry: GeoJSON.Geometry;
}

interface DownloadsMapProps {
  footprints: DownloadFootprint[];
  points: GeoJSON.FeatureCollection | null;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onPointClick?: (feature: GeoJSON.Feature) => void;
  height?: number | string;
  /** 0..1 multiplier for the footprint (AOI) fill and outline opacity. */
  aoiOpacity?: number;
  /** Multiplier for the size of the velocity points. */
  pointScale?: number;
  /** A point to ring on the map, e.g. the one whose time series is open. */
  highlight?: [number, number] | null;
}

const SOURCE_COLOR: Record<DownloadFootprint["source"], string> = {
  asf: "#3dff9a",
  egms: "#ffb020",
};

// Diverging velocity scale: subsidence (red) <-> stable (neutral gray) <-> uplift (blue).
const VELOCITY_NEGATIVE = "#ff5d6c";
// Stable points recede into the background: a dim teal, not a bright gray.
const VELOCITY_NEUTRAL = "#4d625b";
const VELOCITY_POSITIVE = "#3dff9a";

// Radius stops as a function of how much a point moves ("m", 0..1), scaled by the user's size setting.
const moveExpr = (a: number, b: number): maplibregl.ExpressionSpecification => ["+", a, ["*", b, ["get", "m"]]];
const coreRadius = (k: number): maplibregl.ExpressionSpecification => [
  "interpolate", ["linear"], ["zoom"],
  8, moveExpr(0.9 * k, 1.4 * k), 12, moveExpr(1.3 * k, 2.4 * k), 16, moveExpr(2.2 * k, 4 * k), 19, moveExpr(3.5 * k, 6 * k),
];
const haloRadius = (k: number): maplibregl.ExpressionSpecification => [
  "interpolate", ["linear"], ["zoom"],
  8, moveExpr(1.5 * k, 1.5 * k), 12, moveExpr(2.5 * k, 3 * k), 15, moveExpr(6 * k, 7 * k), 19, moveExpr(14 * k, 14 * k),
];

export function DownloadsMap({
  footprints, points, selectedId, onSelect, onPointClick, height = 480,
  aoiOpacity = 1, pointScale = 1, highlight = null,
}: DownloadsMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const lastFlownId = useRef<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BASEMAP_STYLE,
      center: [0, 30],
      zoom: 2,
    });

    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.addControl(new maplibregl.ScaleControl(), "bottom-left");

    map.on("load", () => {
      map.addSource("footprints", { type: "geojson", data: fc([]) });
      map.addSource("velocity-points", { type: "geojson", data: fc([]) });

      map.addLayer({
        id: "footprints-fill",
        type: "fill",
        source: "footprints",
        paint: {
          "fill-color": ["get", "color"],
          "fill-opacity": ["case", ["get", "selected"], 0.28, 0.12],
        },
      });
      map.addLayer({
        id: "footprints-outline",
        type: "line",
        source: "footprints",
        paint: {
          "line-color": ["get", "color"],
          "line-width": ["case", ["get", "selected"], 3.5, 1.5],
        },
      });
      // Velocity points are drawn with a visual hierarchy: the more a point
      // moves relative to the rest ("m", 0..1, set in the points effect below),
      // the larger, brighter and more opaque it is, and only the strongest
      // movers get a soft glow. Stable points stay small and faint, so the
      // few that matter are not lost in a dense cloud.
      const byMove = moveExpr;
      map.addLayer({
        id: "velocity-halo",
        type: "circle",
        source: "velocity-points",
        filter: [">", ["get", "m"], 0.4],
        layout: { "circle-sort-key": ["get", "m"] },
        paint: {
          "circle-radius": haloRadius(1),
          "circle-color": VELOCITY_NEUTRAL,
          "circle-blur": 1,
          "circle-opacity": byMove(0.02, 0.16),
        },
      });
      map.addLayer({
        id: "velocity-points",
        type: "circle",
        source: "velocity-points",
        layout: { "circle-sort-key": ["get", "m"] },
        paint: {
          "circle-radius": coreRadius(1),
          "circle-opacity": byMove(0.2, 0.7),
          "circle-color": VELOCITY_NEUTRAL,
        },
      });

      map.addSource("picked", { type: "geojson", data: fc([]) });
      map.addLayer({
        id: "picked-ring",
        type: "circle",
        source: "picked",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 7, 16, 11, 19, 15],
          "circle-color": "rgba(0,0,0,0)",
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 2.5,
        },
      });

      map.on("click", "footprints-fill", (e) => {
        // A point drawn on top of the footprint also triggers this handler;
        // clicking a point must not re-select (and reset) the dataset.
        if (map.queryRenderedFeatures(e.point, { layers: ["velocity-points"] }).length > 0) return;
        const id = e.features?.[0]?.properties?.id;
        if (id) onSelect(id);
      });
      map.on("click", "velocity-points", (e) => {
        const feature = e.features?.[0];
        if (feature && onPointClick) onPointClick(feature);
      });
      map.on("click", (e) => {
        // MapLibre fires this map-wide handler for every click, including
        // ones already handled above by a layer-scoped listener - so it must
        // check velocity-points too, or clicking a point immediately
        // deselects the row that point belongs to.
        const hits = map.queryRenderedFeatures(e.point, { layers: ["footprints-fill", "velocity-points"] });
        if (hits.length === 0) onSelect(null);
      });
      map.on("mouseenter", "footprints-fill", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "footprints-fill", () => { map.getCanvas().style.cursor = ""; });
      map.on("mouseenter", "velocity-points", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "velocity-points", () => { map.getCanvas().style.cursor = ""; });

      setReady(true);
    });

    mapRef.current = map;
    // Development only: lets `make docs` find a point on screen to click for its screenshots.
    const w = window as unknown as { __insarMaps?: maplibregl.Map[] };
    if (import.meta.env.DEV) (w.__insarMaps ??= []).push(map);
    return () => {
      if (w.__insarMaps) w.__insarMaps = w.__insarMaps.filter((m) => m !== map);
      map.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Identity of the footprint list. Parents often rebuild the array on every
  // render, which used to re-run the fit below and zoom out on every click.
  const footprintKey = footprints.map((f) => `${f.id}:${JSON.stringify(f.geometry).length}`).join("|");

  // Footprints + fit bounds when the list changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const features = buildFootprintFeatures(footprints, selectedId);
    setGeoJSON(map, "footprints", features);

    const bounds = boundsOf(features.map((f) => f.geometry).concat(points ? points.features.map((f) => f.geometry) : []));
    if (bounds) map.fitBounds(bounds, { padding: 40, maxZoom: 12, duration: 400 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, footprintKey]);

  // Selection highlight + fly to the selected footprint, since footprints
  // can be far enough apart (different regions) that the combined "fit all"
  // view zooms out too far for any single one to be visible.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    setGeoJSON(map, "footprints", buildFootprintFeatures(footprints, selectedId));

    const selected = footprints.find((f) => f.id === selectedId);
    if (!selected) lastFlownId.current = null;
    if (selected && lastFlownId.current !== selectedId) {
      lastFlownId.current = selectedId;
      const bounds = boundsOf([selected.geometry]);
      if (bounds) map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 500 });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, selectedId, footprintKey]);

  // Appearance controls
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setPaintProperty("footprints-fill", "fill-opacity", ["case", ["get", "selected"], 0.28 * aoiOpacity, 0.12 * aoiOpacity]);
    map.setPaintProperty("footprints-outline", "line-opacity", 0.2 + 0.8 * aoiOpacity);
  }, [ready, aoiOpacity]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setPaintProperty("velocity-points", "circle-radius", coreRadius(pointScale));
    map.setPaintProperty("velocity-halo", "circle-radius", haloRadius(pointScale));
  }, [ready, pointScale]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const geometry: GeoJSON.Geometry | null = highlight ? { type: "Point", coordinates: highlight } : null;
    setGeoJSON(map, "picked", geometry ? [{ type: "Feature", geometry, properties: {} }] : []);
  }, [ready, highlight]);

  // Velocity points layer, color-scaled to the loaded data's own range
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    if (!points || points.features.length === 0) {
      setGeoJSON(map, "velocity-points", []);
      return;
    }

    const velocities = points.features
      .map((f) => f.properties?.velocity)
      .filter((v): v is number => typeof v === "number");
    // Colors and sizes are relative to the median: a uniform offset (the
    // median of one real EGMS tile was -0.4 mm/yr) would otherwise tint every
    // point as subsidence and hide what actually moves differently.
    const sorted = [...velocities].sort((a, b) => a - b);
    const median = sorted.length ? sorted[Math.floor(0.5 * (sorted.length - 1))] : 0;
    // Scale to the 97th percentile of |relative velocity| rather than the
    // maximum, so a few outliers do not wash out everything else.
    const sortedAbs = velocities.map((v) => Math.abs(v - median)).sort((a, b) => a - b);
    const p97 = sortedAbs.length ? sortedAbs[Math.floor(0.97 * (sortedAbs.length - 1))] : 10;
    const domainMax = Math.max(0.5, p97);

    const decorated = points.features.map((f) => {
      const v = typeof f.properties?.velocity === "number" ? f.properties.velocity - median : 0;
      return { ...f, properties: { ...f.properties, vc: v, m: Math.min(1, Math.abs(v) / domainMax) } };
    });
    setGeoJSON(map, "velocity-points", decorated);

    const color = [
      "interpolate", ["linear"], ["get", "vc"],
      -domainMax, VELOCITY_NEGATIVE,
      0, VELOCITY_NEUTRAL,
      domainMax, VELOCITY_POSITIVE,
    ];
    map.setPaintProperty("velocity-points", "circle-color", color as never);
    map.setPaintProperty("velocity-halo", "circle-color", color as never);

    const bounds = boundsOf(points.features.map((f) => f.geometry));
    if (bounds) map.fitBounds(bounds, { padding: 40, maxZoom: 14, duration: 400 });
  }, [ready, points]);

  return (
    <div style={{ position: "relative", borderRadius: "var(--radius-lg)", overflow: "hidden", border: "1px solid var(--border)", height: typeof height === "string" ? height : undefined }}>
      <div ref={containerRef} style={{ width: "100%", height }} />

      {ready && points && points.features.length > 0 && (
        <div style={{
          position: "absolute", bottom: 10, right: 10, zIndex: 10,
          background: "rgba(10,14,13,0.88)", backdropFilter: "blur(8px)", color: "var(--text)",
          border: "1px solid var(--border)", borderRadius: "var(--radius)",
          padding: "8px 12px", fontSize: 11, boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
        }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>Velocity vs. median (mm/yr)</div>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span>Subsidence</span>
            <div style={{ width: 80, height: 8, borderRadius: 4, background: `linear-gradient(90deg, ${VELOCITY_NEGATIVE}, ${VELOCITY_NEUTRAL}, ${VELOCITY_POSITIVE})` }} />
            <span>Uplift</span>
          </div>
        </div>
      )}

      {!ready && (
        <div style={{
          position: "absolute", inset: 0,
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
          background: "var(--panel)", gap: 8,
        }}>
          <div style={{ fontSize: 24 }}>🗺</div>
          <div style={{ fontSize: 13, color: "var(--text-muted)" }}>Loading map…</div>
        </div>
      )}
    </div>
  );
}

function fc(features: GeoJSON.Feature[]): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features };
}

function buildFootprintFeatures(footprints: DownloadFootprint[], selectedId: string | null): GeoJSON.Feature[] {
  return footprints.map((f) => ({
    type: "Feature",
    geometry: f.geometry,
    properties: { id: f.id, label: f.label, color: SOURCE_COLOR[f.source], selected: f.id === selectedId },
  }));
}

function setGeoJSON(map: maplibregl.Map, sourceId: string, features: GeoJSON.Feature[]) {
  (map.getSource(sourceId) as maplibregl.GeoJSONSource | undefined)?.setData(fc(features));
}

function boundsOf(geometries: GeoJSON.Geometry[]): maplibregl.LngLatBoundsLike | null {
  let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
  let found = false;

  type NestedCoords = number[] | NestedCoords[];

  function visit(coords: NestedCoords) {
    if (typeof coords[0] === "number") {
      const [lon, lat] = coords as number[];
      found = true;
      minLon = Math.min(minLon, lon); maxLon = Math.max(maxLon, lon);
      minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat);
    } else {
      (coords as NestedCoords[]).forEach(visit);
    }
  }

  for (const geom of geometries) {
    if ("coordinates" in geom) visit(geom.coordinates);
  }

  return found ? [[minLon, minLat], [maxLon, maxLat]] : null;
}
