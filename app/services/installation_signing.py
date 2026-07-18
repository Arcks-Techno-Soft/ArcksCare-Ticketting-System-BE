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
from ..models.user import ADMIN_MANAGER_ROLES, User
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
    # Notify the credited sales rep on WhatsApp that their installation closed.
    # Fire-and-forget (own thread + DB session); imported locally to avoid any
    # import cycle. Guarded so it only fires on the NEW -> CLOSED transition.
    if (
        installation.status == InstallationStatus.CLOSED.value
        and prev != InstallationStatus.CLOSED.value
    ):
        from .installation_notify import notify_sales_rep_closed

        notify_sales_rep_closed(installation.id)
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




# --------------------------- off-field signing --------------------------- #
#
# An Admin/Manager/assignee generates a tokenized public link and sends it to
# the sub-engineer manually (WhatsApp etc.). The sub-engineer opens it on site
# and captures the customer's signature, their own signature, and photos of the
# completed installation. Mirrors services/signing.py for tickets.

MAX_INSTALLATION_MEDIA = 12


def _field_sign_url(token: str) -> str:
    base = get_settings().customer_sign_url_base.rstrip("/")
    return f"{base}/field-sign-install/{token}"


def generate_field_sign_link(
    db: Session, installation: Installation, actor: User
) -> Tuple[InstallationResolution, str]:
    """Mint (or re-return) the off-field signing link for an installation.

    Reuses the resolution's existing `customer_sign_token` rather than minting
    a new one, and stamps `field_sign_link_generated_at` to switch the
    installation into off-field mode (on-site app signing is then locked).
    Idempotent — calling it again returns the same URL.
    """
    if actor.role not in ADMIN_MANAGER_ROLES and installation.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee, a Manager, or an Admin can generate this link.",
        )
    resolution = _require_resolution(installation)
    if resolution.engineer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This installation is already signed off.",
        )
    if not installation.sub_engineers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Add at least one sub-engineer before generating the signing link.",
        )
    if resolution.field_sign_link_generated_at is None:
        resolution.field_sign_link_generated_at = datetime.now(timezone.utc)
        _log_event(
            db, installation=installation, actor=actor,
            event_type="FIELD_SIGN_LINK_GENERATED",
        )
        db.commit()
        db.refresh(resolution)
    return resolution, _field_sign_url(resolution.customer_sign_token)


def _save_installation_media(storage, reference: str, resolution, media_files) -> int:
    """Persist installation photos captured at field sign-off. Returns count."""
    from .storage import validate_upload  # local import to avoid cycle

    from ..models.installation import InstallationResolutionMedia

    files = [f for f in (media_files or []) if f is not None and f.filename]
    if not files:
        return 0
    if len(files) > MAX_INSTALLATION_MEDIA:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many files (max {MAX_INSTALLATION_MEDIA}).",
        )
    for i, f in enumerate(files, start=1):
        validate_upload(f)  # image/* or video/* only
        ct = (f.content_type or "").lower()
        kind = "video" if ct.startswith("video/") else "photo"
        ext = (f.filename or "").rsplit(".", 1)[-1].lower() if "." in (f.filename or "") else "bin"
        meta = storage.save(f, reference, f"field-{kind}-{reference}-{i}.{ext}")
        resolution.media.append(
            InstallationResolutionMedia(
                kind=kind,
                filename=meta["filename"],
                content_type=meta["content_type"],
                size_bytes=meta["size_bytes"],
                storage_url=meta["storage_url"],
            )
        )
    return len(files)


def record_field_signatures(
    db: Session,
    installation: Installation,
    resolution: InstallationResolution,
    *,
    sub_engineer_id: int,
    customer_signer_name: str,
    customer_image_bytes: bytes,
    engineer_image_bytes: bytes,
    customer_content_type: str = "image/png",
    engineer_content_type: str = "image/png",
    photo_bytes: bytes | None = None,
    photo_content_type: str | None = None,
    media_files=None,
) -> InstallationResolution:
    """Capture both signatures + installation photos from the public field page.

    No authenticated actor — the token is the auth factor, and the sub-engineer
    self-selects from the installation's own list (validated below).
    """
    if resolution.field_sign_link_generated_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Off-field signing is not enabled for this installation.",
        )
    if resolution.engineer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This installation is already signed off.",
        )
    sub = next(
        (s for s in installation.sub_engineers if s.id == sub_engineer_id), None
    )
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a valid sub-engineer for this installation.",
        )
    if not (customer_signer_name or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Customer name is required.",
        )
    _validate_signature(customer_image_bytes)
    _validate_signature(engineer_image_bytes)
    if photo_bytes:
        _validate_photo(photo_bytes, photo_content_type)

    storage = get_storage()
    now = datetime.now(timezone.utc)
    cust_meta = storage.save_bytes(
        customer_image_bytes, customer_content_type, installation.reference,
        f"signature-customer-{installation.reference}.png",
    )
    eng_meta = storage.save_bytes(
        engineer_image_bytes, engineer_content_type, installation.reference,
        f"signature-subengineer-{installation.reference}.png",
    )
    resolution.customer_signer_name = customer_signer_name.strip()[:120]
    resolution.customer_signature_storage_key = cust_meta["storage_url"]
    resolution.customer_signed_at = now
    resolution.engineer_signature_storage_key = eng_meta["storage_url"]
    resolution.engineer_signed_at = now
    resolution.engineer_signer_name = sub.name.strip()[:120]
    resolution.signed_by_sub_engineer_id = sub.id
    if photo_bytes:
        _store_customer_photo(
            storage, installation.reference, resolution, photo_bytes,
            photo_content_type, now,
        )
    media_count = _save_installation_media(
        storage, installation.reference, resolution, media_files
    )
    db.flush()

    pdf_bytes = generate_installation_pdf(installation, resolution)
    pdf_meta = storage.save_bytes(
        pdf_bytes, "application/pdf", installation.reference,
        f"installation-{installation.reference}.pdf",
    )
    resolution.pdf_storage_key = pdf_meta["storage_url"]
    resolution.pdf_generated_at = now

    _log_event(
        db, installation=installation, actor=None, event_type="CUSTOMER_SIGNED",
        payload={"signer_name": resolution.customer_signer_name},
    )
    _log_event(
        db, installation=installation, actor=None, event_type="SUB_ENGINEER_SIGNED",
        payload={"signer_name": sub.name},
    )
    if media_count:
        _log_event(
            db, installation=installation, actor=None,
            event_type="RESOLUTION_MEDIA_UPLOADED", payload={"count": media_count},
        )
    prev = installation.status
    installation.status = InstallationStatus.CLOSED.value
    installation.closed_at = now
    _log_event(
        db, installation=installation, actor=None, event_type="CLOSED",
        from_status=prev, to_status=installation.status,
    )
    db.commit()
    db.refresh(installation)
    db.refresh(resolution)
    if prev != InstallationStatus.CLOSED.value:
        from .installation_notify import notify_sales_rep_closed

        notify_sales_rep_closed(installation.id)
    logger.info(
        "Field signatures recorded for installation %s by sub-engineer %s — closed",
        installation.reference, sub.name,
    )
    return resolution


def ensure_installation_field_signing_columns(engine) -> None:
    """Add the off-field signing columns to `installation_resolutions`.

    `create_all` doesn't add columns to existing tables, so apply small
    idempotent ALTERs. Works on SQLite + Postgres; scoped to DB_SCHEMA.
    """
    from sqlalchemy import inspect, text

    from ..database import MIGRATION_SCHEMA, qualify

    insp = inspect(engine)
    if "installation_resolutions" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    columns = {
        c["name"]
        for c in insp.get_columns("installation_resolutions", schema=MIGRATION_SCHEMA)
    }
    ts_type = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "TIMESTAMP"
    tbl = qualify("installation_resolutions")
    stmts: list[str] = []
    if "engineer_signer_name" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN engineer_signer_name VARCHAR(120)")
    if "field_sign_link_generated_at" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN field_sign_link_generated_at {ts_type}")
    if "signed_by_sub_engineer_id" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN signed_by_sub_engineer_id INTEGER")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    logger.info(
        "Added off-field signing columns to installation_resolutions (%d)", len(stmts)
    )


def get_installation_resolution_by_token(
    db: Session, token: str
) -> Tuple[Installation, InstallationResolution]:
    """Resolve a public signing token to its installation + resolution."""
    res = (
        db.query(InstallationResolution)
        .filter(InstallationResolution.customer_sign_token == token)
        .one_or_none()
    )
    if res is None:
        raise HTTPException(status_code=404, detail="Signing link not found")
    if (
        res.customer_sign_token_expires_at
        and res.customer_sign_token_expires_at.replace(tzinfo=timezone.utc)
        < datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=410, detail="Signing link expired")
    inst = (
        db.query(Installation)
        .filter(Installation.id == res.installation_id)
        .one()
    )
    return inst, res
