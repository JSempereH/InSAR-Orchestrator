# One image with everything: the web app, the API, MintPy and rclone.
#
#   docker build -t insar-orchestrator .
#
# Runs as an unprivileged user (never root), so it works unchanged with rootless
# Podman or rootless Docker. See docs/deploy.md.

# ── 1. Build the web app ─────────────────────────────────────────────────────
FROM node:20-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── 2. Runtime ───────────────────────────────────────────────────────────────
# micromamba images run as an unprivileged user (uid 1000) and conda-forge ships
# MintPy, GDAL and rclone for both amd64 and arm64, which is the only practical
# way to get MintPy's GDAL dependency without compiling it.
FROM mambaorg/micromamba:2.0-debian12-slim

ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN micromamba install -y -n base -c conda-forge \
        python=3.11 mintpy gdal rclone uv \
    && micromamba clean -a -y

WORKDIR /app/backend
COPY --chown=$MAMBA_USER:$MAMBA_USER packages/insar_core /app/packages/insar_core
COPY --chown=$MAMBA_USER:$MAMBA_USER backend/requirements.txt /app/backend/requirements.txt
# Run from /app/backend so the relative `insar-core @ ../packages/insar_core`
# dependency in requirements.txt resolves, exactly like setup.sh does.
RUN uv pip install --python "$(which python)" --no-cache -r requirements.txt

COPY --chown=$MAMBA_USER:$MAMBA_USER backend/app /app/backend/app
COPY --chown=$MAMBA_USER:$MAMBA_USER --from=frontend /app/dist /app/frontend-dist
COPY --chown=$MAMBA_USER:$MAMBA_USER docker/entrypoint.sh /app/entrypoint.sh

# Everything the app writes lives under /data (mount it from the host). HOME is
# there too, so the container also works when started with an arbitrary uid.
# So `docker exec` finds python, MintPy and rclone without activating the environment.
ENV PATH=/opt/conda/bin:$PATH
ENV DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/insar_app.db \
    DOWNLOADS_DIR=/data/downloads \
    RCLONE_CONFIG=/data/rclone/rclone.conf \
    FRONTEND_DIR=/app/frontend-dist \
    HOME=/data/home \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# micromamba's own entrypoint activates the conda environment, then runs ours.
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
