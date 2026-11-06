import { useMemo, useRef, useState } from "react";
import { CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";
import type { TimeSeries, TimeSeriesAnalysis } from "../../api/client";
import { useElementSize } from "./useElementSize";

interface Props {
  series: TimeSeries;
}

interface ChartRow { ts: number; iso: string; value: number; trend?: number; fit?: number }

type Layer = "trend" | "fit" | "extremes" | "outliers" | "change";
const LAYERS: { id: Layer; label: string; hint: string }[] = [
  { id: "trend", label: "Trend", hint: "Fitted trend without the seasonal cycle" },
  { id: "fit", label: "Seasonal fit", hint: "Trend plus the annual cycle" },
  { id: "extremes", label: "Max / min", hint: "Highest and lowest displacement" },
  { id: "outliers", label: "Outliers", hint: "Observations far from the fit" },
  { id: "change", label: "Change point", hint: "Where the trend or level changes" },
];

const ts = (iso: string) => Date.parse(iso + "T00:00:00Z");
const fmt = (v: number, d = 1) => (v >= 0 ? "+" : "-") + Math.abs(v).toFixed(d);
// "YYYYMMDD" or ISO to "YYYY-MM-DD"
const toIso = (raw: string) => {
  const d = raw.replace(/\D/g, "").slice(0, 8);
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
};
const pText = (p: number) => (p < 0.001 ? "<0.001" : p.toFixed(3));

function evidence(gain: number): string {
  if (gain >= 10) return "very strong evidence";
  if (gain >= 6) return "strong evidence";
  return "";
}

export function TimeSeriesAnalysisView({ series }: Props) {
  const [layers, setLayers] = useState<Record<Layer, boolean>>({ trend: true, fit: false, extremes: true, outliers: true, change: true });

  // The backend computes the statistics (and the overlays); without them the plain series is drawn.
  const analysis: TimeSeriesAnalysis | null = series.analysis ?? null;

  const data = useMemo<ChartRow[]>(() => {
    if (analysis) {
      const { dates, values, trend, fit } = analysis.series;
      return dates.map((iso, k) => ({ ts: ts(iso), iso, value: values[k], trend: trend[k], fit: fit[k] }));
    }
    return series.dates
      .map((d, i) => ({ iso: toIso(d), value: series.values_mm[i] }))
      .filter((p) => Number.isFinite(p.value))
      .sort((a, b) => a.iso.localeCompare(b.iso))
      .map((p) => ({ ts: ts(p.iso), iso: p.iso, value: p.value, trend: undefined, fit: undefined }));
  }, [series, analysis]);

  const chartBox = useRef<HTMLDivElement>(null);
  const { width, height } = useElementSize(chartBox);

  const xTicks = useMemo(() => {
    if (!data.length) return [];
    const a = data[0].ts, b = data[data.length - 1].ts, k = width < 420 ? 4 : 6;
    return Array.from({ length: k + 1 }, (_, i) => a + ((b - a) * i) / k);
  }, [data, width]);

  const toggle = (id: Layer) => setLayers((s) => ({ ...s, [id]: !s[id] }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, minHeight: "100%" }}>
      {analysis && <Summary a={analysis} />}

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} role="group" aria-label="Chart layers">
        {LAYERS.map((l) => (
          <button
            key={l.id}
            title={l.hint}
            aria-pressed={layers[l.id]}
            onClick={() => toggle(l.id)}
            style={{
              fontFamily: "var(--font)", fontSize: 11, fontWeight: 500, padding: "3px 10px", borderRadius: 999, cursor: "pointer",
              color: layers[l.id] ? "var(--primary-text)" : "var(--text-muted)",
              background: layers[l.id] ? "var(--primary-light)" : "transparent",
              border: `1px solid ${layers[l.id] ? "var(--primary-border)" : "var(--border)"}`,
            }}
          >
            {l.label}
          </button>
        ))}
      </div>

      <div ref={chartBox} style={{ flex: "1 1 240px", minHeight: 200, minWidth: 0, position: "relative" }}>
        {width > 0 && height > 0 && (
          <ComposedChart width={width} height={height} data={data} margin={{ top: 22, right: 14, bottom: 4, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
            <XAxis
              dataKey="ts" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={xTicks}
              tickFormatter={(v: number) => new Date(v).toISOString().slice(0, 7)}
              tick={{ fontSize: 10, fill: "var(--text-muted)" }}
            />
            <YAxis
              tick={{ fontSize: 10, fill: "var(--text-muted)" }} width={44} domain={["auto", "auto"]}
              label={{ value: "mm", angle: -90, position: "insideLeft", fontSize: 10, fill: "var(--text-muted)" }}
            />
            <Tooltip
              contentStyle={{ fontSize: 12, borderRadius: 10, border: "1px solid var(--border)", background: "#0e1211", color: "var(--text)" }}
              labelStyle={{ color: "var(--text-muted)" }}
              labelFormatter={(v) => new Date(Number(v)).toISOString().slice(0, 10)}
              formatter={(v, name) => [`${Number(v).toFixed(2)} mm`, String(name)]}
            />
            {layers.fit && analysis?.seasonal.present && (
              <Line type="monotone" dataKey="fit" name="Seasonal fit" stroke="#00c2a8" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            )}
            <Line
              type="monotone" dataKey="value" name="Displacement" stroke="var(--accent)" strokeWidth={2} dot={false} isAnimationActive={false}
              style={{ filter: "drop-shadow(0 0 5px rgba(61,255,154,0.6))" }}
            />
            {layers.trend && analysis && (
              <Line type="linear" dataKey="trend" name="Trend" stroke="#ffffff" strokeOpacity={0.75} strokeDasharray="6 5" strokeWidth={1.6} dot={false} isAnimationActive={false} />
            )}
            {layers.change && analysis?.model.break_date && (
              <ReferenceLine
                x={ts(analysis.model.break_date)} stroke="#ffb020" strokeDasharray="4 4"
                label={{ value: analysis.model.break_date, position: "insideTopRight", fontSize: 10, fill: "#ffb020" }}
              />
            )}
            {layers.extremes && analysis && (
              <>
                <ReferenceDot x={ts(analysis.max.date)} y={analysis.max.value} r={5} fill="#3dff9a" stroke="#fff" strokeWidth={1.5}
                  label={{ value: `max ${analysis.max.value.toFixed(1)}`, position: "top", fontSize: 10, fill: "var(--text)" }} />
                <ReferenceDot x={ts(analysis.min.date)} y={analysis.min.value} r={5} fill="#ff5d6c" stroke="#fff" strokeWidth={1.5}
                  label={{ value: `min ${analysis.min.value.toFixed(1)}`, position: "bottom", fontSize: 10, fill: "var(--text)" }} />
              </>
            )}
            {layers.outliers && analysis?.noise.outliers.slice(0, 25).map((o) => (
              <ReferenceDot key={o.date} x={ts(o.date)} y={o.value} r={5} fill="none" stroke="#ffb020" strokeWidth={1.8} />
            ))}
          </ComposedChart>
        )}
      </div>

      {analysis ? <StatCards a={analysis} /> : (
        <div style={{ fontSize: 12, color: "var(--warning)" }}>
          At least 8 observations are needed for the statistical summary. This series has {data.length}.
        </div>
      )}

    </div>
  );
}

function Summary({ a }: { a: TimeSeriesAnalysis }) {
  const tone = a.model.kind === "stable" ? "var(--text-muted)" : a.model.kind === "linear" ? "var(--accent)" : "var(--warning)";
  const ev = evidence(a.model.bic_gain);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{
          fontSize: 12, fontWeight: 600, padding: "3px 11px", borderRadius: 999, color: tone,
          border: `1px solid ${tone}`, background: "rgba(255,255,255,0.04)",
        }}>
          {a.model.label}
        </span>
        {ev && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{ev}</span>}
      </div>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, lineHeight: 1.55, color: "var(--text)" }}>
        {a.narrative.map((s) => <li key={s}>{s}</li>)}
      </ul>
    </div>
  );
}

function StatCards({ a }: { a: TimeSeriesAnalysis }) {
  const linearOnly = a.model.kind === "linear";
  const cards: { label: string; value: string; sub: string; hint?: string }[] = [
    {
      label: linearOnly ? "Velocity" : "Mean velocity",
      value: `${fmt(a.velocity.ols_mm_yr)} mm/yr`,
      sub: `95% CI ${fmt(a.velocity.ci95[0])} to ${fmt(a.velocity.ci95[1])}`,
      hint: "Straight-line fit over the whole record. The interval is widened for autocorrelated residuals.",
    },
    {
      label: "Robust slope",
      value: `${fmt(a.velocity.theil_sen_mm_yr)} mm/yr`,
      sub: `Mann-Kendall p ${pText(a.velocity.mann_kendall_p)}`,
      hint: "Theil-Sen median slope, insensitive to outliers. Mann-Kendall tests whether a monotonic trend exists.",
    },
    { label: "Net change", value: `${fmt(a.net_change_mm)} mm`, sub: `over ${a.span_years.toFixed(1)} years (trend)`, hint: "Change of the fitted trend between the first and last date." },
    { label: "Maximum", value: `${a.max.value.toFixed(1)} mm`, sub: a.max.date },
    { label: "Minimum", value: `${a.min.value.toFixed(1)} mm`, sub: a.min.date },
    {
      label: "Seasonality",
      value: a.seasonal.present ? `${(2 * a.seasonal.amplitude_mm).toFixed(1)} mm` : "None found",
      sub: a.seasonal.present ? `peak to peak, high in ${new Date(2000, a.seasonal.peak_month, 1).toLocaleString("en", { month: "long" })}` : a.span_years < 1.5 ? "record under 1.5 years" : "no annual cycle detected",
    },
    {
      label: "Noise",
      value: `${a.noise.rms_mm.toFixed(1)} mm`,
      sub: `RMS of residuals, ${a.noise.outliers.length} outlier${a.noise.outliers.length === 1 ? "" : "s"}`,
      hint: "Scatter of the data around the selected model.",
    },
    {
      label: "Sampling",
      value: `${a.n} dates`,
      sub: `every ${a.median_step_days.toFixed(0)} d, ${a.gaps} gap${a.gaps === 1 ? "" : "s"}`,
    },
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(132px, 1fr))", gap: 8 }}>
      {cards.map((c) => (
        <div key={c.label} title={c.hint} style={{ padding: "8px 10px", borderRadius: 10, border: "1px solid var(--border)", background: "rgba(255,255,255,0.03)" }}>
          <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>{c.label}</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 15, fontWeight: 600, marginTop: 2 }}>{c.value}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 1 }}>{c.sub}</div>
        </div>
      ))}
    </div>
  );
}
