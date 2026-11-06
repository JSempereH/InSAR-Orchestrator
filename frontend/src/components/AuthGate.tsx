import { ReactNode, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AUTH_REQUIRED_EVENT, authApi } from "../api/client";

/**
 * Shows the sign-in page instead of the app while the backend asks for a password
 * (APP_PASSWORD) and there is no valid session. With no password set it renders
 * the app straight away.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["auth-status"],
    queryFn: authApi.status,
    staleTime: 60_000,
    retry: false,
  });

  useEffect(() => {
    const onExpired = () => { void refetch(); };
    window.addEventListener(AUTH_REQUIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onExpired);
  }, [refetch]);

  if (isLoading) return null;
  // If the status call itself fails (backend down) show the app, which reports its own errors.
  if (data && data.required && !data.authenticated) {
    return <LoginPage onSignedIn={async () => { await qc.invalidateQueries(); await refetch(); }} />;
  }
  return <>{children}</>;
}

function LoginPage({ onSignedIn }: { onSignedIn: () => Promise<void> }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await authApi.login(password);
      await onSignedIn();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "Could not sign in");
      setBusy(false);
    }
  }

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 20 }}>
      <form className="card" onSubmit={submit} style={{ width: "min(380px, 100%)", padding: 28 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 22 }}>
          <img src="/logo.svg" alt="" width={40} height={40} style={{ borderRadius: 11, boxShadow: "var(--glow)" }} />
          <div>
            <div style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 15 }}>InSAR Orchestrator</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Sign in to continue</div>
          </div>
        </div>
        <div className="field" style={{ marginBottom: 14 }}>
          <label htmlFor="app-password">Password</label>
          <input
            id="app-password"
            className="input"
            type="password"
            autoFocus
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error && (
          <div style={{ padding: "8px 12px", marginBottom: 14, background: "var(--danger-light)", color: "var(--danger)", borderRadius: "var(--radius)", fontSize: 13 }}>
            {error}
          </div>
        )}
        <button className="btn btn-primary" type="submit" disabled={busy || !password} style={{ width: "100%" }}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
