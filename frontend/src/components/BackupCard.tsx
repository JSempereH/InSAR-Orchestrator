import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { backupApi, projectsApi } from "../api/client";

function errorText(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail ?? (err instanceof Error ? err.message : "Request failed");
}

export function BackupCard() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["backup"],
    queryFn: backupApi.overview,
    refetchInterval: (q) => (q.state.data?.state.running ? 1500 : 10000),
  });

  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const toggleProject = useMutation({
    mutationFn: ({ id, value }: { id: string; value: boolean }) => projectsApi.update(id, { backup_enabled: value }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  const [destination, setDestination] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [intervalHours, setIntervalHours] = useState(24);
  const [keepDaily, setKeepDaily] = useState(14);
  const [keepWeekly, setKeepWeekly] = useState(8);
  const [includeSlc, setIncludeSlc] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (data && !loaded) {
      setDestination(data.config.destination ?? "");
      setEnabled(data.config.enabled);
      setIntervalHours(data.config.interval_hours);
      setKeepDaily(data.config.keep_daily);
      setKeepWeekly(data.config.keep_weekly);
      setIncludeSlc(data.config.include_slc);
      setLoaded(true);
    }
  }, [data, loaded]);

  const save = useMutation({
    mutationFn: () =>
      backupApi.saveConfig({
        destination: destination.trim() || null,
        enabled,
        interval_hours: intervalHours,
        keep_daily: keepDaily,
        keep_weekly: keepWeekly,
        include_slc: includeSlc,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backup"] }),
  });
  const test = useMutation({ mutationFn: () => backupApi.test(destination.trim()) });
  const run = useMutation({
    mutationFn: backupApi.run,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backup"] }),
  });

  const rclone = data?.rclone;
  const state = data?.state;
  const config = data?.config;
  const muted = { fontSize: 12, color: "var(--text-muted)" } as const;

  return (
    <div className="card" style={{ padding: 20, marginBottom: 16 }}>
      <h3 style={{ marginBottom: 4 }}>Backup</h3>
      <p style={{ margin: "0 0 14px", ...muted }}>
        Copies every project folder and a safe snapshot of the database to S3, a mounted disk or
        another computer, using rclone. Nothing at the destination is ever deleted, except old
        database snapshots beyond the retention below. MintPy work files are skipped because they can
        be regenerated, and so are raw SLC scenes unless you tick the option below. <code>backend/.env</code> is never uploaded: keep
        a copy of it somewhere else.
      </p>

      {rclone && !rclone.available && (
        <div style={{ padding: "10px 14px", background: "var(--danger-light)", borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>
          rclone is not installed on the backend machine. Install it from rclone.org/install and restart the backend.
        </div>
      )}

      <div className="field">
        <label>Destination</label>
        <input
          className="input"
          value={destination}
          onChange={(e) => setDestination(e.target.value)}
          placeholder="/mnt/backup/insar   or   remotename:bucket/insar"
        />
        <div style={{ ...muted, marginTop: 4 }}>
          A remote is created once with <code>rclone config</code> (S3, SFTP to another computer, and more).
          {rclone?.remotes.length ? <> Configured: {rclone.remotes.join(", ")}.</> : null}
        </div>
      </div>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div className="field" style={{ flex: 1, minWidth: 110 }}>
          <label>Every (hours)</label>
          <input className="input" type="number" min={1} value={intervalHours} onChange={(e) => setIntervalHours(Number(e.target.value))} />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 110 }}>
          <label>Keep daily snapshots</label>
          <input className="input" type="number" min={1} value={keepDaily} onChange={(e) => setKeepDaily(Number(e.target.value))} />
        </div>
        <div className="field" style={{ flex: 1, minWidth: 110 }}>
          <label>Keep weekly snapshots</label>
          <input className="input" type="number" min={0} value={keepWeekly} onChange={(e) => setKeepWeekly(Number(e.target.value))} />
        </div>
      </div>

      {!!projects?.length && (
        <div className="field">
          <label>Projects to back up</label>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {projects.map((p) => (
              <label key={p.id} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, cursor: p.archived_remote ? "default" : "pointer" }}>
                <input
                  type="checkbox"
                  checked={p.backup_enabled && !p.archived_remote}
                  disabled={!!p.archived_remote || toggleProject.isPending}
                  onChange={(e) => toggleProject.mutate({ id: p.id, value: e.target.checked })}
                />
                {p.name}
                {p.archived_remote && <span style={muted}> (archived, already stored remotely)</span>}
              </label>
            ))}
          </div>
          <div style={{ ...muted, marginTop: 4 }}>
            The database snapshot always lists every project, so a restore keeps the history of the ones left unticked.
          </div>
        </div>
      )}

      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, margin: "4px 0 14px", cursor: "pointer" }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Run automatically on this schedule
      </label>
      <label
        style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, margin: "0 0 14px", cursor: "pointer" }}
        title="Raw SLC scenes cost no HyP3 credits, ASF serves them for free, but they are several GB each"
      >
        <input type="checkbox" checked={includeSlc} onChange={(e) => setIncludeSlc(e.target.checked)} />
        Also back up raw SLC scenes (large; free to download again)
      </label>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button className="btn" disabled={!destination.trim() || test.isPending || !rclone?.available} onClick={() => test.mutate()}>
          {test.isPending ? "Testing…" : "Test destination"}
        </button>
        <button
          className="btn"
          disabled={!config?.destination || state?.running || run.isPending || !rclone?.available}
          onClick={() => run.mutate()}
          title="Uses the saved destination"
        >
          {state?.running ? "Backing up…" : "Back up now"}
        </button>
      </div>

      {save.isError && <div style={{ ...muted, color: "var(--danger)", marginTop: 8 }}>{errorText(save.error)}</div>}
      {test.isError && <div style={{ ...muted, color: "var(--danger)", marginTop: 8 }}>{errorText(test.error)}</div>}
      {test.isSuccess && <div style={{ ...muted, marginTop: 8 }}>The destination is writable.</div>}
      {run.isError && <div style={{ ...muted, color: "var(--danger)", marginTop: 8 }}>{errorText(run.error)}</div>}

      {state?.running && (
        <div style={{ ...muted, marginTop: 10 }}>
          {state.step ?? "Working"}: {Math.round(state.pct)}%
        </div>
      )}
      {!state?.running && config?.last_run_at && (
        <div style={{ ...muted, marginTop: 10, color: config.last_status === "error" ? "var(--danger)" : "var(--text-muted)", whiteSpace: "pre-wrap" }}>
          Last run {new Date(config.last_run_at + "Z").toLocaleString()}: {config.last_status === "ok" ? "succeeded" : "failed"}.
          {" "}{config.last_message}
        </div>
      )}
    </div>
  );
}
