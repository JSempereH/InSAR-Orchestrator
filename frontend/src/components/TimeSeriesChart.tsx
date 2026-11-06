import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export interface TimeSeriesChartProps {
  dates: string[];
  valuesMm: number[];
  title?: string;
}

// "YYYYMMDD" -> "YYYY-MM-DD", so the x-axis and tooltip read as real dates
// instead of an 8-digit number.
function formatDate(raw: string): string {
  if (raw.length !== 8) return raw;
  return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
}

export function TimeSeriesChart({ dates, valuesMm, title }: TimeSeriesChartProps) {
  const data = dates.map((date, i) => ({ date: formatDate(date), displacement: valuesMm[i] }));

  return (
    <div className="card" style={{ padding: 20 }}>
      {title && <h3 style={{ marginBottom: 12 }}>{title}</h3>}
      <ResponsiveContainer width="100%" height={280}>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11, fill: "var(--text-muted)" }}
            minTickGap={40}
          />
          <YAxis
            tick={{ fontSize: 11, fill: "var(--text-muted)" }}
            label={{ value: "Displacement (mm)", angle: -90, position: "insideLeft", fontSize: 12, fill: "var(--text-muted)" }}
          />
          <Tooltip
            formatter={(value) => [`${Number(value).toFixed(2)} mm`, "Displacement"]}
            contentStyle={{ fontSize: 12, borderRadius: 10, border: "1px solid var(--border)", background: "#0e1211", color: "var(--text)" }}
            itemStyle={{ color: "var(--text)" }}
            labelStyle={{ color: "var(--text-muted)" }}
          />
          <Line type="monotone" dataKey="displacement" stroke="var(--accent)" strokeWidth={2} dot={false} isAnimationActive={false} style={{ filter: "drop-shadow(0 0 6px rgba(61,255,154,0.7))" }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
