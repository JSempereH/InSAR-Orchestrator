import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { mintpyApi } from "../../api/client";

interface MintPyPanelProps {
  projectId: string;
}

const RUNNING_POLL_MS = 5_000;
const IDLE_POLL_MS = false as const;

export function MintPyPanel({ projectId }: MintPyPanelProps) {
  const qc = useQueryClient();

  const { data: status } = useQuery({
    queryKey: ["mintpy-status", projectId],
    queryFn: () => mintpyApi.status(projectId),
    refetchInterval: (query) => (query.state.data?.status === "RUNNING" ? RUNNING_POLL_MS : IDLE_POLL_MS),
  });

  const runMut = useMutation({
    mutationFn: () => mintpyApi.run(projectId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mintpy-status", projectId] }),
  });

  const cancelMut = useMutation({
    mutationFn: () => mintpyApi.cancel(projectId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mintpy-status", projectId] }),
  });

  const isRunning = status?.status === "RUNNING";
  const isNotStarted = !status || status.status === "NOT_STARTED";

  return (
    <div className="card" style={{ padding: "14px 18px", marginTop: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>MintPy SBAS time series</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
            Runs smallbaselineApp.py over this project's downloaded interferograms to
            produce a displacement time series. Requires MintPy installed on the backend host.
          </div>
        </div>
        {isRunning ? (
          <button
            className="btn btn-ghost btn-sm"
            disabled={cancelMut.isPending}
            onClick={() => cancelMut.mutate()}
          >
            {cancelMut.isPending ? "Cancelling…" : "Cancel"}
          </button>
        ) : (
          <button
            className="btn btn-primary btn-sm"
            disabled={runMut.isPending}
            onClick={() => runMut.mutate()}
          >
            {runMut.isPending ? "Starting…" : isNotStarted ? "Run MintPy SBAS" : "Run again"}
          </button>
        )}
      </div>

      {runMut.isError && (
        <div style={{ marginTop: 10, fontSize: 12, color: "var(--danger)" }}>
          {String(
            (runMut.error as AxiosError<{ detail?: string }>)?.response?.data?.detail ?? runMut.error
          )}
        </div>
      )}

      {status && status.status !== "NOT_STARTED" && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 12, display: "flex", gap: 12, color: "var(--text-muted)" }}>
            <span>
              Status: <strong style={{ color: "var(--text)" }}>{status.status}</strong>
            </span>
            {status.started_at && <span>Started {new Date(status.started_at).toLocaleTimeString()}</span>}
            {status.finished_at && <span>Finished {new Date(status.finished_at).toLocaleTimeString()}</span>}
          </div>
          {status.error && (
            <div style={{ marginTop: 6, fontSize: 12, color: "var(--danger)" }}>{status.error}</div>
          )}
          {status.log_tail && (
            <pre
              style={{
                marginTop: 8, maxHeight: 220, overflow: "auto", fontSize: 11,
                background: "var(--bg-inset, #0000000d)", padding: 10, borderRadius: 6,
                whiteSpace: "pre-wrap",
              }}
            >
              {status.log_tail}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
