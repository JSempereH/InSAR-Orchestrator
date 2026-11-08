import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { mintpyApi } from "../../api/client";
import { LoadingDots } from "../LoadingDots";
import { ProcessingDetails } from "./ProcessingDetails";

interface MintPyPanelProps {
  projectId: string;
  onOpenViewer: (datasetId: string) => void;
}

const RUNNING_POLL_MS = 5_000;
const IDLE_POLL_MS = false as const;

// smallbaselineApp.py always runs this same fixed step sequence by default
// (confirmed against a real run in this repo) - MintPyAdapter.write_config
// never overrides the step list, so this doesn't need to come from the API.
const MINTPY_STEPS = [
  "load_data", "modify_network", "reference_point", "quick_overview",
  "correct_unwrap_error", "invert_network", "correct_LOD", "correct_SET",
  "correct_ionosphere", "correct_troposphere", "deramp", "correct_topography",
  "residual_RMS", "reference_date", "velocity", "geocode", "google_earth", "hdfeos5",
];

// smallbaselineApp.py marks each step with a line like
// "******************** step - load_data ********************" - this finds
// the last one in the log tail so far, so the UI can show "step X of Y"
// instead of a wall of raw log text the user has to scroll through to tell
// whether the run is still progressing.
function parseCurrentStep(logTail: string): { name: string; index: number } | null {
  const matches = [...logTail.matchAll(/\*+\s*step - (\S+)\s*\*+/g)];
  if (matches.length === 0) return null;
  const name = matches[matches.length - 1][1];
  const index = MINTPY_STEPS.indexOf(name);
  return { name, index };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

export function MintPyPanel({ projectId, onOpenViewer }: MintPyPanelProps) {
  const qc = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["mintpy-status", projectId],
    queryFn: () => mintpyApi.status(projectId),
    refetchInterval: (query) => (query.state.data?.status === "RUNNING" ? RUNNING_POLL_MS : IDLE_POLL_MS),
  });

  const succeeded = status?.status === "SUCCEEDED";

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
  const currentStep = isRunning && status?.log_tail ? parseCurrentStep(status.log_tail) : null;
  const diskUsage = status?.disk_usage;

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

      {diskUsage && (
        <div style={{ marginTop: 10, fontSize: 12, color: "var(--text-muted)" }}>
          {succeeded ? (
            <>
              Disk space now in use:{" "}
              <strong style={{ color: "var(--text)" }}>{formatBytes(diskUsage.total_bytes)}</strong>
            </>
          ) : isRunning ? (
            <>
              Disk space in use: <strong style={{ color: "var(--text)" }}>{formatBytes(diskUsage.total_bytes)}</strong>
              {diskUsage.zips_bytes > 0 && ` (${formatBytes(diskUsage.zips_bytes)} still awaiting cleanup)`}
            </>
          ) : (
            diskUsage.zips_bytes > 0 && (
              <>
                Needs <strong style={{ color: "var(--text)" }}>{formatBytes(diskUsage.zips_bytes)}</strong> of disk
                space for the downloaded interferograms. Running will crop them to this project's area and delete
                the originals, freeing most of that space.
              </>
            )
          )}
        </div>
      )}

      {!!status?.warnings?.length && (
        <div style={{
          marginTop: 10, padding: "8px 12px", fontSize: 12, lineHeight: 1.5,
          background: "var(--warning-light)", color: "var(--warning)",
          border: "1px solid var(--warning-border)", borderRadius: "var(--radius)",
        }}>
          {status.warnings.map((w) => <div key={w}>{w}</div>)}
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
          {isRunning && (
            <div style={{ marginTop: 8 }}>
              {currentStep ? (
                <LoadingDots
                  label={
                    currentStep.index >= 0
                      ? `Step ${currentStep.index + 1} of ${MINTPY_STEPS.length}: ${currentStep.name.replace(/_/g, " ")}`
                      : `Running: ${currentStep.name.replace(/_/g, " ")}`
                  }
                />
              ) : (
                <LoadingDots label="Extracting downloaded interferograms…" />
              )}
            </div>
          )}
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

      {status?.status === "SUCCEEDED" && (
        <div style={{
          marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--border)",
          display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
        }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>Results are ready</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
              Explore the velocity map and the displacement time series of any point in the viewer.
            </div>
          </div>
          <button className="btn btn-primary btn-sm" onClick={() => onOpenViewer(projectId)}>
            Open in viewer
          </button>
        </div>
      )}

      <ProcessingDetails projectId={projectId} />
    </div>
  );
}
