# Build context must be the InSAR-Orchestrator repo root (not backend/), so
# ../packages/insar_core is reachable - see docker-compose.yml at the
# Sentinel-Orchestrator root, or build manually with:
#   docker build -f backend/Dockerfile -t insar-backend ..
FROM python:3.11-slim

# Plain pip rejects the `name @ ../relative/path` syntax requirements.txt
# uses for insar-core (it wants a file:// URL); uv's pip-compatible installer
# accepts it, exactly like setup.sh already relies on locally.
RUN pip install --no-cache-dir uv

WORKDIR /app

COPY packages/insar_core /app/packages/insar_core
COPY backend/requirements.txt /app/backend/requirements.txt

# Run from /app/backend, exactly like setup.sh, so the relative path
# dependency `insar-core @ ../packages/insar_core` in requirements.txt
# resolves against /app/packages/insar_core.
WORKDIR /app/backend
RUN uv pip install --system --no-cache -r requirements.txt

COPY backend/app /app/backend/app

# MintPy (smallbaselineApp.py) is NOT installed here - it's a heavy scientific
# stack, and the /mintpy endpoints already fail with a clear 422 when it's
# missing. If you want SBAS runs from inside this container, add MintPy to
# this image (`pip install mintpy` plus its own non-Python prerequisites)
# rather than assuming it's available on the host.
ENV DATABASE_URL=sqlite:////data/insar_app.db
ENV DOWNLOADS_DIR=/data/downloads
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
