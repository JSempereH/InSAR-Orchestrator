import { useQuery } from "@tanstack/react-query";
import { mintpyApi } from "../../api/client";
import { StatusDot, StatusPill } from "./status";

/** The run's own checks (network, coherence, residuals, reference pixel) in plain words. */
export function QualityChecks({ projectId, finishedAt }: { projectId: string; finishedAt?: string | null }) {
  const { data, isError } = useQuery({
    queryKey: ["mintpy-quality", projectId, finishedAt],
    queryFn: () => mintpyApi.quality(projectId),
    staleTime: 60_000,
  });
  if (isError || !data || data.checks.length === 0) return null;
  const attention = data.checks.filter((c) => c.status !== "ok").length;

  return (
    <details style={{ marginTop: 14 }} open={attention > 0}>
      <summary style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 10, fontSize: 13, fontWeight: 600 }}>
        Quality checks
        <StatusPill status={data.status} text={attention === 0 ? "All fine" : `${attention} to look at`} />
      </summary>
      <div style={{ marginTop: 8 }}>
        {data.checks.map((check) => (
          <div key={check.id} style={{ display: "flex", gap: 10, padding: "5px 0", fontSize: 12 }}>
            <StatusDot status={check.status} />
            <div style={{ flex: 1 }}>
              <div>
                <span style={{ fontWeight: 600 }}>{check.label}</span>{" "}
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>
                  {check.value}{check.unit ? ` ${check.unit}` : ""}
                </span>
              </div>
              <div style={{ color: "var(--text-muted)", lineHeight: 1.45 }}>{check.message}</div>
            </div>
          </div>
        ))}
        {data.metrics.velocity_std_mm_yr_median !== undefined && (
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>
            Typical standard error of the velocity: {data.metrics.velocity_std_mm_yr_median} mm/year. This comes from how
            well the series fits the interferograms, so it is optimistic. Validate against EGMS or GNSS for an honest error.
          </div>
        )}
        {data.metrics.velocity_uncertainty && (
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6, lineHeight: 1.45 }}>
            <span style={{ fontWeight: 600, color: "var(--text)" }}>Precision with distance.</span>{" "}
            {data.metrics.velocity_uncertainty.summary}
            {data.metrics.velocity_uncertainty.at.length > 0 && (
              <table style={{ marginTop: 6, borderCollapse: "collapse", fontFamily: "var(--font-mono)" }}>
                <tbody>
                  <tr>
                    <td style={{ paddingRight: 14 }}>between two points</td>
                    {data.metrics.velocity_uncertainty.at.map((m) => (
                      <td key={m.distance_km} style={{ paddingRight: 12, textAlign: "right" }}>{m.distance_km} km</td>
                    ))}
                  </tr>
                  <tr>
                    <td style={{ paddingRight: 14 }}>precision (mm/year)</td>
                    {data.metrics.velocity_uncertainty.at.map((m) => (
                      <td key={m.distance_km} style={{ paddingRight: 12, textAlign: "right" }}>{m.sigma_mm_yr}</td>
                    ))}
                  </tr>
                  <tr>
                    <td style={{ paddingRight: 14 }}>seen in the map (mm/year)</td>
                    {data.metrics.velocity_uncertainty.at.map((m) => (
                      <td key={m.distance_km} style={{ paddingRight: 12, textAlign: "right" }}>{m.observed_mm_yr}</td>
                    ))}
                  </tr>
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </details>
  );
}
