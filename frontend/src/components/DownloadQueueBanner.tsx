import { DownloadProgress } from "../api/client";

// Structurally matches SLCQueueState and EGMSQueueState (client.ts) - they
// were already identical shapes, so no new type needs to flow through there.
export interface QueueBannerState {
  active: boolean;
  current_filename: string | null;
  current_progress: DownloadProgress | null;
  destination: string | null;
  total: number;
  done: number;
}

interface DownloadQueueBannerProps {
  title: string;
  state: QueueBannerState;
  onCancel: () => void;
  cancelling: boolean;
  style?: React.CSSProperties;
}

/**
 * Shared "a download queue is running" banner - was duplicated almost
 * verbatim between the EGMS and SLC download panels (GroundMotion.tsx,
 * SLCPanel.tsx), differing only in the title text and outer margin (which
 * the caller now supplies via `style`).
 */
export function DownloadQueueBanner({ title, state, onCancel, cancelling, style }: DownloadQueueBannerProps) {
  const pct = state.current_progress?.pct ?? 0;

  return (
    <div style={{
      padding: "14px 18px",
      background: "linear-gradient(135deg, rgba(var(--accent-rgb),0.12) 0%, rgba(255,255,255,0.03) 70%), #0b0f0e",
      border: "1px solid rgba(61,255,154,0.3)", borderRadius: "var(--radius-lg)",
      ...style,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--accent)", boxShadow: "0 0 8px var(--accent)" }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "var(--text)" }}>{title}</span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-muted)", background: "rgba(255,255,255,0.06)", padding: "2px 10px", borderRadius: 20 }}>
          {state.done} / {state.total} complete
        </span>
        <button className="btn btn-ghost btn-sm" disabled={cancelling} onClick={onCancel} style={{ fontSize: 11, color: "var(--danger)", borderColor: "rgba(248,113,113,0.3)" }}>
          {cancelling ? "Cancelling…" : "× Cancel remaining"}
        </button>
      </div>

      {state.current_filename && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }} title={state.current_filename}>
          {state.current_filename}
        </div>
      )}

      {state.destination && (
        <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 6 }}>
          💾 {state.destination}
        </div>
      )}

      <div style={{ height: 14, background: "rgba(255,255,255,0.05)", borderRadius: 8, overflow: "hidden", border: "1px solid rgba(61,255,154,0.2)" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 8, transition: "width 0.3s ease",
          background: "linear-gradient(90deg, var(--accent-2), var(--accent))",
        }} />
      </div>
    </div>
  );
}
