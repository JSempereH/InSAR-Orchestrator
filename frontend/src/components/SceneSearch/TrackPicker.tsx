import { useMutation } from "@tanstack/react-query";
import { scenesApi, TrackSummary } from "../../api/client";

interface TrackPickerProps {
  geometry: GeoJSON.Geometry;
  dateStart: string;
  dateEnd: string;
  onSelect: (track: TrackSummary) => void;
}

export function TrackPicker({ geometry, dateStart, dateEnd, onSelect }: TrackPickerProps) {
  const { mutate, data: tracks, isPending, isError } = useMutation({
    mutationFn: () => scenesApi.tracks({ geometry, date_start: dateStart, date_end: dateEnd }),
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)" }}>
          Query ASF to find all available Sentinel-1 tracks for your AOI.
        </p>
        <button className="btn btn-primary" onClick={() => mutate()} disabled={isPending}>
          {isPending ? "Searching…" : "Search tracks"}
        </button>
      </div>

      {isError && (
        <div style={{ padding: "10px 14px", background: "var(--danger-light)", border: "1px solid #fecaca", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13 }}>
          Search failed. Check the backend is running and your AOI is valid.
        </div>
      )}

      {tracks && tracks.length === 0 && (
        <div style={{ padding: "14px", background: "var(--warning-light)", border: "1px solid #fed7aa", borderRadius: "var(--radius)", fontSize: 13, color: "#92400e" }}>
          No tracks found for this area and date range.
        </div>
      )}

      {tracks && tracks.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {tracks.map((t, i) => (
            <TrackCard
              key={`${t.track_number}-${t.flight_direction}`}
              track={t}
              recommended={i === 0}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function TrackCard({ track, recommended, onSelect }: { track: TrackSummary; recommended: boolean; onSelect: (t: TrackSummary) => void }) {
  return (
    <div
      className="card"
      style={{
        padding: "14px 16px",
        display: "flex",
        alignItems: "center",
        gap: 16,
        cursor: "pointer",
        borderColor: recommended ? "#93c5fd" : "var(--border)",
        background: recommended ? "#f0f7ff" : "white",
        transition: "box-shadow 0.15s",
      }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.boxShadow = "var(--shadow-md)"; }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.boxShadow = "var(--shadow)"; }}
      onClick={() => onSelect(track)}
    >
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>Track {track.track_number}</span>
          <span style={{
            padding: "1px 7px",
            background: track.flight_direction === "DESCENDING" ? "#f0fdf4" : "#fff7ed",
            color: track.flight_direction === "DESCENDING" ? "#15803d" : "#c2410c",
            border: "1px solid",
            borderColor: track.flight_direction === "DESCENDING" ? "#bbf7d0" : "#fed7aa",
            borderRadius: 9999,
            fontSize: 11,
            fontWeight: 600,
          }}>
            {track.flight_direction}
          </span>
          {recommended && (
            <span style={{ fontSize: 11, color: "var(--primary)", fontWeight: 600 }}>
              ★ Most scenes
            </span>
          )}
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {track.scene_count} scenes · {track.first_date} → {track.last_date}
        </div>
      </div>
      <button
        className="btn btn-primary btn-sm"
        onClick={(e) => { e.stopPropagation(); onSelect(track); }}
      >
        Select →
      </button>
    </div>
  );
}
