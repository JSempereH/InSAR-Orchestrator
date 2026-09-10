import { useQuery } from "@tanstack/react-query";
import { creditsApi } from "../../api/client";

// The backend refreshes the balance in the background every
// refresh_interval_seconds; this just polls that cache a bit faster so the
// UI catches the new value soon after it lands server-side. Manual refresh
// happens via the page's single "Sync now" button, not a button of its own
// here, so there's only one sync icon on screen.
const POLL_INTERVAL_MS = 30_000;

export function CreditsWidget() {
  const { data } = useQuery({
    queryKey: ["hyp3-credits"],
    queryFn: creditsApi.get,
    refetchInterval: POLL_INTERVAL_MS,
  });

  if (!data) return null;

  const updatedAt = data.updated_at ? new Date(data.updated_at) : null;
  const low = typeof data.credits === "number" && data.credits < 50;

  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 8,
        fontSize: 12, color: "var(--text-muted)",
      }}
      title={data.error ? `Last refresh failed: ${data.error}` : undefined}
    >
      <span style={{ color: "var(--text)", fontWeight: 600 }}>
        {data.credits !== null ? `${data.credits.toFixed(1)} HyP3 credits` : "HyP3 credits: —"}
      </span>
      {low && (
        <span style={{ color: "var(--danger)", fontWeight: 600 }} title="Running low on HyP3 processing credits">
          low
        </span>
      )}
      {updatedAt && (
        <span title={updatedAt.toLocaleString()}>
          synced {updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </span>
      )}
      {data.error && <span style={{ color: "var(--danger)" }}>(refresh failed)</span>}
    </div>
  );
}
