import { useQuery } from "@tanstack/react-query";
import { ExportFormat, mintpyApi, validationApi } from "../../api/client";

const FORMATS: { format: ExportFormat; label: string; layer?: string; title: string }[] = [
  { format: "geotiff", label: "GeoTIFF", layer: "velocity", title: "Velocity map in mm/year, cloud optimised, opens in QGIS and ArcGIS" },
  { format: "netcdf", label: "NetCDF", title: "Velocity, quality layers and the displacement cube, CF conventions (xarray, QGIS)" },
  { format: "csv", label: "CSV", title: "One row per pixel with velocity, quality and a column per date" },
  { format: "gpkg", label: "GeoPackage", title: "Points with velocity and the series, for a GIS or a database" },
];

/** Download the results in formats other tools read. The server builds each file once and reuses it. */
export function ExportMenu({ projectId }: { projectId: string }) {
  // The two calibrated layers exist only once a validation has justified them, so they are offered only then.
  const { data: validation } = useQuery({
    queryKey: ["validation", projectId],
    queryFn: () => validationApi.status(projectId),
  });
  const gnss = validation?.gnss;
  const egms = validation?.egms;
  const shift = gnss?.status === "SUCCEEDED" && !gnss.stale && gnss.result?.calibration?.applied
    ? gnss.result.calibration.offset_mm_yr : undefined;
  const scale = egms?.status === "SUCCEEDED" && !egms.stale ? egms.result?.uncertainty?.recommended_scale : undefined;

  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600 }}>Report</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
        One page with the data, the processing, the quality checks, the comparison with EGMS and GNSS, the limits of the
        result and what to cite. Print it to PDF from the browser, or save the page to attach to a paper.
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8, marginBottom: 14 }}>
        <a className="btn btn-primary btn-sm" target="_blank" rel="noreferrer" href={mintpyApi.reportUrl(projectId)}>Open the report</a>
        <a className="btn btn-secondary btn-sm" download href={mintpyApi.reportUrl(projectId, true)}>Save as HTML</a>
      </div>
      <div style={{ fontSize: 13, fontWeight: 600 }}>Export</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
        Millimetres, positive towards the satellite, relative to the reference pixel and date, which are written inside each file.
        Large areas can take a minute to prepare.
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
        {FORMATS.map(({ format, label, layer, title }) => (
          <a key={format} className="btn btn-secondary btn-sm" title={title} download
             href={mintpyApi.exportUrl(projectId, format, { layer })}>
            {label}
          </a>
        ))}
        <a className="btn btn-secondary btn-sm" download
           title="Precision of each pixel's velocity relative to the reference point, from how the noise of the dates grows with distance. A lower bound of the error"
           href={mintpyApi.exportUrl(projectId, "geotiff", { layer: "velocity_precision" })}>
          GeoTIFF precision
        </a>
        {shift !== undefined && (
          <a className="btn btn-secondary btn-sm" download
             title={`Velocity plus the ${shift >= 0 ? "+" : ""}${shift.toFixed(1)} mm/year shift that ties it to the GNSS stations in the area`}
             href={mintpyApi.exportUrl(projectId, "geotiff", { layer: "velocity_calibrated" })}>
            GeoTIFF tied to GNSS
          </a>
        )}
        {scale !== undefined && (
          <a className="btn btn-secondary btn-sm" download
             title={`The error MintPy states, multiplied by ${scale.toFixed(2)}, the factor found against EGMS`}
             href={mintpyApi.exportUrl(projectId, "geotiff", { layer: "velocity_std_calibrated" })}>
            GeoTIFF realistic error
          </a>
        )}
      </div>
    </div>
  );
}
