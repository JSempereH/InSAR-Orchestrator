from __future__ import annotations

import json
from typing import List, Optional

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Credential
from app.services.crypto import decrypt
from insar_core.adapters.egms import EGMSAdapter, EGMSServiceKey
from insar_core.models.egms import EGMSProduct, EGMSSearchParams
from insar_core.models.scene import AOI

EGMS_PROVIDER = "egms"


def _get_service_key(db: Session) -> dict:
    row = db.query(Credential).filter_by(provider=EGMS_PROVIDER).first()
    if not row:
        raise HTTPException(
            status_code=422,
            detail="No EGMS credentials stored. Add your CLMS API service-account key in Settings.",
        )
    try:
        return json.loads(decrypt(row.encrypted_password))
    except InvalidToken:
        raise HTTPException(
            status_code=422,
            detail=(
                "Stored EGMS credentials could not be decrypted. The SECRET_KEY has changed "
                "since they were saved. Please re-upload your CLMS service-account key in Settings."
            ),
        )
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Stored EGMS service-account key is malformed: {exc}",
        )


def get_egms_adapter(db: Session) -> EGMSAdapter:
    key = EGMSServiceKey.from_dict(_get_service_key(db))
    return EGMSAdapter(key)


def list_options(db: Session, kind: str) -> list:
    return get_egms_adapter(db).list_options(kind)


def search_products(
    db: Session,
    geometry: dict,
    level: str,
    release: str,
    direction: Optional[str] = None,
    product_type: Optional[str] = None,
    tile_id: Optional[str] = None,
) -> List[EGMSProduct]:
    params = EGMSSearchParams(
        aoi=AOI(geometry=geometry),
        level=level,
        release=release,
        direction=direction,
        product_type=product_type,
        tile_id=tile_id,
    )
    return get_egms_adapter(db).search(params)
