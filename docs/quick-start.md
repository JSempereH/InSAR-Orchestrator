# Quick start

[![The pipeline, step by step](images/pipeline.gif)](images/pipeline.mp4)

From zero to a ground-motion time series in four steps.

## 1. Install and start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node.js](https://nodejs.org/) 18 or newer.

```bash
git clone https://github.com/JSempereH/InSAR-Orchestrator.git
cd InSAR-Orchestrator
./setup.sh   # once: installs everything and creates backend/.env
./dev.sh     # starts the app, Ctrl+C stops it
```

Open <http://localhost:5173>.

To run it on a server instead, see [Deploy](deploy.md).

## 2. Install MintPy (only for the ASF way)

[MintPy](https://github.com/insarlab/MintPy) turns HyP3 results into a time series. Without it everything works except the *Run MintPy* step:

```bash
uv pip install --python backend/.venv mintpy
```

## 3. Add your credentials

Open **Settings** and enter what you will use. See [Credentials](credentials.md).

## 4. Get data and look at it

1. Pick a way to [get data](get-data.md): **ASF** (your own time series, anywhere) or **EGMS** (ready-made, Europe).
2. Open the result in the **Viewer**, click a point, and read its [time series](time-series-analysis.md).

## If something goes wrong

- **"Stored credentials could not be decrypted"**: `SECRET_KEY` in `backend/.env` changed. Enter your credentials again in Settings.
- **"smallbaselineApp.py was not found"**: MintPy is not installed (step 2).
- **The Viewer says "No MintPy results yet"**: run MintPy first, from the project's page in **ASF**.
- **The map stays on "Loading map…"**: the map background comes from the internet. Check your connection and reload.
- **The MintPy panel shows a yellow warning about ERA5**: add a Copernicus Climate login, see [Credentials](credentials.md#copernicus-climate-data-store-optional-but-recommended).
