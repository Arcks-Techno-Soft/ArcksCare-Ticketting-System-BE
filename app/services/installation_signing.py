"""Installation signing flow — mirrors services/signing.py."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.installation import Installation, InstallationResolution, InstallationStatus
from ..models.user import User
from .installation_pdf import generate_installation_pdf
from .installation_workflow import _log_event
from .storage import get_storage

logger = logging.getLogger("skposcare.installation_signing")

SIGNATURE_MAX_BYTES = 2 * 1024 * 1024


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _customer_sign_url(token: str) -> str:
    base = get_settings().customer_sign_url_base.rstrip("/")
    return f"{base}/sign-install/{token}"


def _require_resolution(installation: Installation) -> InstallationResolution:
    if installation.resolution is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This installation has no resolution yet — close it first.",
        )
    return installation.resolution


def create_resolution_with_token(
    db: Session, installation: Installation
) -> Tuple[InstallationResolution, str]:
    if installation.resolution is not None:
        return installation.resolution, _customer_sign_url(
            installation.resolution.customer_sign_token
        )

    settings = get_settings()
    token = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(days=settings.customer_sign_token_ttl_days)
    resolution = InstallationResolution(
        installation_id=installation.id,
        customer_sign_token=token,
        customer_sign_token_expires_at=expires,
    )
    db.add(resolution)
    db.flush()
    return resolution, _customer_sign_url(token)


def record_customer_signature_via_engineer(
    db: Session,
    installation: Installation,
    actor: User,
    *,
    signer_name: str,
    image_bytes: bytes,
    content_type: str = "image/png",
) -> InstallationResolution:
    if installation.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee can capture the customer's signature",
        )
    resolution = _require_resolution(installation)
    if resolution.customer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Customer signature already recorded",
        )
    _validate_signature(image_bytes)

    storage = get_storage()
    meta = storage.save_bytes(
        image_bytes,
        content_type,
        installation.reference,
        f"signature-customer-{installation.reference}.png",
    )
    resolution.customer_signer_name = signer_name.strip()[:120]
    resolution.customer_signature_storage_key = meta["storage_url"]
    resolution.customer_signed_at = datetime.now(timezone.utc)
    _log_event(
        db, installation=installation, actor=None, event_type="CUSTOMER_SIGNED",
        payload={"signer_name": resolution.customer_signer_name},
    )
    db.commit()
    db.refresh(resolution)
    logger.info(
        "Customer signed installation %s (%s)",
        installation.reference, resolution.customer_signer_name,
    )
    return resolution


def record_engineer_signature(
    db: Session,
    installation: Installation,
    actor: User,
    image_bytes: bytes,
    content_type: str = "image/png",
    photo_bytes: bytes | None = None,
    photo_content_type: str | None = None,
) -> InstallationResolution:
    if installation.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee can sign",
        )
    resolution = _require_resolution(installation)
    if resolution.customer_signed_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Customer hasn't signed yet — engineer signs last",
        )
    if resolution.engineer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Engineer signature already recorded",
        )
    _validate_signature(image_bytes)
    if photo_bytes:
        _validate_photo(photo_bytes, photo_content_type)

    storage = get_storage()
    meta = storage.save_bytes(
        image_bytes,
        content_type,
        installation.reference,
        f"signature-engineer-{installation.reference}.png",
    )
    resolution.engineer_signature_storage_key = meta["storage_url"]
    resolution.engineer_signed_at = datetime.now(timezone.utc)

    # Persist the optional customer photo BEFORE building the PDF so it's
    # embedded in the document.
    if photo_bytes:
        _store_customer_photo(
            storage, installation.reference, resolution, photo_bytes, photo_content_type,
            datetime.now(timezone.utc),
        )
        _log_event(
            db, installation=installation, actor=actor, event_type="CUSTOMER_PHOTO_CAPTURED",
        )
    db.flush()

    pdf_bytes = generate_installation_pdf(installation, resolution)
    pdf_meta = storage.save_bytes(
        pdf_bytes,
        "application/pdf",
        installation.reference,
        f"installation-{installation.reference}.pdf",
    )
    resolution.pdf_storage_key = pdf_meta["storage_url"]
    resolution.pdf_generated_at = datetime.now(timezone.utc)

    prev = installation.status
    installation.status = InstallationStatus.CLOSED.value
    installation.closed_at = datetime.now(timezone.utc)
    _log_event(
        db, installation=installation, actor=actor, event_type="ENGINEER_SIGNED",
    )
    _log_event(
        db, installation=installation, actor=actor, event_type="CLOSED",
        from_status=prev, to_status=installation.status,
    )
    db.commit()
    db.refresh(installation)
    db.refresh(resolution)
    logger.info(
        "Engineer signed + PDF generated for installation %s",
        installation.reference,
    )
    return resolution


def _validate_signature(image_bytes: bytes) -> None:
    if not image_bytes or len(image_bytes) < 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Signature image looks empty",
        )
    if len(image_bytes) > SIGNATURE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Signature image too large",
        )


# Customer photo captured at sign-off — a real camera image, so allow more
# headroom than the 2 MB signature PNG.
PHOTO_MAX_BYTES = 12 * 1024 * 1024


def _validate_photo(photo_bytes: bytes, content_type: str | None) -> None:
    if not photo_bytes or len(photo_bytes) < 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Customer photo looks empty",
        )
    if len(photo_bytes) > PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Customer photo too large (max 12 MB)",
        )
    ct = (content_type or "").lower()
    if ct and not ct.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Customer photo must be an image",
        )


def _store_customer_photo(
    storage,
    reference: str,
    resolution: InstallationResolution,
    photo_bytes: bytes,
    photo_content_type: str | None,
    captured_at: datetime,
) -> None:
    """Persist (or replace) the customer photo bytes — no validation/log/commit."""
    ct = (photo_content_type or "image/jpeg").lower()
    ext = "png" if "png" in ct else "jpg"
    meta = storage.save_bytes(
        photo_bytes,
        photo_content_type or "image/jpeg",
        reference,
        f"photo-customer-{reference}.{ext}",
    )
    resolution.customer_photo_storage_key = meta["storage_url"]
    resolution.customer_photo_captured_at = captured_at


