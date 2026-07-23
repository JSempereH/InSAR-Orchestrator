from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.database import get_db
from app.models import Credential
from app.schemas import CredentialUpsert, CredentialOut
from app.services.crypto import encrypt

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


@router.put("", response_model=CredentialOut)
def upsert_credential(body: CredentialUpsert, db: Session = Depends(get_db)):
    row = db.query(Credential).filter_by(provider=body.provider).first()
    if row:
        row.username = body.username
        row.encrypted_password = encrypt(body.password)
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = Credential(
            provider=body.provider,
            username=body.username,
            encrypted_password=encrypt(body.password),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/{provider}", response_model=CredentialOut)
def get_credential(provider: str, db: Session = Depends(get_db)):
    row = db.query(Credential).filter_by(provider=provider).first()
    if not row:
        raise HTTPException(404, f"No credentials stored for provider '{provider}'")
    return row


@router.delete("/{provider}", status_code=204)
def delete_credential(provider: str, db: Session = Depends(get_db)):
    row = db.query(Credential).filter_by(provider=provider).first()
    if not row:
        raise HTTPException(404, f"No credentials stored for provider '{provider}'")
    db.delete(row)
    db.commit()
