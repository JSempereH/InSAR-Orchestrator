#!/usr/bin/env bash
# Runs backend (uvicorn) and frontend (vite) together for local development.
# Ctrl+C stops both. Run ./setup.sh first if you haven't already.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -x backend/.venv/bin/uvicorn ]; then
    echo "error: backend/.venv not found. Run ./setup.sh first." >&2
    exit 1
fi

if [ ! -d frontend/node_modules ]; then
    echo "error: frontend/node_modules not found. Run ./setup.sh first." >&2
    exit 1
fi

BACKEND_PID=""

cleanup() {
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "==> Starting backend on http://localhost:8000"
(cd backend && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000) &
BACKEND_PID=$!

echo "==> Starting frontend on http://localhost:5173"
npm run dev --prefix frontend
