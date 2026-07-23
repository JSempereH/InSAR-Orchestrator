import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { projectsApi, Project, TrackSummary } from "../api/client";
import { AOIMap } from "../components/Map/AOIMap";
import { TrackPicker } from "../components/SceneSearch/TrackPicker";

type View = "list" | "new";
type Step = 1 | 2 | 3;

export function ProjectsPage() {
  const [view, setView] = useState<View>("list");
  return view === "list"
    ? <ProjectList onNew={() => setView("new")} />
    : <NewProject onDone={() => setView("list")} />;
}

/* ── Project list ─────────────────────────────────────────────── */
function ProjectList({ onNew }: { onNew: () => void }) {
  const qc = useQueryClient();
  const { data: projects, isLoading } = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const deleteMut = useMutation({
    mutationFn: projectsApi.delete,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  return (
    <div className="page">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <h2>Projects</h2>
          <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>
            Each project defines an area of interest and a Sentinel-1 track. Pairs are submitted to HyP3 for cloud processing.
          </p>
        </div>
        <button className="btn btn-primary" onClick={onNew}>+ New project</button>
      </div>

      {isLoading && <p style={{ color: "var(--text-muted)" }}>Loading…</p>}

      {!isLoading && projects?.length === 0 && (
        <div className="card" style={{ padding: 48, textAlign: "center" }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>🛰</div>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>No projects yet</div>
          <div style={{ color: "var(--text-muted)", fontSize: 13, marginBottom: 20 }}>
            Create a project to start searching for InSAR data.
          </div>
          <button className="btn btn-primary" onClick={onNew}>Create your first project</button>
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {projects?.map((p) => <ProjectCard key={p.id} project={p} onDelete={() => deleteMut.mutate(p.id)} />)}
      </div>
    </div>
  );
}

function ProjectCard({ project: p, onDelete }: { project: Project; onDelete: () => void }) {
  return (
    <div className="card" style={{ padding: "16px 20px", display: "flex", alignItems: "center", gap: 16 }}>
      <div style={{
        width: 40, height: 40, borderRadius: 10, flexShrink: 0,
        background: "var(--primary-light)",
        display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18,
      }}>
        🗺
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, marginBottom: 2 }}>{p.name}</div>
        {p.description && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>{p.description}</div>}
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Chip label={`Track ${p.track_number}`} />
          <Chip label={p.flight_direction ?? "—"} />
          <Chip label={`${p.date_start} → ${p.date_end}`} />
          <Chip label={`${p.max_temporal_neighbors} neighbors`} />
        </div>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-faint)", whiteSpace: "nowrap" }}>
        {new Date(p.created_at).toLocaleDateString()}
      </div>
      <button
        className="btn btn-danger btn-sm"
        onClick={() => { if (confirm(`Delete "${p.name}"?`)) onDelete(); }}
      >
        Delete
      </button>
    </div>
  );
}

function Chip({ label }: { label: string }) {
  return (
    <span style={{
      padding: "2px 8px", borderRadius: 9999,
      background: "#f1f5f9", color: "var(--text-muted)",
      fontSize: 11, fontWeight: 500,
    }}>
      {label}
    </span>
  );
}

/* ── New project wizard ───────────────────────────────────────── */
function NewProject({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [step, setStep]   = useState<Step>(1);
  const [name, setName]   = useState("");
  const [desc, setDesc]   = useState("");
  const [geometry, setGeometry]     = useState<GeoJSON.Geometry | null>(null);
  const [dateStart, setDateStart]   = useState("2020-01-01");
  const [dateEnd, setDateEnd]       = useState(new Date().toISOString().slice(0, 10));
  const [track, setTrack]           = useState<TrackSummary | null>(null);
  const [maxNeighbors, setMaxNeighbors] = useState(3);

  const createMut = useMutation({
    mutationFn: projectsApi.create,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["projects"] }); onDone(); },
  });

  return (
    <div className="page" style={{ maxWidth: 860 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 28 }}>
        <button className="btn btn-ghost btn-sm" onClick={onDone} style={{ fontSize: 18, padding: "2px 8px" }}>←</button>
        <div>
          <h2 style={{ marginBottom: 2 }}>New project</h2>
          <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>
            Define an area of interest, select a Sentinel-1 track, and configure the interferometric network.
          </p>
        </div>
      </div>

      {/* Step indicator */}
      <div className="steps" style={{ marginBottom: 32 }}>
        {([
          { n: 1, label: "Area & dates" },
          { n: 2, label: "Track selection" },
          { n: 3, label: "Confirm" },
        ] as { n: Step; label: string }[]).map(({ n, label }, i, arr) => (
          <>
            <div className={`step ${step === n ? "active" : step > n ? "done" : ""}`} key={n}>
              <div className="step-num">{step > n ? "✓" : n}</div>
              <span>{label}</span>
            </div>
            {i < arr.length - 1 && (
              <div className={`step-connector ${step > n ? "done" : ""}`} key={`c${n}`} />
            )}
          </>
        ))}
      </div>

      {/* Step 1: Name + AOI + dates */}
      {step === 1 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <div className="card" style={{ padding: 20 }}>
            <h3 style={{ marginBottom: 16 }}>Project details</h3>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 14 }}>
              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Project name *</label>
                <input
                  className="input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Murcia 2020-2024"
                />
              </div>
              <div className="field">
                <label>Start date *</label>
                <input className="input" type="date" value={dateStart} onChange={(e) => setDateStart(e.target.value)} />
              </div>
              <div className="field">
                <label>End date *</label>
                <input className="input" type="date" value={dateEnd} onChange={(e) => setDateEnd(e.target.value)} />
              </div>
              <div className="field" style={{ gridColumn: "1 / -1" }}>
                <label>Description (optional)</label>
                <textarea className="input" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Brief description of the study area or objective" />
              </div>
            </div>
          </div>

          <div>
            <div style={{ marginBottom: 10 }}>
              <h3 style={{ marginBottom: 4 }}>Area of interest</h3>
              <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)" }}>
                Draw a polygon on the map. Keep it tight: large areas increase processing time and cost.
              </p>
            </div>
            <AOIMap onGeometryChange={setGeometry} height={500} />
            {geometry && (
              <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--success)" }}>
                <span>✓</span> Polygon defined
              </div>
            )}
          </div>

          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              className="btn btn-primary"
              disabled={!name || !geometry}
              onClick={() => setStep(2)}
            >
              Next: find tracks →
            </button>
          </div>
        </div>
      )}

      {/* Step 2: Track picker */}
      {step === 2 && geometry && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <div className="card" style={{ padding: 20 }}>
            <h3 style={{ marginBottom: 4 }}>Select a Sentinel-1 track</h3>
            <p style={{ margin: "0 0 16px", fontSize: 13, color: "var(--text-muted)" }}>
              All scenes must come from the same relative orbit (track) so the acquisition geometry is consistent.
              Choose the one with the most scenes for maximum temporal density.
            </p>
            <TrackPicker
              geometry={geometry}
              dateStart={dateStart}
              dateEnd={dateEnd}
              onSelect={(t) => { setTrack(t); setStep(3); }}
            />
          </div>
          <div>
            <button className="btn btn-ghost btn-sm" onClick={() => setStep(1)}>← Back</button>
          </div>
        </div>
      )}

      {/* Step 3: Confirm */}
      {step === 3 && track && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <div className="card" style={{ padding: 20 }}>
            <h3 style={{ marginBottom: 16 }}>Review and create</h3>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 20 }}>
              <SummaryRow label="Project name" value={name} />
              <SummaryRow label="Track" value={`${track.track_number} (${track.flight_direction})`} />
              <SummaryRow label="Date range" value={`${dateStart} → ${dateEnd}`} />
              <SummaryRow label="Available scenes" value={String(track.scene_count)} />
            </div>

            <div className="field" style={{ maxWidth: 200 }}>
              <label>Max temporal neighbors</label>
              <input
                className="input"
                type="number"
                min={1}
                max={10}
                value={maxNeighbors}
                onChange={(e) => setMaxNeighbors(+e.target.value)}
              />
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                Each scene pairs with the next {maxNeighbors} scenes in time.
              </span>
            </div>

            {track.scene_count > 1 && (
              <div style={{
                marginTop: 16,
                padding: "10px 14px",
                background: "var(--primary-light)",
                border: "1px solid #bfdbfe",
                borderRadius: "var(--radius)",
                fontSize: 13,
              }}>
                This configuration will generate approximately{" "}
                <strong>{estimatePairs(track.scene_count, maxNeighbors)} interferometric pairs</strong>{" "}
                when you submit a batch.
              </div>
            )}
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <button className="btn btn-ghost btn-sm" onClick={() => setStep(2)}>← Back</button>
            <button
              className="btn btn-primary"
              disabled={createMut.isPending}
              onClick={() =>
                createMut.mutate({
                  name,
                  description: desc || undefined,
                  geometry: geometry!,
                  track_number: track.track_number,
                  flight_direction: track.flight_direction,
                  date_start: dateStart,
                  date_end: dateEnd,
                  max_temporal_neighbors: maxNeighbors,
                })
              }
            >
              {createMut.isPending ? "Creating…" : "Create project"}
            </button>
          </div>

          {createMut.isError && (
            <div style={{ color: "var(--danger)", fontSize: 13 }}>
              Failed to create project. {String(createMut.error)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 3 }}>{label}</div>
      <div style={{ fontWeight: 500 }}>{value}</div>
    </div>
  );
}

function estimatePairs(scenes: number, neighbors: number): number {
  let total = 0;
  for (let i = 0; i < scenes; i++) total += Math.min(neighbors, scenes - i - 1);
  return total;
}
