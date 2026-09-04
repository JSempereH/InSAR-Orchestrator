# Backend

FastAPI REST API with SQLite persistence.

> Run `../setup.sh` from the repo root to install this backend's venv + dependencies automatically, then `../dev.sh` to run it alongside the frontend.

## Running

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive API docs at `http://localhost:8000/docs`.

## Key endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| POST/GET | `/api/projects` | Create / list projects |
| GET/DELETE | `/api/projects/{id}` | Get / delete a project |
| GET | `/api/projects/{id}/batches` | List batches for a project |
| POST | `/api/projects/{id}/batches/plan` | Dry-run: count new pairs without submitting |
| POST | `/api/projects/{id}/batches` | Submit new SBAS pairs to HyP3 |
| GET | `/api/projects/{id}/batches/{bid}/jobs` | List jobs in a batch |
| POST | `/api/scenes/tracks` | Available orbital tracks for an AOI |
| POST | `/api/scenes/search` | Search ASF for Sentinel-1 scenes |
| PUT | `/api/credentials` | Store encrypted Earthdata credentials |
| POST/GET/DELETE | `/api/downloads/queue` | Manage the download queue |
| POST | `/api/admin/poll` | Force immediate HyP3 status sync |
| WS | `/ws/batches/{id}` | Real-time job status stream |
| GET | `/api/credits` | Last known HyP3 credit balance (background-refreshed every 5 min) |
| POST | `/api/admin/poll-credits` | Force an immediate HyP3 credit-balance refresh |
| POST | `/api/projects/{id}/mintpy/run` | Start MintPy SBAS over the project's downloaded interferograms (requires MintPy on this host) |
| GET | `/api/projects/{id}/mintpy/status` | Poll status + tailing log of the project's MintPy run |
| DELETE | `/api/projects/{id}/mintpy` | Cancel a running MintPy job |
