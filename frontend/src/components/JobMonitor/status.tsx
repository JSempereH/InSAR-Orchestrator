import { CheckStatus } from "../../api/client";

// Green is success, amber a warning, red a failure: the palette's meanings.
export const STATUS_COLOR: Record<CheckStatus | "unknown", string> = {
  ok: "var(--success)",
  warn: "var(--warning)",
  fail: "var(--danger)",
  unknown: "var(--text-faint)",
};

export const STATUS_LABEL: Record<CheckStatus | "unknown", string> = {
  ok: "Looks good",
  warn: "Check this",
  fail: "Not reliable",
  unknown: "Not checked",
};

export function StatusDot({ status }: { status: CheckStatus | "unknown" }) {
  return (
    <span
      aria-label={status}
      style={{
        display: "inline-block", width: 8, height: 8, borderRadius: "50%", flexShrink: 0, marginTop: 5,
        background: STATUS_COLOR[status], boxShadow: status === "ok" ? "0 0 8px rgba(61,255,154,0.6)" : "none",
      }}
    />
  );
}

export function StatusPill({ status, text }: { status: CheckStatus | "unknown"; text?: string }) {
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: 6, padding: "2px 10px", borderRadius: 999, fontSize: 11,
        fontWeight: 700, color: STATUS_COLOR[status], border: `1px solid ${STATUS_COLOR[status]}`,
        background: "rgba(255,255,255,0.03)",
      }}
    >
      {text ?? STATUS_LABEL[status]}
    </span>
  );
}
