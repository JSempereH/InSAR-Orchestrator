import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { egmsApi, storageApi, EGMSProduct, EGMSQueueState } from "../api/client";
import { AOIMap } from "../components/Map/AOIMap";

const LEVELS = [
  { value: "L2A", label: "L2a: Ascending/descending displacement" },
  { value: "L2B", label: "L2b: Calibrated displacement" },
  { value: "L3", label: "L3: Ortho (vertical / east-west velocity)" },
];

const DIRECTIONS = [
  { value: "ascending", label: "Ascending" },
  { value: "descending", label: "Descending" },
];

interface GroundMotionPageProps {
  onGoToSettings?: () => void;
}

export function GroundMotionPage({ onGoToSettings }: GroundMotionPageProps) {
  const qc = useQueryClient();

  const [geometry, setGeometry] = useState<GeoJSON.Geometry | null>(null);
  const [level, setLevel] = useState("L3");
  const [release, setRelease] = useState("");
  const [direction, setDirection] = useState("descending");
  const [productType, setProductType] = useState("");
  const [tileId, setTileId] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [destinationName, setDestinationName] = useState("");
  const [storageMountpoint, setStorageMountpoint] = useState("");

  const isL3 = level === "L3";

  const releasesQuery = useQuery({
    queryKey: ["egms-releases"],
    queryFn: () => egmsApi.options("releases"),
    retry: false,
  });
  const productTypesQuery = useQuery({
    queryKey: ["egms-product-types"],
    queryFn: () => egmsApi.options("product_types"),
    enabled: isL3,
    retry: false,
  });
  const { data: storageTargets } = useQuery({ queryKey: ["storage-targets"], queryFn: storageApi.targets });

  const needsCredentials = releasesQuery.isError;

  // Default to the most recently published release once options load.
  useEffect(() => {
    if (releasesQuery.data?.length && !release) {
      setRelease(releasesQuery.data[releasesQuery.data.length - 1]);
    }
  }, [releasesQuery.data]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (productTypesQuery.data?.length && !productType) {
      setProductType(productTypesQuery.data[0]);
    }
  }, [productTypesQuery.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const searchMut = useMutation({
    mutationFn: () =>
      egmsApi.search({
        geometry: geometry!,
        level,
        release,
        direction: isL3 ? undefined : direction,
        product_type: isL3 ? productType : undefined,
        tile_id: isL3 && tileId ? tileId : undefined,
      }),
    onSuccess: (products) => setSelected(new Set(products.map((p) => p.filename))),
  });

  const products = searchMut.data ?? [];
  const selectedProducts = useMemo(
    () => products.filter((p) => selected.has(p.filename)),
    [products, selected]
  );
  const totalSizeMb = selectedProducts.reduce((sum, p) => sum + (p.size_mb ?? 0), 0);

  const { data: queueState } = useQuery({
    queryKey: ["egms-download-queue"],
    queryFn: egmsApi.getQueue,
    refetchInterval: (q) => (q.state.data?.active ? 800 : 5000),
  });

  const downloadMut = useMutation({
    mutationFn: () =>
      egmsApi.startDownload({
        products: selectedProducts,
        destination_name: destinationName,
        storage_mountpoint: storageMountpoint || undefined,
        geometry: geometry!,
        level,
        release,
        direction: isL3 ? undefined : direction,
        product_type: isL3 ? productType : undefined,
        tile_id: isL3 && tileId ? tileId : undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["egms-download-queue"] });
      qc.invalidateQueries({ queryKey: ["egms-downloads"] });
    },
  });

  const cancelMut = useMutation({
    mutationFn: egmsApi.cancelQueue,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["egms-download-queue"] }),
  });

  function toggle(filename: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename); else next.add(filename);
      return next;
    });
  }

  return (
    <div className="page" style={{ maxWidth: 900 }}>
      <h2 style={{ marginBottom: 4 }}>Ground Motion (EGMS)</h2>
      <p style={{ margin: "0 0 20px", color: "var(--text-muted)", fontSize: 13 }}>
        Search and download finished ground-motion products from the Copernicus European Ground
        Motion Service for an area of interest. No HyP3 processing involved.
      </p>

      {needsCredentials && (
        <div style={{
          padding: "12px 16px", marginBottom: 20,
          background: "var(--warning-light)", border: "1px solid #fed7aa",
          borderRadius: "var(--radius)", fontSize: 13, color: "#92400e",
          display: "flex", alignItems: "center", gap: 12,
        }}>
          <span style={{ flex: 1 }}>
            No EGMS credentials found. Add your CLMS API service-account key in Settings before searching.
          </span>
          {onGoToSettings && (
            <button className="btn btn-secondary btn-sm" onClick={onGoToSettings}>Go to Settings →</button>
          )}
        </div>
      )}

      {queueState && queueState.active && (
        <EGMSDownloadBanner
          state={queueState}
          onCancel={() => cancelMut.mutate()}
          cancelling={cancelMut.isPending}
        />
      )}

      <div className="card" style={{ padding: 20, marginBottom: 20 }}>
        <h3 style={{ marginBottom: 16 }}>Area of interest</h3>
        <p style={{ margin: "0 0 10px", fontSize: 13, color: "var(--text-muted)" }}>
          Draw a polygon. EGMS accepts areas up to 5° x 5°.
        </p>
        <AOIMap onGeometryChange={setGeometry} height={420} />
      </div>

      <div className="card" style={{ padding: 20, marginBottom: 20 }}>
        <h3 style={{ marginBottom: 16 }}>Product selection</h3>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 14 }}>
          <div className="field">
            <label>Level</label>
            <select className="input" value={level} onChange={(e) => setLevel(e.target.value)}>
              {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
            </select>
          </div>

          <div className="field">
            <label>Release</label>
            <select className="input" value={release} onChange={(e) => setRelease(e.target.value)} disabled={!releasesQuery.data?.length}>
              {!releasesQuery.data?.length && <option value="">-</option>}
              {releasesQuery.data?.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>

          {!isL3 && (
            <div className="field">
              <label>Orbit direction</label>
              <select className="input" value={direction} onChange={(e) => setDirection(e.target.value)}>
                {DIRECTIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
              </select>
            </div>
          )}

          {isL3 && (
            <>
              <div className="field">
                <label>Component</label>
                <select className="input" value={productType} onChange={(e) => setProductType(e.target.value)} disabled={!productTypesQuery.data?.length}>
                  {!productTypesQuery.data?.length && <option value="">-</option>}
                  {productTypesQuery.data?.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <div className="field">
                <label>Tile ID (optional)</label>
                <input className="input" value={tileId} onChange={(e) => setTileId(e.target.value)} placeholder="e.g. E40N30" />
              </div>
            </>
          )}
        </div>

        <button
          className="btn btn-primary"
          disabled={!geometry || !release || searchMut.isPending || needsCredentials}
          onClick={() => searchMut.mutate()}
        >
          {searchMut.isPending ? "Searching…" : "Search products"}
        </button>

        {searchMut.isError && (
          <div style={{ marginTop: 12, color: "var(--danger)", fontSize: 13 }}>
            Search failed. {String(searchMut.error)}
          </div>
        )}
      </div>

      {searchMut.isSuccess && (
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginBottom: 4 }}>Results</h3>
          <p style={{ margin: "0 0 16px", fontSize: 13, color: "var(--text-muted)" }}>
            {products.length} product{products.length === 1 ? "" : "s"} found.
          </p>

          {products.length === 0 && (
            <div style={{ padding: 14, background: "var(--warning-light)", border: "1px solid #fed7aa", borderRadius: "var(--radius)", fontSize: 13, color: "#92400e" }}>
              No products cover this AOI for the selected level/release.
            </div>
          )}

          {products.length > 0 && (
            <>
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 16, maxHeight: 260, overflowY: "auto" }}>
                {products.map((p) => (
                  <ProductRow key={p.filename} product={p} checked={selected.has(p.filename)} onToggle={() => toggle(p.filename)} />
                ))}
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 14 }}>
                <div className="field">
                  <label>Destination name</label>
                  <input className="input" value={destinationName} onChange={(e) => setDestinationName(e.target.value)} placeholder="e.g. Murcia ground motion" />
                </div>
                <div className="field">
                  <label>Storage destination</label>
                  <select className="input" value={storageMountpoint} onChange={(e) => setStorageMountpoint(e.target.value)}>
                    {storageTargets?.map((t) => (
                      <option key={t.mountpoint ?? "default"} value={t.mountpoint ?? ""} disabled={!t.writable}>
                        {t.mountpoint ? `${t.mountpoint} (${t.device})` : "App default"}
                        {" · "}{t.free_gb} GB free{!t.writable ? " · not writable" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <button
                  className="btn btn-primary"
                  disabled={selectedProducts.length === 0 || !destinationName || downloadMut.isPending}
                  onClick={() => downloadMut.mutate()}
                >
                  {downloadMut.isPending ? "Queuing…" : `Download ${selectedProducts.length} selected`}
                </button>
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  {selectedProducts.length} selected · ~{totalSizeMb.toFixed(1)} MB
                </span>
              </div>

              {downloadMut.isError && (
                <div style={{ marginTop: 12, color: "var(--danger)", fontSize: 13 }}>
                  Failed to queue download. {String(downloadMut.error)}
                </div>
              )}

              {downloadMut.data?.destination && (
                <div style={{ marginTop: 12, fontSize: 12, color: "var(--text-muted)" }}>
                  💾 Saving to <code style={{ background: "#f1f5f9", padding: "1px 5px", borderRadius: 4 }}>{downloadMut.data.destination}</code>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ProductRow({ product, checked, onToggle }: { product: EGMSProduct; checked: boolean; onToggle: () => void }) {
  return (
    <label
      style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "8px 12px", borderRadius: "var(--radius)",
        border: "1px solid var(--border)", cursor: "pointer",
        background: checked ? "var(--primary-light)" : "white",
      }}
    >
      <input type="checkbox" checked={checked} onChange={onToggle} />
      <span style={{ flex: 1, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={product.filename}>
        {product.filename}
      </span>
      <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
        {product.size_mb ? `${product.size_mb.toFixed(1)} MB` : "-"}
      </span>
    </label>
  );
}

function EGMSDownloadBanner({ state, onCancel, cancelling }: { state: EGMSQueueState; onCancel: () => void; cancelling: boolean }) {
  const p = state.current_progress;
  const pct = p?.pct ?? 0;

  return (
    <div style={{
      marginBottom: 18, padding: "14px 18px",
      background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
      border: "1px solid rgba(34,197,94,0.3)", borderRadius: "var(--radius-lg)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#22c55e", boxShadow: "0 0 6px #22c55e" }} />
        <span style={{ fontWeight: 700, fontSize: 13, color: "#f1f5f9" }}>Downloading EGMS products</span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "#94a3b8", background: "rgba(255,255,255,0.06)", padding: "2px 10px", borderRadius: 20 }}>
          {state.done} / {state.total} complete
        </span>
        <button className="btn btn-ghost btn-sm" disabled={cancelling} onClick={onCancel} style={{ fontSize: 11, color: "#f87171", borderColor: "rgba(248,113,113,0.3)" }}>
          {cancelling ? "Cancelling…" : "× Cancel remaining"}
        </button>
      </div>

      {state.current_filename && (
        <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 4 }} title={state.current_filename}>
          {state.current_filename}
        </div>
      )}

      {state.destination && (
        <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6 }}>
          💾 {state.destination}
        </div>
      )}

      <div style={{ height: 14, background: "rgba(255,255,255,0.05)", borderRadius: 8, overflow: "hidden", border: "1px solid rgba(34,197,94,0.2)" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 8, transition: "width 0.3s ease",
          background: "linear-gradient(90deg, #15803d, #22c55e)",
        }} />
      </div>
    </div>
  );
}
