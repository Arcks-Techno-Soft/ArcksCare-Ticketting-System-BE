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
from typing import Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.resolution import Resolution
from ..models.shipment import TicketShipment
from ..models.ticket import ServiceType, Ticket, TicketStatus
from ..models.user import User, UserRole
from .pdf_generator import generate_resolution_pdf
from .storage import get_storage
from .ticket_workflow import _log_event

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
) -> Resolution:
    """Engineer signs after the customer has signed; closes the ticket and generates the PDF."""
    _reject_if_remote(ticket)
    if ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee can sign",
        )
    resolution = _require_resolution(ticket)
    _reject_if_field_signing(resolution)
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
    # The engineer signature closes the ticket — block it while parts ship.
    _assert_parts_delivered(db, ticket)
    _validate_signature(image_bytes)

    storage = get_storage()
    meta = storage.save_bytes(
        image_bytes,
        content_type,
        ticket.reference,
        f"signature-engineer-{ticket.reference}.png",
    )
    resolution.engineer_signature_storage_key = meta["storage_url"]
    resolution.engineer_signed_at = datetime.now(timezone.utc)
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

    prev = ticket.status
    ticket.status = TicketStatus.CLOSED.value
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ENGINEER_SIGNED",
    )
    _log_event(
        db, ticket=ticket, actor=actor, event_type="CLOSED",
        from_status=prev, to_status=ticket.status,
    )
    db.commit()
    db.refresh(ticket)
    db.refresh(resolution)
    logger.info("Engineer signed + PDF generated for %s", ticket.reference)
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
        actor.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value)
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
) -> Resolution:
    """Record the customer + sub-engineer signatures collected via the remote
    link, generate the resolution PDF, and close the ticket. No auth — the
    token is the auth factor (validated by the caller)."""
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

    prev = ticket.status
    ticket.status = TicketStatus.CLOSED.value
    _log_event(
        db, ticket=ticket, actor=None, event_type="CUSTOMER_SIGNED",
        payload={"signer_name": resolution.customer_signer_name},
    )
    _log_event(
        db, ticket=ticket, actor=None, event_type="SUB_ENGINEER_SIGNED",
        payload={"signer_name": sub.name},
    )
    _log_event(
        db, ticket=ticket, actor=None, event_type="CLOSED",
        from_status=prev, to_status=ticket.status,
    )
    db.commit()
    db.refresh(ticket)
    db.refresh(resolution)
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
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    logger.info("Added field-signing columns to resolutions (%d)", len(stmts))


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
