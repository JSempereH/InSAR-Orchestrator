import axios from "axios";

export const api = axios.create({
  baseURL: "http://localhost:8000",
  headers: { "Content-Type": "application/json" },
});

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
  created_at: string;
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
}

export interface Batch {
  id: string;
  project_id: string;
  label?: string;
  total_pairs: number;
  created_at: string;
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
}

export interface BatchPlan {
  total_pairs: number;
  scene_count: number;
  pairs_preview: [string, string][];
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

// ── API calls ──────────────────────────────────────────────────────────────

export const projectsApi = {
  list: () => api.get<Project[]>("/api/projects").then((r) => r.data),
  create: (data: ProjectCreate) =>
    api.post<Project>("/api/projects", data).then((r) => r.data),
  get: (id: string) =>
    api.get<Project>(`/api/projects/${id}`).then((r) => r.data),
  delete: (id: string) => api.delete(`/api/projects/${id}`),
  batches: (id: string) =>
    api.get<Batch[]>(`/api/projects/${id}/batches`).then((r) => r.data),
};

export const scenesApi = {
  tracks: (body: {
    geometry: GeoJSON.Geometry;
    date_start: string;
    date_end: string;
  }) => api.post<TrackSummary[]>("/api/scenes/tracks", body).then((r) => r.data),
  search: (body: {
    geometry: GeoJSON.Geometry;
    date_start: string;
    date_end: string;
    track_number?: number;
    flight_direction?: string;
  }) => api.post<Scene[]>("/api/scenes/search", body).then((r) => r.data),
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

export const adminApi = {
  poll: () => api.post<{ active: number; updated: number }>("/api/admin/poll").then((r) => r.data),
};

export const downloadQueueApi = {
  start: (jobs: { job_id: string; hyp3_job_id: string }[]) =>
    api.post<QueueState>("/api/downloads/queue", { jobs }).then((r) => r.data),
  get: () => api.get<QueueState>("/api/downloads/queue").then((r) => r.data),
  cancel: () => api.delete("/api/downloads/queue").then((r) => r.data),
};

export const credentialsApi = {
  upsert: (provider: string, username: string, password: string) =>
    api
      .put("/api/credentials", { provider, username, password })
      .then((r) => r.data),
  get: (provider: string) =>
    api.get(`/api/credentials/${provider}`).then((r) => r.data),
  delete: (provider: string) => api.delete(`/api/credentials/${provider}`),
};
