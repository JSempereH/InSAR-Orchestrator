#!/usr/bin/env bash
# Installs everything needed to run the backend and frontend:
#   - Python venv for the backend (via uv), including insar_core
#   - backend/.env with a persistent SECRET_KEY
#   - frontend node_modules
#
# Safe to re-run: skips steps that are already done and never overwrites
# an existing backend/.env.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Checking prerequisites"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed." >&2
    echo "       Install it from https://docs.astral.sh/uv/getting-started/installation/ and re-run this script." >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "error: 'npm' (Node.js) is not installed." >&2
    echo "       Install Node.js >= 18 from https://nodejs.org/ and re-run this script." >&2
    exit 1
fi

echo "==> Setting up backend virtualenv (backend/.venv)"
if [ ! -d backend/.venv ]; then
    uv venv --python 3.11 backend/.venv
else
    echo "    backend/.venv already exists, skipping creation"
fi

echo "==> Installing backend + insar_core dependencies"
# Must run from backend/ so the "../packages/insar_core" relative path
# dependency in requirements.txt resolves correctly.
(cd backend && uv pip install --python .venv -r requirements.txt -r requirements-dev.txt)

echo "==> Preparing backend/.env"
if [ ! -f backend/.env ]; then
    cp backend/.env.example backend/.env
    SECRET_KEY=$(backend/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    # Portable in-place sed for both GNU and BSD/macOS sed.
    sed -i.bak "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" backend/.env
    rm -f backend/.env.bak
    echo "    created backend/.env with a generated SECRET_KEY"
else
    echo "    backend/.env already exists, leaving it untouched"
fi

echo "==> Installing frontend dependencies"
npm install --prefix frontend

cat <<'EOF'

==> Done!

Next steps:
  ./dev.sh          # start backend + frontend together

...or manually:
  cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  cd frontend && npm run dev
EOF
