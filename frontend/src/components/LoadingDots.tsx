interface LoadingDotsProps {
  label?: string;
}

// Three dots pulsing in sequence, so a slow request (parsing a multi-GB EGMS
// file, or reading a MintPy .h5, can take tens of seconds) reads as "still
// working" instead of looking frozen. Reuses the "pulse" keyframe already
// defined in index.css for the app's other in-progress indicators (see the
// download-progress banners in Dashboard.tsx/JobTable.tsx), just staggered
// across three dots instead of one.
export function LoadingDots({ label }: LoadingDotsProps) {
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }} role="status" aria-live="polite">
      {[0, 0.15, 0.3].map((delay) => (
        <div
          key={delay}
          style={{
            width: 6, height: 6, borderRadius: "50%",
            background: "var(--primary)",
            animation: "pulse 1s ease-in-out infinite",
            animationDelay: `${delay}s`,
          }}
        />
      ))}
      {label && <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: 2 }}>{label}</span>}
    </div>
  );
}
