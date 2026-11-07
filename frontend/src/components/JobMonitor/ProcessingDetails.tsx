import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { projectsApi, Provenance } from "../../api/client";

// The MintPy options that most change a result. Shown when MintPy reports them.
const KEY_OPTIONS: [string, string][] = [
  ["mintpy.unwrapError.method", "Unwrapping-error correction"],
  ["mintpy.troposphericDelay.method", "Troposphere correction"],
  ["mintpy.topographicResidual", "DEM error correction"],
  ["mintpy.networkInconsistency.maskDataset", "Network inconsistency mask"],
  ["mintpy.timeFunc.polynomial", "Velocity model (polynomial order)"],
];

const muted = { color: "var(--text-muted)" } as const;
const mono = { fontFamily: "var(--font-mono)", fontSize: 11 } as const;

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "3px 0", fontSize: 12 }}>
      <div style={{ ...muted, width: 210, flexShrink: 0 }}>{label}</div>
      <div style={{ flex: 1, minWidth: 0, wordBreak: "break-word" }}>{children}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--text-faint)", marginBottom: 4 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function downloadJson(provenance: Provenance, name: string) {
  const blob = new Blob([JSON.stringify(provenance, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${name.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-provenance.json`;
  link.click();
  URL.revokeObjectURL(url);
}

/**
 * How this project's results were produced: HyP3 parameters per batch, the pairing
 * rule, MintPy's effective options, the crop and the software versions. The same
 * record is saved next to the data (provenance.json) by every MintPy run.
 */
export function ProcessingDetails({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["provenance", projectId],
    queryFn: () => projectsApi.provenance(projectId),
    enabled: open,
    staleTime: 15_000,
  });

  const run = data?.last_mintpy_run ?? null;
  const options = run?.mintpy.options ?? null;
  const software = run?.software ?? data?.batches.find((b) => b.parameters)?.parameters?.software ?? null;

  return (
    <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
      <button
        className="btn btn-ghost btn-sm"
        style={{ padding: "0 4px", fontSize: 12 }}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? "▲" : "▼"} Processing details
      </button>
      <span style={{ ...muted, fontSize: 11, marginLeft: 8 }}>
        parameters, MintPy options and software versions behind these results
      </span>

      {open && isLoading && <div style={{ ...muted, fontSize: 12, marginTop: 10 }}>Loading…</div>}
      {open && isError && <div style={{ color: "var(--danger)", fontSize: 12, marginTop: 10 }}>Could not load the details.</div>}

      {open && data && (
        <div style={{ marginTop: 6 }}>
          <Section title="HyP3 batches">
            {data.batches.length === 0 && <div style={{ ...muted, fontSize: 12 }}>No batches yet.</div>}
            {data.batches.map((b) => (
              <Row key={b.id} label={b.label ?? "Batch"}>
                {b.parameters ? (
                  <span style={mono}>
                    looks {String(b.parameters.hyp3.looks)} · DEM {b.parameters.hyp3.include_dem ? "yes" : "no"} ·
                    {" "}{b.parameters.pairing.max_temporal_neighbors} temporal neighbors ·{" "}
                    {b.parameters.search.date_start} to {b.parameters.search.date_end} · {b.total_pairs} pairs
                  </span>
                ) : (
                  <span style={muted}>Submitted before parameters were recorded. HyP3 defaults applied at the time are not stored.</span>
                )}
              </Row>
            ))}
          </Section>

          <Section title="Last MintPy run">
            {!run && (
              <div style={{ ...muted, fontSize: 12 }}>
                {data.archived ? "The project is archived; restore it to see the run record." : "MintPy has not run yet."}
              </div>
            )}
            {run && (
              <>
                <Row label="Result">
                  {run.mintpy_run.status ?? "unknown"}
                  {run.mintpy_run.finished_at && <span style={muted}> · {new Date(run.mintpy_run.finished_at).toLocaleString()}</span>}
                  {run.mintpy_run.attempt && run.mintpy_run.attempt > 1 && <span style={muted}> · automatic restart {run.mintpy_run.attempt - 1}</span>}
                </Row>
                {run.mintpy_run.warnings?.map((w) => (
                  <Row key={w} label="Warning"><span style={{ color: "var(--warning)" }}>{w}</span></Row>
                ))}
                <Row label="Interferograms used"><span style={mono}>{run.interferograms.count}</span></Row>
                {run.results?.timeseries && (
                  <Row label="Series shown in the viewer">
                    <span style={mono}>{run.results.timeseries}</span>
                  </Row>
                )}
                {run.results?.reference && (
                  <Row label="Reference point">
                    <span style={mono}>
                      {run.results.reference.lat != null
                        ? `${run.results.reference.lat}, ${run.results.reference.lon}`
                        : `row ${run.results.reference.row}, column ${run.results.reference.column}`}
                      {run.results.reference.date ? ` · reference date ${run.results.reference.date}` : ""}
                    </span>
                    <span style={muted}>
                      {options && options["mintpy.reference.yx"] === "no" && options["mintpy.reference.lalo"] === "no"
                        ? " (picked by MintPy: the most coherent pixel)" : ""}
                    </span>
                  </Row>
                )}
                <Row label="Crop">
                  {run.crop.applied ? (
                    <span style={mono}>{run.crop.subset_lalo} <span style={muted}>(AOI plus {Math.round(run.crop.margin_fraction * 100)}% margin)</span></span>
                  ) : "none (full scene)"}
                </Row>
                {options ? (
                  KEY_OPTIONS.filter(([key]) => key in options).map(([key, label]) => (
                    <Row key={key} label={label}><span style={mono}>{options[key]}</span></Row>
                  ))
                ) : (
                  <Row label="MintPy options"><span style={muted}>Unavailable (MintPy was not importable when recorded).</span></Row>
                )}
                <div style={{ ...muted, fontSize: 11, marginTop: 4 }}>
                  Options not set by this app are MintPy's own defaults for its version; the full list is in the downloaded file.
                </div>
              </>
            )}
          </Section>

          {software && (
            <Section title="Software">
              <Row label="Versions">
                <span style={mono}>
                  {["python", "mintpy", "insar-core", "hyp3-sdk", "asf-search"]
                    .filter((k) => software[k])
                    .map((k) => `${k} ${software[k]}`)
                    .join(" · ")}
                </span>
              </Row>
            </Section>
          )}

          <button
            className="btn btn-secondary btn-sm"
            style={{ marginTop: 14 }}
            onClick={() => downloadJson(data, String(data.project.name ?? "project"))}
          >
            Download full record (JSON)
          </button>
        </div>
      )}
    </div>
  );
}
