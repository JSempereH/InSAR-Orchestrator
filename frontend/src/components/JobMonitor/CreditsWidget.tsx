import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { creditsApi } from "../../api/client";

// The backend refreshes the balance in the background every
// refresh_interval_seconds; this just polls that cache a bit faster so the
// UI catches the new value soon after it lands server-side.
const POLL_INTERVAL_MS = 30_000;

export function CreditsWidget() {
  const qc = useQueryClient();
  const [, forceTick] = useState(0);

  const { data } = useQuery({
    queryKey: ["hyp3-credits"],
    queryFn: creditsApi.get,
    refetchInterval: POLL_INTERVAL_MS,
  });

  const refreshMut = useMutation({
    mutationFn: creditsApi.refresh,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["hyp3-credits"] }),
  });

  // Re-render every second so "updated Ns ago" / "next in Ns" stay live
  // without waiting for the next query refetch.
  useEffect(() => {
    const id = setInterval(() => forceTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  if (!data) return null;

  const updatedAt = data.updated_at ? new Date(data.updated_at).getTime() : null;
  const secondsAgo = updatedAt ? Math.max(0, Math.round((Date.now() - updatedAt) / 1000)) : null;
  const nextInSeconds = secondsAgo !== null ? Math.max(0, data.refresh_interval_seconds - secondsAgo) : null;
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
      {secondsAgo !== null && (
        <span>
          updated {secondsAgo}s ago · next in {nextInSeconds}s
        </span>
      )}
      {data.error && <span style={{ color: "var(--danger)" }}>(refresh failed)</span>}
      <button
        className="btn btn-ghost btn-sm"
        style={{ fontSize: 11, padding: "2px 6px" }}
        disabled={refreshMut.isPending}
        onClick={() => refreshMut.mutate()}
        title="Refresh HyP3 credit balance now"
      >
        {refreshMut.isPending ? "…" : "↻"}
      </button>
    </div>
  );
}
