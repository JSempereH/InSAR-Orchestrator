import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { DashboardPage } from "./pages/Dashboard";
import { ProjectsPage } from "./pages/Projects";
import { GroundMotionPage } from "./pages/GroundMotion";
import { DownloadsPage } from "./pages/Downloads";
import { SettingsPage } from "./pages/Settings";
import "./App.css";

const queryClient = new QueryClient();

type Page = "dashboard" | "projects" | "ground-motion" | "downloads" | "settings";

const NAV_GROUPS: { label: string | null; items: { id: Page; icon: string; label: string }[] }[] = [
  {
    label: "ASF",
    items: [
      { id: "dashboard", icon: "◈", label: "Dashboard" },
      { id: "projects",  icon: "⊞", label: "Projects" },
    ],
  },
  {
    label: null,
    items: [
      { id: "ground-motion", icon: "⛰", label: "EGMS" },
      { id: "downloads",     icon: "⬇", label: "Downloads" },
      { id: "settings",      icon: "⚙", label: "Settings" },
    ],
  },
];

function App() {
  const [page, setPage] = useState<Page>("dashboard");

  return (
    <QueryClientProvider client={queryClient}>
      <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>

        {/* ── Sidebar ───────────────────────────────────── */}
        <nav style={{
          width: 220,
          background: "var(--sidebar-bg)",
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
            <div style={{
              width: 32, height: 32, borderRadius: 8,
              background: "var(--primary)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 16, flexShrink: 0,
            }}>
              🛰
            </div>
            <div>
              <div style={{ color: "#f1f5f9", fontWeight: 700, fontSize: 13, lineHeight: 1.2 }}>
                InSAR Orchestrator
              </div>
              <div style={{ color: "#475569", fontSize: 11 }}>v0.2.0</div>
            </div>
          </div>

          {/* Nav items */}
          <div style={{ flex: 1, padding: "8px 8px 0" }}>
            {NAV_GROUPS.map((group, gi) => (
              <div key={group.label ?? gi} style={{ marginBottom: gi < NAV_GROUPS.length - 1 ? 14 : 0 }}>
                {group.label && (
                  <div style={{
                    padding: "4px 12px 6px",
                    fontSize: 10,
                    fontWeight: 700,
                    letterSpacing: "0.06em",
                    color: "#475569",
                    textTransform: "uppercase",
                  }}>
                    {group.label}
                  </div>
                )}
                {group.items.map(({ id, icon, label }) => {
                  const active = page === id;
                  return (
                    <button
                      key={id}
                      onClick={() => setPage(id)}
                      style={{
                        width: "100%",
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        padding: "9px 12px",
                        paddingLeft: group.label ? 20 : 12,
                        marginBottom: 2,
                        background: active ? "var(--sidebar-active)" : "transparent",
                        border: "none",
                        borderRadius: 6,
                        color: active ? "#60a5fa" : "var(--sidebar-text)",
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
                      <span style={{ fontSize: 15, opacity: 0.85 }}>{icon}</span>
                      {label}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>

          {/* Footer */}
          <div style={{ padding: "12px 16px", borderTop: "1px solid var(--sidebar-border)" }}>
            <div style={{ fontSize: 11, color: "#334155" }}>
              Sentinel-1 · ASF · HyP3 · EGMS · MintPy
            </div>
          </div>
        </nav>

        {/* ── Main ──────────────────────────────────────── */}
        <main style={{ flex: 1, overflowY: "auto", background: "var(--bg)" }}>
          {page === "dashboard"      && <DashboardPage />}
          {page === "projects"       && <ProjectsPage />}
          {page === "ground-motion"  && <GroundMotionPage onGoToSettings={() => setPage("settings")} />}
          {page === "downloads"      && <DownloadsPage />}
          {page === "settings"       && <SettingsPage />}
        </main>
      </div>
    </QueryClientProvider>
  );
}

export default App;
