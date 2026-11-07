import axios from "axios";

// In a built app the backend serves the UI, so requests are relative to the same
// origin and work behind any host name or reverse proxy. `npm run dev` talks to
// the backend on localhost:8000 instead. VITE_API_URL overrides both.
const API_BASE_URL = import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? "http://localhost:8000" : "");

export const api = axios.create({
  baseURL: API_BASE_URL,
  headers: { "Content-Type": "application/json" },
  withCredentials: true, // the session cookie, when APP_PASSWORD is set
});

export const AUTH_REQUIRED_EVENT = "insar-auth-required";

// Any 401 means the session ended (or never started): tell the app to show the sign-in page.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && !String(error.config?.url ?? "").startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
    }
    return Promise.reject(error);
  },
);

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
}

export const authApi = {
  status: () => api.get<AuthStatus>("/api/auth/status").then((r) => r.data),
  login: (password: string) => api.post<AuthStatus>("/api/auth/login", { password }).then((r) => r.data),
  logout: () => api.post<AuthStatus>("/api/auth/logout").then((r) => r.data),
};

// ── Types ──────────────────────────────────────────────────────────────────

export interface Project {
  id: string;
  name: string;
  description?: string;
  geometry: GeoJSON.Geometry;
  track_number?: number;
  flight_direction?: string;
  date_start?: string;
  date_end?: string;
  max_temporal_neighbors: number;
  storage_path?: string;
  backup_enabled: boolean;
  archived_remote?: string | null;
  archived_at?: string | null;
  created_at: string;
}

export interface ArchiveState {
  project_id: string | null;
  kind: "archive" | "restore" | null;
  active: boolean;
  status: "idle" | "running" | "done" | "error";
  step?: string | null;
  pct: number;
  error?: string | null;
}

export interface ProjectCreate {
  name: string;
  description?: string;
  geometry: GeoJSON.Geometry;
  track_number?: number;
  flight_direction?: string;
  date_start?: string;
  date_end?: string;
  max_temporal_neighbors?: number;
  storage_mountpoint?: string;
}

export interface StorageTarget {
  mountpoint: string | null; // null = app default
  device: string;
  fstype: string;
  total_gb: number;
  free_gb: number;
  writable: boolean;
}

export interface PathUsage {
  path: string;
  used_gb: number;
  free_gb: number;
  total_gb: number;
}

export interface MoveState {
  active: boolean;
  status: "idle" | "running" | "done" | "error";
  pct: number;
  src: string | null;
  dst: string | null;
  error: string | null;
}

export interface Batch {
  id: string;
  project_id: string;
  label?: string;
  total_pairs: number;
  status: "submitting" | "done";
  auto_download: boolean;
  run_mintpy_when_done: boolean;
  parameters?: BatchParameters | null;
  created_at: string;
}

export interface BatchParameters {
  processor: string;
  hyp3: Record<string, string | boolean>;
  pairing: { method: string; max_temporal_neighbors: number };
  search: { track_number?: number | null; flight_direction?: string | null; date_start?: string | null; date_end?: string | null };
  software: Record<string, string | null>;
  app_version: string;
}

export interface MintPyRunRecord {
  mintpy_run: {
    status: string | null; started_at: string | null; finished_at: string | null;
    return_code: number | null; attempt: number | null; error: string | null;
    warnings?: string[];
  };
  results?: {
    timeseries: string | null;
    velocity: string | null;
    reference?: { row: number; column: number; date: string | null; lat: number | null; lon: number | null } | null;
  };
  software: Record<string, string | null>;
  mintpy: { custom_config: string; options: Record<string, string> | null };
  crop: { applied: boolean; subset_lalo: string | null; margin_fraction: number; min_margin_deg: number };
  interferograms: { count: number; names: string[] };
  written_at: string;
}

export interface Provenance {
  schema: number;
  generated_at: string;
  app_version: string;
  archived: boolean;
  project: Record<string, unknown>;
  batches: {
    id: string; label: string | null; created_at: string | null; total_pairs: number;
    status: string; auto_download: boolean; parameters: BatchParameters | null;
  }[];
  pairs: Record<string, unknown>[];
  last_mintpy_run: MintPyRunRecord | null;
}

export interface ProjectRefresh {
  date_end: string;
  total_pairs: number;
  scene_count?: number;
  estimated_size_gb?: number;
  batch?: Batch;
}

export interface Job {
  id: string;
  batch_id: string;
  hyp3_job_id?: string;
  reference_granule: string;
  secondary_granule: string;
  reference_date?: string;
  secondary_date?: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  credit_cost?: number;
  downloaded: number;
  download_path?: string;
  error_message?: string;
  submitted_at?: string;
  completed_at?: string;
  is_downloading: boolean;
}

export interface TrackSummary {
  track_number: number;
  flight_direction: string;
  scene_count: number;
  first_date: string;
  last_date: string;
}

export interface Scene {
  file_id: string;
  granule_name: string;
  acquisition_date: string;
  orbit: number;
  track_number: number;
  flight_direction: string;
  polarization: string;
  size_mb?: number;
  download_url?: string;
  file_name?: string;
  already_downloaded: boolean;
}

export interface SLCDownload {
  id: string;
  project_id: string;
  destination_path: string;
  filenames: string[];
  created_at: string;
}

export interface BatchPlan {
  total_pairs: number;
  scene_count: number;
  pairs_preview: [string, string][];
  estimated_size_gb?: number;
}

export interface DownloadProgress {
  status: "idle" | "running" | "done" | "error";
  pct?: number;
  filename?: string;
  file_index?: number;
  file_count?: number;
  total_bytes?: number;
  downloaded_bytes?: number;
  speed_bps?: number;
  eta_s?: number;
  error?: string;
}

export interface QueueState {
  active: boolean;
  current_job_id: string | null;
  current_progress: DownloadProgress | null;
  pending_count: number;
  pending_job_ids: { job_id: string; hyp3_job_id: string }[];
  total: number;
  done: number;
  cancelled: boolean;
}

// ── EGMS (Copernicus ground motion) ───────────────────────────────────────

export interface EGMSProduct {
  query_id: string;
  filename: string;
  level: string;
  size_mb?: number;
}

export interface EGMSSearchRequest {
  geometry: GeoJSON.Geometry;
  level: string;              // "L2A" | "L2B" | "L3"
  release: string;            // e.g. "2019-2023"
  direction?: string;         // required for L2A/L2B
  product_type?: string;      // required for L3
  tile_id?: string;
}

export interface SLCQueueState {
  active: boolean;
  current_filename: string | null;
  current_progress: DownloadProgress | null;
  pending_count: number;
  destination: string | null;
  total: number;
  done: number;
  cancelled: boolean;
}

export interface EGMSQueueState {
  active: boolean;
  current_filename: string | null;
  current_progress: DownloadProgress | null;
  pending_count: number;
  destination: string | null;
  total: number;
  done: number;
  cancelled: boolean;
}

export interface EGMSDownloadRecord {
  id: string;
  name: string;
  geometry: GeoJSON.Geometry;
  level: string;
  release: string;
  direction?: string;
  product_type?: string;
  tile_id?: string;
  destination_path: string;
  filenames: string[];
  created_at: string;
}

export interface ProjectDownloadSummary {
  storage_path: string | null;
  total_jobs: number;
  downloaded_jobs: number;
}

// Shared shape returned by both the EGMS and MintPy time series endpoints.
export interface TimeSeries {
  dates: string[];
  values_mm: number[];
  /** Statistical summary computed by the backend; null when it could not be computed. */
  analysis?: TimeSeriesAnalysis | null;
}

export type TimeSeriesModelKind = "stable" | "linear" | "quadratic" | "bilinear" | "step";

// Computed server-side by insar_core.analysis.timeseries.analyze_time_series.
export interface TimeSeriesAnalysis {
  n: number;
  start_date: string;
  end_date: string;
  span_years: number;
  median_step_days: number;
  gaps: number;
  max: { value: number; date: string };
  min: { value: number; date: string };
  mean: number;
  std: number;
  velocity: {
    ols_mm_yr: number;
    se_mm_yr: number;
    ci95: [number, number];
    theil_sen_mm_yr: number;
    mann_kendall_z: number;
    mann_kendall_p: number;
    lag1_autocorr: number;
  };
  model: {
    kind: TimeSeriesModelKind;
    label: string;
    bic_gain: number;
    break_date?: string;
    slope_before?: number;
    slope_after?: number;
    step_mm?: number;
    acceleration_mm_yr2?: number;
  };
  seasonal: { present: boolean; amplitude_mm: number; peak_month: number };
  noise: { rms_mm: number; outliers: { date: string; value: number; residual: number }[] };
  net_change_mm: number;
  /** Cleaned, date-sorted series with the fitted overlays, all the same length. */
  series: { dates: string[]; values: number[]; fit: number[]; trend: number[] };
  break_index: number | null;
  narrative: string[];
}

// ── API calls ──────────────────────────────────────────────────────────────

export const projectsApi = {
  list: () => api.get<Project[]>("/api/projects").then((r) => r.data),
  create: (data: ProjectCreate) =>
    api.post<Project>("/api/projects", data).then((r) => r.data),
  get: (id: string) =>
    api.get<Project>(`/api/projects/${id}`).then((r) => r.data),
  update: (id: string, data: { description?: string; date_end?: string; max_temporal_neighbors?: number; backup_enabled?: boolean }) =>
    api.patch<Project>(`/api/projects/${id}`, data).then((r) => r.data),
  refresh: (id: string, body: { dry_run: boolean; date_end?: string; run_mintpy?: boolean }) =>
    api.post<ProjectRefresh>(`/api/projects/${id}/refresh`, body).then((r) => r.data),
  provenance: (id: string) => api.get<Provenance>(`/api/projects/${id}/provenance`).then((r) => r.data),
  archive: (id: string, destination?: string) =>
    api.post<ArchiveState>(`/api/projects/${id}/archive`, { destination: destination || null }).then((r) => r.data),
  restore: (id: string) => api.post<ArchiveState>(`/api/projects/${id}/restore`).then((r) => r.data),
  archiveState: () => api.get<ArchiveState>("/api/archive/state").then((r) => r.data),
  delete: (id: string) => api.delete(`/api/projects/${id}`),
  batches: (id: string) =>
    api.get<Batch[]>(`/api/projects/${id}/batches`).then((r) => r.data),
  downloadSummary: (id: string) =>
    api.get<ProjectDownloadSummary>(`/api/projects/${id}/download-summary`).then((r) => r.data),
  moveStorage: (id: string, mountpoint: string | null) =>
    api.post<MoveState>(`/api/projects/${id}/storage/move`, { mountpoint }).then((r) => r.data),
  deleteDownloadData: (id: string) =>
    api.delete<{ deleted_jobs: number; freed_gb: number }>(`/api/projects/${id}/download-data`).then((r) => r.data),
};

export const scenesApi = {
  tracks: (body: {
    geometry: GeoJSON.Geometry;
    date_start: string;
    date_end: string;
  }) => api.post<TrackSummary[]>("/api/scenes/tracks", body).then((r) => r.data),
};

export const slcApi = {
  scenes: (projectId: string) =>
    api.get<Scene[]>(`/api/projects/${projectId}/slc/scenes`).then((r) => r.data),
  queueDownload: (projectId: string, scenes: Scene[]) =>
    api
      .post<SLCDownload>(`/api/projects/${projectId}/slc/downloads/queue`, { scenes })
      .then((r) => r.data),
  getQueue: () => api.get<SLCQueueState>("/api/slc/downloads/queue").then((r) => r.data),
  cancelQueue: () => api.delete("/api/slc/downloads/queue").then((r) => r.data),
  listDownloads: (projectId: string) =>
    api.get<SLCDownload[]>(`/api/projects/${projectId}/slc/downloads`).then((r) => r.data),
  deleteDownload: (id: string, deleteFiles = false) =>
    api.delete<{ deleted: boolean; freed_gb: number }>(`/api/slc/downloads/${id}`, { params: { delete_files: deleteFiles } }).then((r) => r.data),
};

export const jobsApi = {
  plan: (
    projectId: string,
    body: { max_temporal_neighbors?: number; label?: string; dry_run: true }
  ) =>
    api
      .post<BatchPlan>(`/api/projects/${projectId}/batches/plan`, body)
      .then((r) => r.data),
  submit: (
    projectId: string,
    body: {
      max_temporal_neighbors?: number;
      label?: string;
      dry_run: false;
      auto_download?: boolean;
    }
  ) =>
    api
      .post<Batch>(`/api/projects/${projectId}/batches`, body)
      .then((r) => r.data),
  listJobs: (projectId: string, batchId: string) =>
    api
      .get<Job[]>(`/api/projects/${projectId}/batches/${batchId}/jobs`)
      .then((r) => r.data),
};

export const batchesApi = {
  setAutoDownload: (batchId: string, auto_download: boolean) =>
    api.patch<Batch>(`/api/batches/${batchId}`, { auto_download }).then((r) => r.data),
  rename: (batchId: string, label: string) =>
    api.patch<Batch>(`/api/batches/${batchId}`, { label }).then((r) => r.data),
};

export const adminApi = {
  poll: () => api.post<{ active: number; updated: number }>("/api/admin/poll").then((r) => r.data),
};

export interface CreditsSnapshot {
  credits: number | null;
  updated_at: string | null;
  error: string | null;
  refresh_interval_seconds: number;
}

export const creditsApi = {
  get: () => api.get<CreditsSnapshot>("/api/credits").then((r) => r.data),
  refresh: () => api.post<CreditsSnapshot>("/api/admin/poll-credits").then((r) => r.data),
};

export interface DiskUsage {
  zips_bytes: number;
  extracted_bytes: number;
  mintpy_output_bytes: number;
  total_bytes: number;
}

export interface MintPyStatus {
  status: "NOT_STARTED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "INTERRUPTED";
  started_at?: string | null;
  finished_at?: string | null;
  return_code?: number | null;
  error?: string | null;
  work_dir?: string;
  log_tail?: string;
  disk_usage?: DiskUsage | null;
  warnings?: string[];
}

export const mintpyApi = {
  run: (projectId: string) => api.post<MintPyStatus>(`/api/projects/${projectId}/mintpy/run`).then((r) => r.data),
  status: (projectId: string) => api.get<MintPyStatus>(`/api/projects/${projectId}/mintpy/status`).then((r) => r.data),
  cancel: (projectId: string) => api.delete<MintPyStatus>(`/api/projects/${projectId}/mintpy`).then((r) => r.data),
  getTimeSeries: (projectId: string, lat: number, lon: number) =>
    api.get<TimeSeries>(`/api/projects/${projectId}/mintpy/timeseries`, { params: { lat, lon } }).then((r) => r.data),
  getPoints: (projectId: string) =>
    api.get<GeoJSON.FeatureCollection>(`/api/projects/${projectId}/mintpy/points`).then((r) => r.data),
};

export const downloadQueueApi = {
  start: (jobs: { job_id: string; hyp3_job_id: string }[]) =>
    api.post<QueueState>("/api/downloads/queue", { jobs }).then((r) => r.data),
  get: () => api.get<QueueState>("/api/downloads/queue").then((r) => r.data),
  cancel: () => api.delete("/api/downloads/queue").then((r) => r.data),
};

export const storageApi = {
  targets: () => api.get<StorageTarget[]>("/api/storage/targets").then((r) => r.data),
  usage: (path: string) =>
    api.get<PathUsage>("/api/storage/usage", { params: { path } }).then((r) => r.data),
  moveState: () => api.get<MoveState>("/api/storage/move").then((r) => r.data),
};

export const egmsApi = {
  options: (kind: string) => api.get<string[]>(`/api/egms/options/${kind}`).then((r) => r.data),
  search: (body: EGMSSearchRequest) =>
    api.post<EGMSProduct[]>("/api/egms/search", body).then((r) => r.data),
  startDownload: (
    body: EGMSSearchRequest & { products: EGMSProduct[]; destination_name: string; storage_mountpoint?: string }
  ) => api.post<EGMSQueueState>("/api/egms/downloads/queue", body).then((r) => r.data),
  getQueue: () => api.get<EGMSQueueState>("/api/egms/downloads/queue").then((r) => r.data),
  cancelQueue: () => api.delete("/api/egms/downloads/queue").then((r) => r.data),
  listDownloads: () => api.get<EGMSDownloadRecord[]>("/api/egms/downloads").then((r) => r.data),
  deleteDownload: (id: string, deleteFiles = false) =>
    api.delete<{ deleted: boolean; freed_gb: number }>(`/api/egms/downloads/${id}`, { params: { delete_files: deleteFiles } }).then((r) => r.data),
  moveDownload: (id: string, mountpoint: string | null) =>
    api.post<MoveState>(`/api/egms/downloads/${id}/move`, { mountpoint }).then((r) => r.data),
  getPoints: (id: string) => api.get<GeoJSON.FeatureCollection>(`/api/egms/downloads/${id}/points`).then((r) => r.data),
  getTimeSeries: (id: string, pointId: string) =>
    api.get<TimeSeries>(`/api/egms/downloads/${id}/points/${encodeURIComponent(pointId)}/timeseries`).then((r) => r.data),
};

export const credentialsApi = {
  upsert: (provider: string, username: string, password: string) =>
    api
      .put("/api/credentials", { provider, username, password })
      .then((r) => r.data),
};

// ── Backup ─────────────────────────────────────────────────────────────────

export interface BackupConfig {
  destination: string | null;
  enabled: boolean;
  interval_hours: number;
  keep_daily: number;
  keep_weekly: number;
  include_slc: boolean;
  last_run_at?: string | null;
  last_status?: "ok" | "error" | null;
  last_message?: string | null;
}

export interface BackupOverview {
  rclone: { available: boolean; version?: string; remotes: string[] };
  config: BackupConfig;
  state: { running: boolean; status: string; step?: string | null; pct: number; error?: string | null };
}

export const backupApi = {
  overview: () => api.get<BackupOverview>("/api/backup").then((r) => r.data),
  saveConfig: (config: Omit<BackupConfig, "last_run_at" | "last_status" | "last_message">) =>
    api.put<BackupConfig>("/api/backup/config", config).then((r) => r.data),
  test: (destination: string) =>
    api.post<{ ok: boolean; destination: string }>("/api/backup/test", { destination }).then((r) => r.data),
  run: () => api.post("/api/backup/run").then((r) => r.data),
};
