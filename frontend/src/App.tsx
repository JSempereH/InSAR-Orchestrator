import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { authApi } from "./api/client";
import { AuthGate } from "./components/AuthGate";
import { DashboardPage } from "./pages/Dashboard";
import { GroundMotionPage } from "./pages/GroundMotion";
import { DownloadsPage } from "./pages/Downloads";
import { SettingsPage } from "./pages/Settings";
import { ViewerPage } from "./pages/Viewer";
import "./App.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Without this, staleTime defaults to 0: every query refetches on
      // every mount and window focus, even ones that already have their own
      // explicit refetchInterval elsewhere for "this needs to be live."
      // Most data here (projects, batches, credentials) doesn't change from
      // second to second, so a short baseline avoids that redundant chatter
      // without meaningfully delaying anything a user is actively waiting on.
      staleTime: 10_000,
    },
  },
});

type Page = "dashboard" | "ground-motion" | "viewer" | "downloads" | "settings";

function NavIcon({ d }: { d: string }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

const NAV: { id: Page; icon: string; label: string }[] = [
  { id: "dashboard",     icon: "M12 2 4 6v6c0 5 3.4 8.6 8 10 4.6-1.4 8-5 8-10V6l-8-4Zm0 6v8m-4-4h8", label: "ASF" },
  { id: "ground-motion", icon: "M3 17c3-8 6 4 9-3s6-6 9-8", label: "EGMS" },
  { id: "viewer",        icon: "M9 3 3 6v15l6-3 6 3 6-3V3l-6 3-6-3Zm0 0v15m6-12v15", label: "Viewer" },
  { id: "downloads",     icon: "M12 3v12m0 0-4-4m4 4 4-4M4 20h16", label: "Downloads" },
  { id: "settings",      icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm0-13v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2m0-14-2 2M7 17l-2 2", label: "Settings" },
];

function SignOutButton() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["auth-status"], queryFn: authApi.status, staleTime: 60_000, retry: false });
  if (!data?.required) return null;
  return (
    <div style={{ padding: 8, borderTop: "1px solid var(--sidebar-border)" }}>
      <button
        className="btn btn-ghost btn-sm"
        style={{ width: "100%", justifyContent: "flex-start" }}
        onClick={async () => { await authApi.logout(); await qc.invalidateQueries({ queryKey: ["auth-status"] }); }}
      >
        Sign out
      </button>
    </div>
  );
}

function App() {
  const [page, setPage] = useState<Page>("dashboard");
  // Dataset to preselect when the viewer opens from another page ("View on map").
  const [viewerTarget, setViewerTarget] = useState<string | null>(null);

  function openViewer(datasetId: string) {
    setViewerTarget(datasetId);
    setPage("viewer");
  }

  return (
    <QueryClientProvider client={queryClient}>
      <AuthGate>
      <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>

        {/* ── Sidebar ───────────────────────────────────── */}
        <nav style={{
          width: 220,
          background: "var(--sidebar-bg)",
          backdropFilter: "blur(14px)",
          display: "flex",
          flexDirection: "column",
          flexShrink: 0,
          borderRight: "1px solid var(--sidebar-border)",
        }}>
          {/* Brand */}
          <div style={{
            padding: "20px 16px 18px",
            borderBottom: "1px solid var(--sidebar-border)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}>
            <img
              src="/logo.svg"
              alt=""
              width={34}
              height={34}
              style={{ flexShrink: 0, borderRadius: 10, boxShadow: "var(--glow)" }}
            />
            <div>
              <div style={{ color: "var(--text)", fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 13, lineHeight: 1.2 }}>
                InSAR Orchestrator
              </div>
              <div style={{ color: "var(--text-faint)", fontFamily: "var(--font-mono)", fontSize: 11 }}>v0.2.0</div>
            </div>
          </div>

          {/* Nav items */}
          <div style={{ flex: 1, padding: "8px 8px 0" }}>
            {NAV.map(({ id, icon, label }) => {
              const active = page === id;
              return (
                <button
                  key={id}
                  onClick={() => { if (id === "viewer") setViewerTarget(null); setPage(id); }}
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "9px 12px",
                    marginBottom: 2,
                    background: active ? "var(--sidebar-active)" : "transparent",
                    border: active ? "1px solid var(--primary-border)" : "1px solid transparent",
                    boxShadow: active ? "0 0 18px rgba(61,255,154,0.15)" : "none",
                    borderRadius: 10,
                    color: active ? "var(--accent)" : "var(--sidebar-text)",
                    fontSize: 13,
                    fontWeight: active ? 600 : 400,
                    fontFamily: "var(--font)",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                  onMouseEnter={(e) => {
                    if (!active) (e.currentTarget as HTMLElement).style.background = "var(--sidebar-hover)";
                  }}
                  onMouseLeave={(e) => {
                    if (!active) (e.currentTarget as HTMLElement).style.background = "transparent";
                  }}
                >
                  <NavIcon d={icon} />
                  {label}
                </button>
              );
            })}
          </div>

          <SignOutButton />
        </nav>

        {/* ── Main ──────────────────────────────────────── */}
        <main style={{ flex: 1, overflowY: "auto", background: "var(--bg)" }}>
          {page === "dashboard"      && <DashboardPage onOpenViewer={openViewer} />}
          {page === "ground-motion"  && <GroundMotionPage onGoToSettings={() => setPage("settings")} />}
          {page === "viewer"         && <ViewerPage key={viewerTarget ?? "none"} initialId={viewerTarget} onGoTo={setPage} />}
          {page === "downloads"      && <DownloadsPage onOpenViewer={openViewer} />}
          {page === "settings"       && <SettingsPage />}
        </main>
      </div>
      </AuthGate>
    </QueryClientProvider>
  );
}

export default App;
