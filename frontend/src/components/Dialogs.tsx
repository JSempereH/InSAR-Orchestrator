import {
  createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";

/**
 * In-app replacements for window.confirm / window.alert, plus toasts, so every
 * prompt follows the project's black and green glass look instead of the
 * browser's own dialog.
 *
 *   const { confirm, ask, toast } = useDialogs();
 *   if (!(await confirm({ title: "Delete project?", danger: true }))) return;
 */

type Variant = "primary" | "danger" | "ghost";

export interface DialogButton<T> {
  label: string;
  value: T;
  variant?: Variant;
}

interface AskOptions<T> {
  title: string;
  message?: ReactNode;
  buttons: DialogButton<T>[];
  /** Value returned when the dialog is dismissed with Escape or a click outside. */
  dismissValue: T;
}

interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

export type ToastTone = "success" | "info" | "error";

interface ToastOptions {
  title?: string;
  message: ReactNode;
  tone?: ToastTone;
  actionLabel?: string;
  onAction?: () => void;
  durationMs?: number;
}

interface DialogsApi {
  ask: <T>(options: AskOptions<T>) => Promise<T>;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
  toast: (options: ToastOptions) => void;
}

const DialogsContext = createContext<DialogsApi | null>(null);

export function useDialogs(): DialogsApi {
  const ctx = useContext(DialogsContext);
  if (!ctx) throw new Error("useDialogs must be used inside <DialogsProvider>");
  return ctx;
}

interface OpenDialog {
  options: AskOptions<unknown>;
  resolve: (value: unknown) => void;
}

interface ToastItem extends ToastOptions {
  id: number;
}

const TONE_COLOR: Record<ToastTone, string> = {
  success: "var(--accent)",
  info: "var(--info)",
  error: "var(--danger)",
};

export function DialogsProvider({ children }: { children: ReactNode }) {
  const [dialog, setDialog] = useState<OpenDialog | null>(null);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const ask = useCallback(<T,>(options: AskOptions<T>) => {
    return new Promise<T>((resolve) => {
      setDialog((previous) => {
        // A second dialog replaces the first, which is treated as dismissed.
        previous?.resolve(previous.options.dismissValue);
        return { options: options as AskOptions<unknown>, resolve: resolve as (v: unknown) => void };
      });
    });
  }, []);

  const confirm = useCallback(
    (o: ConfirmOptions) =>
      ask<boolean>({
        title: o.title,
        message: o.message,
        dismissValue: false,
        buttons: [
          { label: o.cancelLabel ?? "Cancel", value: false, variant: "ghost" },
          { label: o.confirmLabel ?? "Confirm", value: true, variant: o.danger ? "danger" : "primary" },
        ],
      }),
    [ask],
  );

  const dismissToast = useCallback((id: number) => {
    setToasts((items) => items.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback(
    (options: ToastOptions) => {
      const id = nextId.current++;
      setToasts((items) => [...items, { ...options, id }]);
      window.setTimeout(() => dismissToast(id), options.durationMs ?? 9000);
    },
    [dismissToast],
  );

  function close(value: unknown) {
    if (!dialog) return;
    dialog.resolve(value);
    setDialog(null);
  }

  const api = useMemo(() => ({ ask, confirm, toast }), [ask, confirm, toast]);

  return (
    <DialogsContext.Provider value={api}>
      {children}
      {dialog && <Modal dialog={dialog} onClose={close} />}
      <div
        style={{
          position: "fixed", right: 20, bottom: 20, zIndex: 1100,
          display: "flex", flexDirection: "column", gap: 10, maxWidth: 380,
        }}
      >
        {toasts.map((t) => (
          <ToastCard key={t.id} toast={t} onDismiss={() => dismissToast(t.id)} />
        ))}
      </div>
    </DialogsContext.Provider>
  );
}

const glass: React.CSSProperties = {
  background: "linear-gradient(180deg, rgba(255,255,255,0.07), rgba(255,255,255,0.015)), var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 16,
  boxShadow: "0 18px 50px rgba(0,0,0,0.6)",
};

function Modal({ dialog, onClose }: { dialog: OpenDialog; onClose: (value: unknown) => void }) {
  const { options } = dialog;
  const lastButton = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    lastButton.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(options.dismissValue);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [options, onClose]);

  return (
    <div
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(options.dismissValue); }}
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(0,0,0,0.62)", backdropFilter: "blur(4px)",
        display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={options.title}
        style={{ ...glass, width: "min(480px, 100%)", padding: 24, position: "relative", overflow: "hidden" }}
      >
        <div
          style={{
            position: "absolute", top: 0, left: 0, right: 0, height: 1,
            background: "linear-gradient(90deg, transparent, rgba(61,255,154,0.55), transparent)",
          }}
        />
        <h3 style={{ margin: "0 0 10px", fontFamily: "var(--font-display)" }}>{options.title}</h3>
        {options.message && (
          <div style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.55, whiteSpace: "pre-line" }}>
            {options.message}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap", marginTop: 22 }}>
          {options.buttons.map((b, i) => (
            <button
              key={b.label}
              ref={i === options.buttons.length - 1 ? lastButton : undefined}
              className={`btn ${b.variant === "primary" ? "btn-primary" : b.variant === "danger" ? "btn-danger" : "btn-ghost"}`}
              onClick={() => onClose(b.value)}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: () => void }) {
  const color = TONE_COLOR[toast.tone ?? "success"];
  return (
    <div
      role="status"
      style={{
        ...glass, borderRadius: 12, padding: "12px 14px", borderLeft: `3px solid ${color}`,
        boxShadow: `0 12px 36px rgba(0,0,0,0.55), 0 0 22px ${color === "var(--accent)" ? "rgba(61,255,154,0.12)" : "transparent"}`,
        display: "flex", gap: 12, alignItems: "flex-start",
      }}
    >
      <div style={{ flex: 1, fontSize: 13 }}>
        {toast.title && <div style={{ fontWeight: 600, marginBottom: 2, color }}>{toast.title}</div>}
        <div style={{ color: "var(--text)", lineHeight: 1.45 }}>{toast.message}</div>
        {toast.actionLabel && (
          <button
            className="btn btn-secondary btn-sm"
            style={{ marginTop: 8 }}
            onClick={() => { toast.onAction?.(); onDismiss(); }}
          >
            {toast.actionLabel}
          </button>
        )}
      </div>
      <button
        className="btn btn-ghost btn-sm"
        aria-label="Dismiss"
        style={{ padding: "0 6px", color: "var(--text-faint)" }}
        onClick={onDismiss}
      >
        ×
      </button>
    </div>
  );
}
