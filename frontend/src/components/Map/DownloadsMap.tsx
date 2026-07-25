import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

const OSM_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors',
      maxzoom: 19,
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm" }],
};

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
  height?: number;
}

const SOURCE_COLOR: Record<DownloadFootprint["source"], string> = {
  asf: "#2563eb",
  egms: "#d97706",
};

// Diverging velocity scale: subsidence (red) <-> stable (neutral gray) <-> uplift (blue).
const VELOCITY_NEGATIVE = "#dc2626";
const VELOCITY_NEUTRAL = "#94a3b8";
const VELOCITY_POSITIVE = "#2563eb";

export function DownloadsMap({ footprints, points, selectedId, onSelect, height = 480 }: DownloadsMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: OSM_STYLE,
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
      map.addLayer({
        id: "velocity-points",
        type: "circle",
        source: "velocity-points",
        paint: {
          "circle-radius": 4,
          "circle-color": VELOCITY_NEUTRAL,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1,
        },
      });

      map.on("click", "footprints-fill", (e) => {
        const id = e.features?.[0]?.properties?.id;
        if (id) onSelect(id);
      });
      map.on("click", (e) => {
        const hits = map.queryRenderedFeatures(e.point, { layers: ["footprints-fill"] });
        if (hits.length === 0) onSelect(null);
      });
      map.on("mouseenter", "footprints-fill", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "footprints-fill", () => { map.getCanvas().style.cursor = ""; });

      setReady(true);
    });

    mapRef.current = map;
    return () => map.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Footprints + fit bounds when the list changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const features: GeoJSON.Feature[] = footprints.map((f) => ({
      type: "Feature",
      geometry: f.geometry,
      properties: { id: f.id, label: f.label, color: SOURCE_COLOR[f.source], selected: f.id === selectedId },
    }));
    setGeoJSON(map, "footprints", features);

    const bounds = boundsOf(features.map((f) => f.geometry).concat(points ? points.features.map((f) => f.geometry) : []));
    if (bounds) map.fitBounds(bounds, { padding: 40, maxZoom: 12, duration: 400 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, footprints]);

  // Selection highlight + fly to the selected footprint, since footprints
  // can be far enough apart (different regions) that the combined "fit all"
  // view zooms out too far for any single one to be visible.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const features: GeoJSON.Feature[] = footprints.map((f) => ({
      type: "Feature",
      geometry: f.geometry,
      properties: { id: f.id, label: f.label, color: SOURCE_COLOR[f.source], selected: f.id === selectedId },
    }));
    setGeoJSON(map, "footprints", features);

    const selected = footprints.find((f) => f.id === selectedId);
    if (selected) {
      const bounds = boundsOf([selected.geometry]);
      if (bounds) map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 500 });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // Velocity points layer, color-scaled to the loaded data's own range
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    if (!points || points.features.length === 0) {
      setGeoJSON(map, "velocity-points", []);
      return;
    }

    setGeoJSON(map, "velocity-points", points.features);

    const velocities = points.features
      .map((f) => f.properties?.velocity)
      .filter((v): v is number => typeof v === "number");
    const domainMax = velocities.length ? Math.max(1, ...velocities.map(Math.abs)) : 10;

    map.setPaintProperty("velocity-points", "circle-color", [
      "case",
      ["==", ["get", "velocity"], null], VELOCITY_NEUTRAL,
      ["interpolate", ["linear"], ["get", "velocity"],
        -domainMax, VELOCITY_NEGATIVE,
        0, VELOCITY_NEUTRAL,
        domainMax, VELOCITY_POSITIVE,
      ],
    ]);

    const bounds = boundsOf(points.features.map((f) => f.geometry));
    if (bounds) map.fitBounds(bounds, { padding: 40, maxZoom: 14, duration: 400 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, points]);

  return (
    <div style={{ position: "relative", borderRadius: "var(--radius-lg)", overflow: "hidden", border: "1px solid var(--border)" }}>
      <div ref={containerRef} style={{ width: "100%", height }} />

      {ready && points && points.features.length > 0 && (
        <div style={{
          position: "absolute", bottom: 10, right: 10, zIndex: 10,
          background: "rgba(255,255,255,0.95)", borderRadius: "var(--radius)",
          padding: "8px 12px", fontSize: 11, boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
        }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>Velocity (mm/yr)</div>
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
          background: "#f8fafc", gap: 8,
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

function setGeoJSON(map: maplibregl.Map, sourceId: string, features: GeoJSON.Feature[]) {
  (map.getSource(sourceId) as maplibregl.GeoJSONSource | undefined)?.setData(fc(features));
}

function boundsOf(geometries: GeoJSON.Geometry[]): maplibregl.LngLatBoundsLike | null {
  let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
  let found = false;

  function visit(coords: any) {
    if (typeof coords[0] === "number") {
      const [lon, lat] = coords;
      found = true;
      minLon = Math.min(minLon, lon); maxLon = Math.max(maxLon, lon);
      minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat);
    } else {
      coords.forEach(visit);
    }
  }

  for (const geom of geometries) {
    if ("coordinates" in geom) visit(geom.coordinates);
  }

  return found ? [[minLon, minLat], [maxLon, maxLat]] : null;
}
