#!/bin/sh
# Prepares /data on first start, then runs the command.
set -eu

DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA/downloads" "$DATA/rclone" "$DATA/home" /tmp/matplotlib

# Encrypts the stored credentials. Generated once and kept in /data, so it survives
# restarts and is included whenever /data is copied. An explicit SECRET_KEY wins.
if [ -z "${SECRET_KEY:-}" ]; then
    if [ ! -s "$DATA/secret_key" ]; then
        (umask 077; python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > "$DATA/secret_key")
        echo "Generated a new SECRET_KEY in $DATA/secret_key. Keep a copy of it apart from your backups."
    fi
    SECRET_KEY="$(cat "$DATA/secret_key")"
    export SECRET_KEY
fi

exec "$@"
