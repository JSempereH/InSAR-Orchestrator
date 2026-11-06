#!/usr/bin/env bash
# Installs InSAR Orchestrator as a systemd *user* service. No root, no containers.
#
#   deploy/install-user.sh                 # listen on 127.0.0.1:8000
#   deploy/install-user.sh --host 0.0.0.0  # also reachable from other machines (set APP_PASSWORD first)
#   deploy/install-user.sh --port 9000
#
# Needs uv and Node.js 18+ (only to build the web app once). MintPy and rclone are
# optional and can be installed in your home too: see docs/deploy.md.
# Safe to re-run: it rebuilds and restarts, and never overwrites backend/.env.
set -euo pipefail

HOST=127.0.0.1
PORT=8000
while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "error: unknown option $1" >&2; exit 1 ;;
    esac
done

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"

command -v systemctl >/dev/null 2>&1 || { echo "error: systemd is not available on this machine." >&2; exit 1; }

echo "==> Installing the app (backend venv, dependencies, frontend packages)"
./setup.sh

echo "==> Building the web app"
npm run build --prefix frontend

if ! grep -q '^APP_PASSWORD=.' backend/.env 2>/dev/null; then
    if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
        echo "error: refusing to listen on $HOST without a password." >&2
        echo "       Add a line APP_PASSWORD=<something long> to backend/.env and run this again." >&2
        exit 1
    fi
    echo "    note: APP_PASSWORD is not set in backend/.env. Fine for localhost; set it before exposing the app."
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT="$UNIT_DIR/insar-orchestrator.service"

echo "==> Writing $UNIT"
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=InSAR Orchestrator
After=network-online.target

[Service]
WorkingDirectory=$ROOT/backend
EnvironmentFile=$ROOT/backend/.env
Environment=FRONTEND_DIR=$ROOT/frontend/dist
# Services started by systemd do not read your shell profile, so list extra tool
# folders here (for example where MintPy or rclone are installed).
Environment=PATH=%h/.local/bin:%h/micromamba/envs/insar/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$ROOT/backend/.venv/bin/uvicorn app.main:app --host $HOST --port $PORT --proxy-headers
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=default.target
UNIT_EOF

systemctl --user daemon-reload
systemctl --user enable --now insar-orchestrator.service
sleep 2
systemctl --user --no-pager --lines=5 status insar-orchestrator.service || true

cat <<DONE_EOF

==> Running at http://$HOST:$PORT

To keep it running after you log out and start it at boot, run once:
    loginctl enable-linger "\$USER"
(on some systems this asks for sudo the first time).

Logs:    journalctl --user -u insar-orchestrator -f
Restart: systemctl --user restart insar-orchestrator
DONE_EOF
