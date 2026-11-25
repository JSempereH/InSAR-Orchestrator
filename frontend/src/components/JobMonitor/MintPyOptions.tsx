import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MintPyOption, MintPyRunRequest, mintpyApi } from "../../api/client";

export interface RunChoices {
  options: Record<string, string>;
  lat: string;
  lon: string;
}

export const NO_CHOICES: RunChoices = { options: {}, lat: "", lon: "" };

/** The request body for a run: only what the person changed. An empty request runs the defaults. */
export function toRequest(choices: RunChoices): MintPyRunRequest {
  const options = Object.fromEntries(Object.entries(choices.options).filter(([, v]) => v.trim() !== ""));
  const lat = Number(choices.lat), lon = Number(choices.lon);
  const reference = choices.lat.trim() !== "" && choices.lon.trim() !== "" && Number.isFinite(lat) && Number.isFinite(lon)
    ? { lat, lon } : null;
  return { options, reference };
}

export function hasChoices(choices: RunChoices): boolean {
  const request = toRequest(choices);
  return Object.keys(request.options ?? {}).length > 0 || request.reference !== null;
}

const field = {
  width: "100%", padding: "6px 8px", fontSize: 12, borderRadius: 8, color: "var(--text)",
  background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", fontFamily: "var(--font)",
} as const;
const labelStyle = { fontSize: 12, fontWeight: 600, marginBottom: 3, display: "block" } as const;
const hint = { fontSize: 11, color: "var(--text-muted)", marginTop: 3, lineHeight: 1.4 } as const;

function OptionField({ option, value, onChange }: { option: MintPyOption; value: string; onChange: (v: string) => void }) {
  const id = `mintpy-${option.key}`;
  return (
    <div style={{ marginBottom: 10 }}>
      <label htmlFor={id} style={labelStyle}>{option.label}</label>
      {option.kind === "choice" ? (
        <select id={id} style={field} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">Default ({option.default})</option>
          {(option.choices ?? []).filter((c) => c !== "auto").map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      ) : (
        <input id={id} style={field} value={value} placeholder={`Default (${option.default})`}
               inputMode="decimal" onChange={(e) => onChange(e.target.value)} />
      )}
      <div style={hint}>{option.help}</div>
    </div>
  );
}

/**
 * The MintPy options worth changing, and the reference point. Leaving everything alone runs the
 * defaults, which is what a run without this form always did. What was used is saved with the
 * results (provenance), so a changed run can be told apart from a default one later.
 */
export function MintPyOptionsForm({
  projectId, value, onChange,
}: { projectId: string; value: RunChoices; onChange: (next: RunChoices) => void }) {
  const [open, setOpen] = useState(false);
  const { data, isError } = useQuery({
    queryKey: ["mintpy-options", projectId],
    queryFn: () => mintpyApi.options(projectId),
    enabled: open,
    staleTime: 60_000,
  });
  const groups = data ? [...new Set(data.options.map((o) => o.group))] : [];
  const changed = hasChoices(value);

  return (
    <div style={{ marginTop: 12 }}>
      <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Hide options" : "Options"}{changed && !open ? " (changed)" : ""}
      </button>
      {open && (
        <div style={{ marginTop: 10, padding: "12px 14px", border: "1px solid var(--border)", borderRadius: 12, background: "rgba(0,0,0,0.25)" }}>
          {isError && <div style={{ fontSize: 12, color: "var(--danger)" }}>Could not load the options.</div>}
          {data && (
            <>
              <div style={{ ...hint, marginBottom: 10 }}>
                These change how MintPy processes this project. Leave a field on its default unless you know why.
                {data.last_run && Object.keys(data.last_run).length > 0 && " The last run used custom values (see Processing details)."}
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: "0 24px" }}>
                {groups.map((group) => (
                  <div key={group}>
                    <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--text-faint)", marginBottom: 6 }}>
                      {group}
                    </div>
                    {data.options.filter((o) => o.group === group).map((option) => (
                      <OptionField key={option.key} option={option} value={value.options[option.key] ?? ""}
                                   onChange={(v) => onChange({ ...value, options: { ...value.options, [option.key]: v } })} />
                    ))}
                  </div>
                ))}
                <div>
                  <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--text-faint)", marginBottom: 6 }}>
                    Reference point
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <div style={{ flex: 1 }}>
                      <label htmlFor="mintpy-ref-lat" style={labelStyle}>Latitude</label>
                      <input id="mintpy-ref-lat" style={field} value={value.lat} placeholder="Automatic" inputMode="decimal"
                             onChange={(e) => onChange({ ...value, lat: e.target.value })} />
                    </div>
                    <div style={{ flex: 1 }}>
                      <label htmlFor="mintpy-ref-lon" style={labelStyle}>Longitude</label>
                      <input id="mintpy-ref-lon" style={field} value={value.lon} placeholder="Automatic" inputMode="decimal"
                             onChange={(e) => onChange({ ...value, lon: e.target.value })} />
                    </div>
                  </div>
                  <div style={hint}>
                    Every value in the result is relative to this pixel. Left empty, the app picks the most coherent pixel
                    away from the edge, which can be inside a subsiding area. Choose a point you know is stable.
                    {data.last_reference && ` Last run: ${data.last_reference}.`}
                  </div>
                </div>
              </div>
              {changed && (
                <button className="btn btn-ghost btn-sm" style={{ marginTop: 6 }} onClick={() => onChange(NO_CHOICES)}>
                  Back to the defaults
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
