"""Signing flow service.

This is the glue between:
  - workflow.resolve()  → creates the Resolution row + customer signing link
  - public sign endpoint → records customer signature
  - admin sign endpoint  → records engineer signature, generates PDF, closes ticket
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.resolution import Resolution, ResolutionMedia
from ..models.shipment import TicketShipment
from ..models.ticket import PaymentStatus, ServiceType, Ticket, TicketStatus, WarrantyStatus
from ..models.user import User, UserRole, ADMIN_MANAGER_ROLES, ADMIN_ROLES, SUPER_ADMIN_ROLES
from .pdf_generator import generate_resolution_pdf
from .storage import get_storage
from .ticket_workflow import _log_event, payment_blocks_close

logger = logging.getLogger("skposcare.signing")

SIGNATURE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB is plenty for a PNG signature


# ----------------------------- helpers ----------------------------------- #

def _new_token() -> str:
    """Cryptographically random URL-safe token (~43 chars)."""
    return secrets.token_urlsafe(32)


def _customer_sign_url(token: str) -> str:
    base = get_settings().customer_sign_url_base.rstrip("/")
    return f"{base}/sign/{token}"


def _field_sign_url(token: str) -> str:
    """Public URL the sub-engineer opens to collect both signatures off-site."""
    base = get_settings().customer_sign_url_base.rstrip("/")
    return f"{base}/field-sign/{token}"


def _reject_if_remote(ticket: Ticket) -> None:
    """Signatures and PDFs don't apply to remote-support tickets — they close
    in a single Resolve & Close step with no resolution document."""
    if ticket.service_type == ServiceType.REMOTE_SUPPORT.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Signatures and the resolution PDF don't apply to remote-support tickets.",
        )


def _require_resolution(ticket: Ticket) -> Resolution:
    if ticket.resolution is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This ticket has no resolution. Engineer must resolve it first.",
        )
    return ticket.resolution


def _reject_if_field_signing(resolution: Resolution) -> None:
    """On-site signing is locked once a remote field-signing link is generated."""
    if resolution.field_sign_link_generated_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A remote signing link is active for this ticket — "
                "on-site signing is disabled."
            ),
        )


def _assert_parts_delivered(db: Session, ticket: Ticket) -> None:
    """Block the RESOLVED → CLOSED transition while any spare-parts shipment
    is still in transit. The ticket can't close until every shipment has been
    marked delivered."""
    undelivered = (
        db.query(TicketShipment)
        .filter(
            TicketShipment.ticket_id == ticket.id,
            TicketShipment.delivered_at.is_(None),
        )
        .count()
    )
    if undelivered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{undelivered} spare-parts shipment(s) still in transit — "
                "mark every shipment delivered before closing the ticket."
            ),
        )


# ----------------------------- public API -------------------------------- #

def create_resolution_with_token(db: Session, ticket: Ticket) -> Tuple[Resolution, str]:
    """Called from workflow.resolve(). Returns (resolution, customer_sign_url)."""
    if ticket.resolution is not None:
        # Idempotency: if resolve is somehow called twice, return the existing one.
        return ticket.resolution, _customer_sign_url(ticket.resolution.customer_sign_token)

    settings = get_settings()
    token = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(days=settings.customer_sign_token_ttl_days)
    resolution = Resolution(
        ticket_id=ticket.id,
        customer_sign_token=token,
        customer_sign_token_expires_at=expires,
    )
    db.add(resolution)
    db.flush()
    return resolution, _customer_sign_url(token)


def get_resolution_by_token(db: Session, token: str) -> Tuple[Ticket, Resolution]:
    res = (
        db.query(Resolution)
        .filter(Resolution.customer_sign_token == token)
        .one_or_none()
    )
    if res is None:
        raise HTTPException(status_code=404, detail="Signing link not found")
    if res.customer_sign_token_expires_at and res.customer_sign_token_expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Signing link expired")
    ticket = db.query(Ticket).filter(Ticket.id == res.ticket_id).one()
    return ticket, res


def record_customer_signature_via_engineer(
    db: Session,
    ticket: Ticket,
    actor: User,
    *,
    signer_name: str,
    image_bytes: bytes,
    content_type: str = "image/png",
) -> Resolution:
    """Variant used when the engineer is on-site collecting the customer's
    signature on their own device. Same effect as record_customer_signature
    but with a role check (only the assigned engineer can capture it)."""
    _reject_if_remote(ticket)
    if ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee can capture the customer's signature",
        )
    resolution = _require_resolution(ticket)
    _reject_if_field_signing(resolution)
    return record_customer_signature(
        db, ticket, resolution,
        signer_name=signer_name,
        image_bytes=image_bytes,
        content_type=content_type,
    )


def record_customer_signature(
    db: Session,
    ticket: Ticket,
    resolution: Resolution,
    *,
    signer_name: str,
    image_bytes: bytes,
    content_type: str = "image/png",
) -> Resolution:
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
        ticket.reference,
        f"signature-customer-{ticket.reference}.png",
    )
    resolution.customer_signer_name = signer_name.strip()[:120]
    resolution.customer_signature_storage_key = meta["storage_url"]
    resolution.customer_signed_at = datetime.now(timezone.utc)
    _log_event(
        db, ticket=ticket, actor=None, event_type="CUSTOMER_SIGNED",
        payload={"signer_name": resolution.customer_signer_name},
    )
    db.commit()
    db.refresh(resolution)
    logger.info("Customer signed %s (%s)", ticket.reference, resolution.customer_signer_name)
    return resolution


def record_engineer_signature(
    db: Session,
    ticket: Ticket,
    actor: User,
    image_bytes: bytes,
    content_type: str = "image/png",
    photo_bytes: Optional[bytes] = None,
    photo_content_type: Optional[str] = None,
) -> Resolution:
    """Engineer signs after the customer has signed; closes the ticket and
    generates the PDF. An optional customer photo, captured at this final
    sign-off step, is persisted before the PDF is built so it's embedded."""
    _reject_if_remote(ticket)
    if ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee can sign",
        )
    resolution = _require_resolution(ticket)
    _reject_if_field_signing(resolution)
    is_third_party = ticket.service_type == ServiceType.THIRD_PARTY_SUPPORT.value
    if is_third_party:
        # Third-party tickets close on the engineer signature alone (no customer
        # signature), but the device name + issue info must be recorded first.
        if not (ticket.third_party_device_name or "").strip() or not (
            ticket.third_party_issue_info or ""
        ).strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Enter the third-party device name and issue info before closing.",
            )
    elif resolution.customer_signed_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Customer hasn't signed yet — engineer signs last",
        )
    if resolution.engineer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Engineer signature already recorded",
        )
    # The engineer signature closes the ticket — block it while parts ship.
    _assert_parts_delivered(db, ticket)
    _validate_signature(image_bytes)
    if photo_bytes:
        _validate_photo(photo_bytes, photo_content_type)

    storage = get_storage()
    meta = storage.save_bytes(
        image_bytes,
        content_type,
        ticket.reference,
        f"signature-engineer-{ticket.reference}.png",
    )
    resolution.engineer_signature_storage_key = meta["storage_url"]
    resolution.engineer_signed_at = datetime.now(timezone.utc)

    # Persist the optional customer photo BEFORE building the PDF so it's
    # embedded in the document.
    if photo_bytes:
        _store_customer_photo(
            storage, ticket.reference, resolution, photo_bytes, photo_content_type,
            datetime.now(timezone.utc),
        )
        _log_event(db, ticket=ticket, actor=actor, event_type="CUSTOMER_PHOTO_CAPTURED")
    db.flush()

    # Build the PDF now that both signatures are in place.
    pdf_bytes = generate_resolution_pdf(ticket, resolution)
    pdf_meta = storage.save_bytes(
        pdf_bytes,
        "application/pdf",
        ticket.reference,
        f"resolution-{ticket.reference}.pdf",
    )
    resolution.pdf_storage_key = pdf_meta["storage_url"]
    resolution.pdf_generated_at = datetime.now(timezone.utc)

    _log_event(
        db, ticket=ticket, actor=actor, event_type="ENGINEER_SIGNED",
    )
    if payment_blocks_close(ticket):
        # Signatures + PDF are done, but payment is still owed on this
        # out-of-warranty ticket — keep it RESOLVED and flag payment pending.
        # An admin/manager/engineer records the payment later to close it.
        _log_event(db, ticket=ticket, actor=actor, event_type="PAYMENT_PENDING")
        logger.info("Engineer signed for %s — held RESOLVED pending payment", ticket.reference)
    else:
        prev = ticket.status
        ticket.status = TicketStatus.CLOSED.value
        _log_event(
            db, ticket=ticket, actor=actor, event_type="CLOSED",
            from_status=prev, to_status=ticket.status,
        )
        logger.info("Engineer signed + PDF generated for %s — closed", ticket.reference)
    db.commit()
    db.refresh(ticket)
    db.refresh(resolution)
    if ticket.status == TicketStatus.CLOSED.value:
        # Local import avoids any import cycle through the notify service.
        from .ticket_notify import notify_sales_rep_closed
        notify_sales_rep_closed(ticket.id)
    return resolution


# ----------------------- remote field signing ---------------------------- #

def generate_field_sign_link(
    db: Session, ticket: Ticket, actor: User
) -> Tuple[Resolution, str]:
    """Generate the no-login link a sub-engineer uses to collect both
    signatures off-site. Returns (resolution, field_sign_url).

    Available once the ticket is RESOLVED and has at least one sub-engineer.
    Idempotent — calling it again returns the existing link. Once generated,
    on-site signing in the admin app is locked for this ticket.
    """
    _reject_if_remote(ticket)
    if (
        actor.role not in ADMIN_MANAGER_ROLES
        and ticket.assigned_engineer_id != actor.id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee, Admin, or Manager can generate the signing link",
        )
    if ticket.status != TicketStatus.RESOLVED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The ticket must be RESOLVED before generating a signing link.",
        )
    if not ticket.sub_engineers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Add a sub-engineer to the ticket before generating a remote signing link.",
        )
    resolution = _require_resolution(ticket)
    if resolution.customer_signed_at is not None or resolution.engineer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Signing has already started for this ticket.",
        )
    if resolution.field_sign_link_generated_at is None:
        resolution.field_sign_link_generated_at = datetime.now(timezone.utc)
        _log_event(
            db, ticket=ticket, actor=actor, event_type="FIELD_SIGN_LINK_GENERATED",
        )
        db.commit()
        db.refresh(resolution)
        logger.info("Field-signing link generated for %s by %s", ticket.reference, actor.username)
    return resolution, _field_sign_url(resolution.customer_sign_token)


def record_field_signatures(
    db: Session,
    ticket: Ticket,
    resolution: Resolution,
    *,
    sub_engineer_id: int,
    customer_signer_name: str,
    customer_image_bytes: bytes,
    engineer_image_bytes: bytes,
    customer_content_type: str = "image/png",
    engineer_content_type: str = "image/png",
    photo_bytes: Optional[bytes] = None,
    photo_content_type: Optional[str] = None,
    media_files: Optional[List[UploadFile]] = None,
) -> Resolution:
    """Record the customer + sub-engineer signatures collected via the remote
    link, generate the resolution PDF, and close the ticket. No auth — the
    token is the auth factor (validated by the caller).

    `media_files` is an optional list of photos/videos the sub-engineer
    attaches as evidence of the on-site work — never blocks sign-off."""
    if resolution.field_sign_link_generated_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This ticket is not set up for remote signing.",
        )
    if resolution.customer_signed_at is not None or resolution.engineer_signed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This resolution has already been signed.",
        )
    sub = next((s for s in ticket.sub_engineers if s.id == sub_engineer_id), None)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a valid sub-engineer for this ticket.",
        )
    # This submission closes the ticket — block it while parts ship.
    _assert_parts_delivered(db, ticket)
    _validate_signature(customer_image_bytes)
    _validate_signature(engineer_image_bytes)

    storage = get_storage()
    cust_meta = storage.save_bytes(
        customer_image_bytes,
        customer_content_type,
        ticket.reference,
        f"signature-customer-{ticket.reference}.png",
    )
    eng_meta = storage.save_bytes(
        engineer_image_bytes,
        engineer_content_type,
        ticket.reference,
        f"signature-subengineer-{ticket.reference}.png",
    )

    now = datetime.now(timezone.utc)
    resolution.customer_signer_name = customer_signer_name.strip()[:120]
    resolution.customer_signature_storage_key = cust_meta["storage_url"]
    resolution.customer_signed_at = now
    resolution.engineer_signature_storage_key = eng_meta["storage_url"]
    resolution.engineer_signed_at = now
    resolution.engineer_signer_name = sub.name.strip()[:120]
    _save_customer_photo(
        storage, ticket.reference, resolution, photo_bytes, photo_content_type,
        db=db, ticket=ticket, captured_at=now,
    )
    _save_resolution_media(
        storage, ticket.reference, resolution, media_files,
        db=db, ticket=ticket,
    )
    resolution.signed_by_sub_engineer_id = sub.id
    db.flush()

    # Both signatures are in place — render the PDF and close the ticket.
    pdf_bytes = generate_resolution_pdf(ticket, resolution)
    pdf_meta = storage.save_bytes(
        pdf_bytes,
        "application/pdf",
        ticket.reference,
        f"resolution-{ticket.reference}.pdf",
    )
    resolution.pdf_storage_key = pdf_meta["storage_url"]
    resolution.pdf_generated_at = now

    _log_event(
        db, ticket=ticket, actor=None, event_type="CUSTOMER_SIGNED",
        payload={"signer_name": resolution.customer_signer_name},
    )
    _log_event(
        db, ticket=ticket, actor=None, event_type="SUB_ENGINEER_SIGNED",
        payload={"signer_name": sub.name},
    )
    if payment_blocks_close(ticket):
        # Out-of-warranty payment still owed — keep RESOLVED, flag pending.
        _log_event(db, ticket=ticket, actor=None, event_type="PAYMENT_PENDING")
    else:
        prev = ticket.status
        ticket.status = TicketStatus.CLOSED.value
        _log_event(
            db, ticket=ticket, actor=None, event_type="CLOSED",
            from_status=prev, to_status=ticket.status,
        )
    db.commit()
    db.refresh(ticket)
    db.refresh(resolution)
    if ticket.status == TicketStatus.CLOSED.value:
        from .ticket_notify import notify_sales_rep_closed
        notify_sales_rep_closed(ticket.id)
    logger.info(
        "Field signatures recorded for %s by sub-engineer %s — ticket closed",
        ticket.reference, sub.name,
    )
    return resolution


def ensure_resolution_field_signing_columns(engine) -> None:
    """Add the remote field-signing columns to `resolutions` if missing.

    `Base.metadata.create_all` doesn't add columns to existing tables, so we
    apply these small ALTERs directly. Idempotent. Works on SQLite + Postgres.
    """
    from sqlalchemy import inspect, text

    from ..database import MIGRATION_SCHEMA, qualify

    insp = inspect(engine)
    if "resolutions" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    columns = {c["name"] for c in insp.get_columns("resolutions", schema=MIGRATION_SCHEMA)}
    ts_type = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "TIMESTAMP"
    tbl = qualify("resolutions")
    stmts: list[str] = []
    if "engineer_signer_name" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN engineer_signer_name VARCHAR(120)")
    if "field_sign_link_generated_at" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN field_sign_link_generated_at {ts_type}")
    if "signed_by_sub_engineer_id" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN signed_by_sub_engineer_id INTEGER")
    # Optional customer photo captured at sign-off.
    if "customer_photo_storage_key" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN customer_photo_storage_key VARCHAR(500)")
    if "customer_photo_captured_at" not in columns:
        stmts.append(f"ALTER TABLE {tbl} ADD COLUMN customer_photo_captured_at {ts_type}")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    logger.info("Added field-signing/photo columns to resolutions (%d)", len(stmts))


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


def _validate_photo(photo_bytes: bytes, content_type: Optional[str]) -> None:
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
    resolution: Resolution,
    photo_bytes: bytes,
    photo_content_type: Optional[str],
    captured_at: datetime,
) -> None:
    """Persist (or replace) the customer photo bytes on the resolution. Pure
    storage + column write — no validation, logging, or commit."""
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


def _save_customer_photo(
    storage,
    reference: str,
    resolution: Resolution,
    photo_bytes: Optional[bytes],
    photo_content_type: Optional[str],
    *,
    db: Session,
    ticket: Ticket,
    captured_at: Optional[datetime] = None,
) -> None:
    """Persist an optional customer photo bundled with a signature submission.
    No-op when no photo was supplied — the photo never blocks sign-off."""
    if not photo_bytes:
        return
    _validate_photo(photo_bytes, photo_content_type)
    _store_customer_photo(
        storage, reference, resolution, photo_bytes, photo_content_type,
        captured_at or datetime.now(timezone.utc),
    )
    _log_event(db, ticket=ticket, actor=None, event_type="CUSTOMER_PHOTO_CAPTURED")


# At most this many photos/videos per field sign-off — a sanity cap on a
# public, unauthenticated endpoint. Per-file size is enforced by storage.
MAX_RESOLUTION_MEDIA = 12


def _save_resolution_media(
    storage,
    reference: str,
    resolution: Resolution,
    media_files: Optional[List[UploadFile]],
    *,
    db: Session,
    ticket: Ticket,
) -> None:
    """Persist optional photos/videos attached at field sign-off. No-op when
    none were supplied — media never blocks closing the ticket."""
    from .storage import validate_upload  # local import to avoid cycle

    files = [f for f in (media_files or []) if f is not None and f.filename]
    if not files:
        return
    if len(files) > MAX_RESOLUTION_MEDIA:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many files (max {MAX_RESOLUTION_MEDIA}).",
        )
    for i, f in enumerate(files, start=1):
        validate_upload(f)  # image/* or video/* only
        ct = (f.content_type or "").lower()
        kind = "video" if ct.startswith("video/") else "photo"
        ext = (f.filename or "").rsplit(".", 1)[-1].lower() if "." in (f.filename or "") else "bin"
        meta = storage.save(f, reference, f"field-{kind}-{reference}-{i}.{ext}")
        resolution.media.append(
            ResolutionMedia(
                kind=kind,
                filename=meta["filename"],
                content_type=meta["content_type"],
                size_bytes=meta["size_bytes"],
                storage_url=meta["storage_url"],
            )
        )
    _log_event(
        db, ticket=ticket, actor=None, event_type="RESOLUTION_MEDIA_UPLOADED",
        payload={"count": len(files)},
    )
