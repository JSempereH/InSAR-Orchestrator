# InSAR Orchestrator

A self-hosted, end-to-end InSAR processing platform for Sentinel-1 data. Instead of navigating ASF, HyP3, or ESGM dashboards separately, this tool centralises everything: draw an area on a map, discover available satellite tracks, submit interferometric pairs to HyP3 for cloud processing, monitor jobs in real time, and prepare results for MintPy SBAS analysis, all from a single web UI.

This tool is designed with research in mind. This is **not** a production tool, no warranty guaranteed.

---

## Architecture

```
insar-orchestrator/
├── packages/insar_core/   # Core Python library (pip-installable)
│   ├── adapters/          # ASF scene search, HyP3 SDK wrappers
│   ├── models/            # Pure dataclasses: SARScene, AOI, Job, …
│   └── pipeline/          # SBAS pair builder, orchestrator, MintPy adapter
├── backend/               # FastAPI REST API + WebSocket
│   └── app/
│       ├── routers/       # projects, batches, jobs, scenes, credentials
│       └── services/      # HyP3 integration, polling loop, encryption
└── frontend/              # React 18 + Vite SPA
    └── src/
        ├── components/    # AOI map (MapLibre), job monitor, track picker
        └── pages/         # Dashboard, Projects wizard, Settings
```


---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | ≥ 3.11 | For backend + core library |
| Node.js | ≥ 18 | For frontend |
| MintPy | ≥ 1.6.3 | Only needed for local SBAS analysis (`smallbaselineApp.py` must be in `PATH`) |
| NASA Earthdata account | — | Free at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov/users/new) |

---

## Setup

### 1. Clone and install the core library

```bash
git clone <repo-url> insar-orchestrator
cd insar-orchestrator
```

The `insar_core` library must be installed in the same Python environment as the backend:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # installs insar-core from ../packages/insar_core
```

### 2. Configure backend environment

Create `backend/.env` (never commit this file):

```bash
# backend/.env

# Optional: persistent encryption key for stored credentials.
# If omitted, a new key is generated each restart (credentials reset).
# Generate once and paste here:
#   python -c "from cryptography.fernet import Fernet; print('SECRET_KEY=' + Fernet.generate_key().decode())"
SECRET_KEY=

# Optional overrides (defaults shown):
# DATABASE_URL=sqlite:///./insar_app.db
# DOWNLOADS_DIR=./downloads
```

### 3. Configure Earthdata credentials

**Option A: environment variables (recommended for development):**

```bash
export EARTHDATA_USER=your_username
export EARTHDATA_PASS=your_password
```

**Option B: `~/.netrc`** (standard NASA tool format):

```
machine urs.earthdata.nasa.gov
    login your_username
    password your_password
```

```bash
chmod 600 ~/.netrc
```

**Option C: web UI** (Settings page): credentials are encrypted with Fernet before being stored in the database.


---

## Running the backend

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

Key endpoints:

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| POST | `/api/projects` | Create a project (AOI + parameters) |
| GET | `/api/projects` | List all projects |
| GET | `/api/scenes/search` | Search ASF for Sentinel-1 scenes |
| GET | `/api/scenes/tracks` | Available tracks for an AOI + date range |
| POST | `/api/projects/{id}/batches` | Plan or submit a batch of pairs to HyP3 |
| GET | `/api/batches/{id}/jobs` | List jobs in a batch |
| PUT | `/api/jobs/{id}/download` | Download a completed job |
| POST | `/api/credentials` | Store encrypted Earthdata credentials |
| WS | `/ws/batches/{id}` | Real-time job status stream |

---

## Running the frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The app expects the backend at `http://localhost:8000`.

To build for production:

```bash
npm run build   # outputs to frontend/dist/
```


---

## Using `insar_core` as a standalone library

The core library can be used independently from the web app, useful for scripting or research notebooks.

```python
from datetime import date
from insar_core import AOI, SearchParams, ASFAdapter, HyP3Adapter, build_sbas_pairs

# Define area of interest
aoi = AOI.from_bbox(lon_min=-1.25, lat_min=37.92, lon_max=-0.95, lat_max=38.08)

params = SearchParams(
    aoi=aoi,
    date_start=date(2022, 1, 1),
    date_end=date(2024, 12, 31),
    track_number=110,
    flight_direction="DESCENDING",
)

# Search scenes (no auth needed)
adapter = ASFAdapter()
scenes = adapter.search(params)
print(f"{len(scenes)} scenes found")

# Build SBAS pairs
pairs = build_sbas_pairs(scenes, max_temporal_neighbors=3)
print(f"{len(pairs)} interferometric pairs")

# Submit to HyP3 (requires Earthdata credentials)
from insar_core.credentials import load_earthdata_credentials
creds = load_earthdata_credentials()   # reads from env vars or ~/.netrc

hyp3 = HyP3Adapter(username=creds.username, password=creds.password)
for ref, sec in pairs[:1]:   # test with one pair first
    job = hyp3.submit_pair(ref.granule_name, sec.granule_name, name="my-project")
    print(f"Submitted: {job.hyp3_job_id} [{job.status}]")
```
