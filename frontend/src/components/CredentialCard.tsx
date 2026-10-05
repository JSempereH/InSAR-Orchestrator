import { ReactNode, useState } from "react";

export type SaveState = "idle" | "saving" | "saved" | "error";

/**
 * Shared save/saving/saved/error state + handler for a credential form -
 * was duplicated (as separate useState triples) between the Earthdata and
 * EGMS blocks in Settings.tsx.
 */
export function useCredentialSave(save: () => Promise<void>) {
  const [state, setState] = useState<SaveState>("idle");
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setState("saving");
    setError(null);
    try {
      await save();
      setState("saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
      setState("error");
    }
  }

  function resetToIdle() {
    setState("idle");
    setError(null);
  }

  return { state, error, handleSave, resetToIdle };
}

interface CredentialCardProps {
  icon: string;
  iconBg: string;
  title: string;
  description: ReactNode;
  children: ReactNode;
  onSave: () => void;
  saveLabel: string;
  savingLabel: string;
  saveDisabled: boolean;
  state: SaveState;
  error?: string | null;
}

/**
 * One provider's credential card: icon + title + description header, the
 * caller's own field(s) as children, and a save button with saving/saved/
 * error feedback - the chrome that was duplicated around each of Settings
 * page's credential forms.
 */
export function CredentialCard({
  icon, iconBg, title, description, children, onSave, saveLabel, savingLabel, saveDisabled, state, error,
}: CredentialCardProps) {
  return (
    <div className="card" style={{ padding: 24, marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 14, marginBottom: 20 }}>
        <div style={{
          width: 40, height: 40, borderRadius: 10, flexShrink: 0,
          background: iconBg,
          display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20,
        }}>
          {icon}
        </div>
        <div>
          <h3 style={{ marginBottom: 4 }}>{title}</h3>
          <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)" }}>{description}</p>
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {children}

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button
            className="btn btn-primary"
            onClick={onSave}
            disabled={saveDisabled || state === "saving"}
          >
            {state === "saving" ? savingLabel : saveLabel}
          </button>
          {state === "saved" && (
            <span style={{ color: "var(--success)", fontSize: 13, fontWeight: 500 }}>✓ Saved</span>
          )}
          {state === "error" && (
            <span style={{ color: "var(--danger)", fontSize: 13 }}>{error ?? "Failed to save"}</span>
          )}
        </div>
      </div>
    </div>
  );
}
