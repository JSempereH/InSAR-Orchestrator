import { useRef, type ReactNode, type RefObject } from "react";

import type { PanelConfig } from "./panelConfig";

const MIN_W = 340, MIN_H = 220, HEADER_H = 40;

interface Props {
  title: string;
  config: PanelConfig;
  onChange: (c: PanelConfig) => void;
  /** The element the panel lives in and is confined to. */
  boundsRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onDetach: () => void;
  children: ReactNode;
}

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));

function Icon({ d }: { d: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

const iconButton: React.CSSProperties = {
  display: "grid", placeItems: "center", width: 26, height: 26, borderRadius: 7, border: "1px solid transparent",
  background: "transparent", color: "var(--text-muted)", cursor: "pointer", padding: 0,
};

export function FloatingPanel({ title, config, onChange, boundsRef, onClose, onDetach, children }: Props) {
  const drag = useRef<{ dx: number; dy: number } | null>(null);
  const resize = useRef<{ w: number; h: number; cx: number; cy: number } | null>(null);

  const bounds = () => {
    const el = boundsRef.current;
    return { w: el?.clientWidth ?? 900, h: el?.clientHeight ?? 600, left: el?.getBoundingClientRect().left ?? 0, top: el?.getBoundingClientRect().top ?? 0 };
  };

  // Geometry as displayed (clamped to the stage), used as the starting point of a drag or resize.
  const shown = () => {
    const bb = bounds();
    const sw = Math.min(config.w, Math.max(MIN_W, bb.w - 16));
    const sx = clamp(config.x, 0, Math.max(0, bb.w - sw));
    const sy = clamp(config.y, 0, Math.max(0, bb.h - HEADER_H));
    return { x: sx, y: sy, w: sw, h: Math.min(config.h, Math.max(MIN_H, bb.h - sy - 8)) };
  };

  function startDrag(e: React.PointerEvent<HTMLDivElement>) {
    if (config.mode === "max" || (e.target as HTMLElement).closest("button")) return;
    const b = bounds();
    drag.current = { dx: e.clientX - b.left - shown().x, dy: e.clientY - b.top - shown().y };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function moveDrag(e: React.PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    const b = bounds();
    const cur = shown();
    onChange({
      ...config, w: cur.w, h: cur.h,
      x: clamp(e.clientX - b.left - drag.current.dx, 0, Math.max(0, b.w - cur.w)),
      y: clamp(e.clientY - b.top - drag.current.dy, 0, Math.max(0, b.h - HEADER_H)),
    });
  }
  const endDrag = () => { drag.current = null; };

  function startResize(e: React.PointerEvent<HTMLDivElement>) {
    resize.current = { w: shown().w, h: shown().h, cx: e.clientX, cy: e.clientY };
    e.currentTarget.setPointerCapture(e.pointerId);
    e.stopPropagation();
  }
  function moveResize(e: React.PointerEvent<HTMLDivElement>) {
    if (!resize.current) return;
    const b = bounds(), cur = shown();
    onChange({
      ...config, x: cur.x, y: cur.y,
      w: clamp(resize.current.w + e.clientX - resize.current.cx, MIN_W, Math.max(MIN_W, b.w - cur.x)),
      h: clamp(resize.current.h + e.clientY - resize.current.cy, MIN_H, Math.max(MIN_H, b.h - cur.y)),
    });
  }
  const endResize = () => { resize.current = null; };

  function preset(fw: number, fh: number) {
    const b = bounds();
    const w = clamp(Math.round(b.w * fw), MIN_W, b.w), h = clamp(Math.round(b.h * fh), MIN_H, b.h);
    onChange({ ...config, mode: "normal", w, h, x: clamp(config.x, 0, b.w - w), y: clamp(config.y, 0, b.h - Math.min(h, HEADER_H)) });
  }

  // Keep the panel inside the stage even if the stage shrank since the settings
  // were saved (exiting full screen, smaller window, a stale saved size).
  const b = bounds();
  const w = Math.min(config.w, Math.max(MIN_W, b.w - 16));
  const x = clamp(config.x, 0, Math.max(0, b.w - w));
  const y = clamp(config.y, 0, Math.max(0, b.h - HEADER_H));
  const h = Math.min(config.h, Math.max(MIN_H, b.h - y - 8));

  const style: React.CSSProperties =
    config.mode === "max"
      ? { left: 8, top: 8, right: 8, bottom: 8 }
      : { left: x, top: y, width: w, height: config.mode === "min" ? HEADER_H : h };

  return (
    <div
      role="dialog"
      aria-label={title}
      style={{
        position: "absolute", zIndex: 30, display: "flex", flexDirection: "column", overflow: "hidden",
        background: "rgba(10,14,13,0.95)", backdropFilter: "blur(14px)", border: "1px solid var(--border)",
        borderRadius: 14, boxShadow: "0 24px 60px -16px rgba(0,0,0,0.85), 0 0 0 1px rgba(61,255,154,0.08)", ...style,
      }}
    >
      <div
        onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={endDrag}
        onDoubleClick={() => onChange({ ...config, mode: config.mode === "max" ? "normal" : "max" })}
        style={{
          height: HEADER_H, flexShrink: 0, display: "flex", alignItems: "center", gap: 6, padding: "0 8px 0 14px",
          borderBottom: config.mode === "min" ? "none" : "1px solid var(--border)", cursor: config.mode === "max" ? "default" : "grab", touchAction: "none", userSelect: "none",
        }}
      >
        <span style={{ flex: 1, fontSize: 12, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{title}</span>
        {(["S", "M", "L"] as const).map((k) => (
          <button
            key={k} style={{ ...iconButton, fontSize: 10, fontWeight: 600, fontFamily: "var(--font-mono)" }} aria-label={`Size ${k}`}
            title={k === "S" ? "Small" : k === "M" ? "Medium" : "Large"}
            onClick={() => (k === "S" ? preset(0.34, 0.5) : k === "M" ? preset(0.5, 0.8) : preset(0.8, 0.97))}
          >
            {k}
          </button>
        ))}
        <button style={iconButton} aria-label={config.mode === "min" ? "Restore" : "Minimize"} title={config.mode === "min" ? "Restore" : "Minimize"}
          onClick={() => onChange({ ...config, mode: config.mode === "min" ? "normal" : "min" })}>
          <Icon d={config.mode === "min" ? "M6 15l6-6 6 6" : "M6 12h12"} />
        </button>
        <button style={iconButton} aria-label={config.mode === "max" ? "Restore" : "Maximize"} title={config.mode === "max" ? "Restore" : "Maximize"}
          onClick={() => onChange({ ...config, mode: config.mode === "max" ? "normal" : "max" })}>
          <Icon d={config.mode === "max" ? "M8 3v5H3m13-5v5h5M8 21v-5H3m13 5v-5h5" : "M4 4h16v16H4z"} />
        </button>
        <button style={iconButton} aria-label="Open in a separate window" title="Open in a separate window (move it to another screen)" onClick={onDetach}>
          <Icon d="M14 4h6v6m0-6-9 9M10 6H5a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5" />
        </button>
        <button style={iconButton} aria-label="Close" title="Close" onClick={onClose}>
          <Icon d="M6 6l12 12M18 6 6 18" />
        </button>
      </div>

      {config.mode !== "min" && <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 14 }}>{children}</div>}

      {config.mode === "normal" && (
        <div
          onPointerDown={startResize} onPointerMove={moveResize} onPointerUp={endResize} onPointerCancel={endResize}
          title="Drag to resize" aria-label="Resize"
          style={{
            position: "absolute", right: 0, bottom: 0, width: 18, height: 18, cursor: "nwse-resize", touchAction: "none",
            background: "linear-gradient(135deg, transparent 50%, rgba(61,255,154,0.55) 50%, rgba(61,255,154,0.55) 58%, transparent 58%, transparent 72%, rgba(61,255,154,0.55) 72%, rgba(61,255,154,0.55) 80%, transparent 80%)",
          }}
        />
      )}
    </div>
  );
}
