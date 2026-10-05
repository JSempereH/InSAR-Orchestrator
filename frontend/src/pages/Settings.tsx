import { useState } from "react";
import { credentialsApi } from "../api/client";
import { CredentialCard, useCredentialSave } from "../components/CredentialCard";

export function SettingsPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const earthdata = useCredentialSave(async () => {
    await credentialsApi.upsert("earthdata", username, password);
    setPassword("");
  });

  const [egmsKeyJson, setEgmsKeyJson] = useState("");
  const egms = useCredentialSave(async () => {
    const parsed = JSON.parse(egmsKeyJson);
    if (!parsed.client_id || !parsed.user_id || !parsed.token_uri || !parsed.private_key) {
      throw new Error("Key is missing client_id, user_id, token_uri, or private_key.");
    }
    await credentialsApi.upsert("egms", parsed.client_id, egmsKeyJson);
  });

  return (
    <div className="page" style={{ maxWidth: 620 }}>
      <h2 style={{ marginBottom: 4 }}>Settings</h2>
      <p style={{ margin: "0 0 28px", color: "var(--text-muted)", fontSize: 13 }}>
        Manage platform credentials and application preferences.
      </p>

      <CredentialCard
        icon="🔑"
        iconBg="#e0f2fe"
        title="NASA Earthdata"
        description={
          <>
            Required for HyP3 job submission and Sentinel-1 data access.
            Credentials are encrypted before storage.{" "}
            <a href="https://urs.earthdata.nasa.gov/users/new" target="_blank" rel="noreferrer" style={{ color: "var(--primary)" }}>
              Register here
            </a>{" "}
            if you don't have an account.
          </>
        }
        onSave={earthdata.handleSave}
        saveLabel="Save credentials"
        savingLabel="Saving…"
        saveDisabled={!username || !password}
        state={earthdata.state}
        error={earthdata.error}
      >
        <div className="field">
          <label>Username</label>
          <input
            className="input"
            type="text"
            value={username}
            onChange={(e) => { setUsername(e.target.value); earthdata.resetToIdle(); }}
            autoComplete="username"
            placeholder="Your Earthdata username"
          />
        </div>
        <div className="field">
          <label>Password</label>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => { setPassword(e.target.value); earthdata.resetToIdle(); }}
            autoComplete="current-password"
            placeholder="••••••••"
          />
        </div>
      </CredentialCard>

      <CredentialCard
        icon="⛰"
        iconBg="#dcfce7"
        title="Copernicus EGMS"
        description={
          <>
            Required to search and download European Ground Motion Service products. Paste the
            full JSON service-account key generated from your{" "}
            <a href="https://land.copernicus.eu" target="_blank" rel="noreferrer" style={{ color: "var(--primary)" }}>
              CLMS account
            </a>{" "}
            page. Stored encrypted, same as your Earthdata credentials.
          </>
        }
        onSave={egms.handleSave}
        saveLabel="Save EGMS key"
        savingLabel="Saving…"
        saveDisabled={!egmsKeyJson}
        state={egms.state}
        error={egms.error}
      >
        <div className="field">
          <label>Service-account key (JSON)</label>
          <textarea
            className="input"
            rows={5}
            style={{ fontFamily: "monospace", fontSize: 12 }}
            value={egmsKeyJson}
            onChange={(e) => { setEgmsKeyJson(e.target.value); egms.resetToIdle(); }}
            placeholder='{"client_id": "...", "user_id": "...", "token_uri": "...", "private_key": "..."}'
          />
        </div>
      </CredentialCard>

      {/* Secret key hint */}
      <div className="card" style={{ padding: 20 }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
          <div style={{
            width: 40, height: 40, borderRadius: 10, flexShrink: 0,
            background: "#fef3c7",
            display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20,
          }}>
            🔐
          </div>
          <div style={{ flex: 1 }}>
            <h3 style={{ marginBottom: 4 }}>Encryption key</h3>
            <p style={{ margin: "0 0 12px", fontSize: 13, color: "var(--text-muted)" }}>
              Add <code style={{ background: "#f1f5f9", padding: "1px 5px", borderRadius: 4, fontSize: 12 }}>SECRET_KEY</code> to{" "}
              <code style={{ background: "#f1f5f9", padding: "1px 5px", borderRadius: 4, fontSize: 12 }}>backend/.env</code>{" "}
              so encrypted credentials persist across backend restarts.
            </p>
            <div style={{
              background: "#0f172a",
              color: "#94a3b8",
              borderRadius: "var(--radius)",
              padding: "12px 16px",
              fontFamily: "monospace",
              fontSize: 12,
              lineHeight: 1.6,
            }}>
              <span style={{ color: "#64748b" }}># Run once and copy the output into backend/.env</span>
              {"\n"}
              <span style={{ color: "#93c5fd" }}>python</span>
              {' -c "from cryptography.fernet import Fernet; print(\'SECRET_KEY=\' + Fernet.generate_key().decode())"'}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
