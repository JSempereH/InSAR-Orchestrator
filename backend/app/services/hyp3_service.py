from __future__ import annotations

from typing import Optional

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Credential
from app.services.crypto import decrypt
from insar_core.adapters.hyp3 import HyP3Adapter


def _get_earthdata_creds(db: Session) -> tuple[Optional[str], Optional[str]]:
    row = db.query(Credential).filter_by(provider="earthdata").first()
    if not row:
        return None, None
    try:
        return row.username, decrypt(row.encrypted_password)
    except InvalidToken:
        raise HTTPException(
            status_code=422,
            detail=(
                "Stored credentials could not be decrypted. The SECRET_KEY has changed "
                "since they were saved. Please re-enter your Earthdata credentials in Settings."
            ),
        )


def get_hyp3_adapter(db: Session) -> HyP3Adapter:
    username, password = _get_earthdata_creds(db)
    return HyP3Adapter(username=username, password=password)


def get_earthdata_credentials(db: Session) -> tuple[str, str]:
    """Resolve Earthdata credentials: DB (Settings UI) first, then env vars/~/.netrc.

    Unlike get_hyp3_adapter(), this raises if nothing is found - direct ASF
    data-pool downloads need real credentials, they can't silently proceed
    without them the way hyp3_sdk does when given None.
    """
    username, password = _get_earthdata_creds(db)
    if username and password:
        return username, password

    from insar_core.credentials import load_earthdata_credentials

    try:
        creds = load_earthdata_credentials()
        return creds.username, creds.password
    except EnvironmentError:
        raise HTTPException(
            status_code=400,
            detail=(
                "Earthdata credentials not found. Configure them in Settings, "
                "or set EARTHDATA_USER/EARTHDATA_PASS, or add them to ~/.netrc."
            ),
        )


