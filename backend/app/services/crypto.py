"""Encryption of the credentials kept in the database.

The key is SECRET_KEY (a Fernet key). Without one, a key is generated once and kept in a
file next to the downloads (mode 0600), so stored credentials stay readable across restarts.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import settings

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


class InvalidSecretKey(RuntimeError):
    pass


def _key_file() -> Path:
    return Path(settings.downloads_dir) / ".secret_key"


def _load_key() -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()
    path = _key_file()
    if path.is_file():
        return path.read_text().strip().encode()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(key.decode())
    logger.warning("SECRET_KEY is not set: generated a key and saved it in %s. Keep that file with the database.", path)
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        try:
            _fernet = Fernet(_load_key())
        except (ValueError, TypeError) as exc:
            raise InvalidSecretKey(
                "SECRET_KEY is not a Fernet key (32 url-safe base64 bytes). Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from exc
    return _fernet


def validate_key() -> None:
    """Fail at start, with a readable message, rather than with a 500 on every credentials call."""
    _get_fernet()


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
