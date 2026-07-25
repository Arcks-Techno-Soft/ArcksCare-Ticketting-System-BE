"""Installation workflow state machine.

Mirrors ticket_workflow.py but for installations. Keeps the router thin and
the audit log consistent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..database import MIGRATION_SCHEMA, qualify
from ..models.installation import (
    Installation,
    InstallationAttempt,
    InstallationEvent,
    InstallationNote,
    InstallationStatus,
)
from ..models.user import User, UserRole, ADMIN_MANAGER_ROLES, ADMIN_ROLES, SUPER_ADMIN_ROLES

logger = logging.getLogger("skposcare.installation_workflow")


# Columns backing the optional uploaded invoice document. `create_all` adds new
# tables but never new columns on existing tables, so this idempotent ALTER
# backfills them for databases that pre-date the feature. Scoped to DB_SCHEMA so
# a test backend can never touch the public/production table. SQLite + Postgres.
_INVOICE_DOC_COLUMNS = {
    "invoice_document_filename": "VARCHAR(255)",
    "invoice_document_content_type": "VARCHAR(120)",
    "invoice_document_size_bytes": "INTEGER",
    "invoice_document_storage_key": "VARCHAR(500)",
    "invoice_document_uploaded_at": "TIMESTAMP WITH TIME ZONE",
}

# Site-address columns. Same idempotent-ALTER rationale as above.
_ADDRESS_COLUMNS = {
    "address_line1": "VARCHAR(200)",
    "address_line2": "VARCHAR(200)",
    "address_line3": "VARCHAR(200)",
    "city": "VARCHAR(80)",
    "state": "VARCHAR(80)",
    "pincode": "VARCHAR(10)",
    "latitude": "DOUBLE PRECISION",
    "longitude": "DOUBLE PRECISION",
}


def ensure_installation_invoice_document_columns(engine: Engine) -> None:
    """Add installations.invoice_document_* columns if any are missing."""
    insp = inspect(engine)
    if "installations" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    existing = {c["name"] for c in insp.get_columns("installations", schema=MIGRATION_SCHEMA)}
    missing = {k: v for k, v in _INVOICE_DOC_COLUMNS.items() if k not in existing}
    if not missing:
        return
    # SQLite has no "TIMESTAMP WITH TIME ZONE"; fall back to a plain timestamp.
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        for name, ddl in missing.items():
            if is_sqlite and ddl.startswith("TIMESTAMP"):
                ddl = "TIMESTAMP"
            conn.execute(text(f"ALTER TABLE {qualify('installations')} ADD COLUMN {name} {ddl}"))


def ensure_installation_sales_rep_column(engine: Engine) -> None:
    """Add installations.sales_rep_id if missing.

    Credits a SALES user with sourcing the installation. Nullable so rows that
    pre-date the feature stay valid. Same idempotent-ALTER rationale as the
    other installation columns; works on SQLite (dev) and Postgres (prod).
    """
    insp = inspect(engine)
    if "installations" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    existing = {c["name"] for c in insp.get_columns("installations", schema=MIGRATION_SCHEMA)}
    if "sales_rep_id" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE {qualify('installations')} ADD COLUMN sales_rep_id INTEGER")
        )


def ensure_installation_products_column(engine: Engine) -> None:
    """Add installations.products_for_installation if it's missing.

    Idempotent. `create_all` doesn't add columns to existing tables, so we
    ALTER directly. Nullable so rows that pre-date the feature stay valid.
    """
    insp = inspect(engine)
    if "installations" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    existing = {c["name"] for c in insp.get_columns("installations", schema=MIGRATION_SCHEMA)}
    if "products_for_installation" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE {qualify('installations')} ADD COLUMN products_for_installation TEXT")
        )


def ensure_installation_address_columns(engine: Engine) -> None:
    """Add installations site-address columns if any are missing."""
    insp = inspect(engine)
    if "installations" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    existing = {c["name"] for c in insp.get_columns("installations", schema=MIGRATION_SCHEMA)}
    missing = {k: v for k, v in _ADDRESS_COLUMNS.items() if k not in existing}
    if not missing:
        return
    # SQLite has no DOUBLE PRECISION type name; use its REAL affinity instead.
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        for name, ddl in missing.items():
            if is_sqlite and ddl == "DOUBLE PRECISION":
                ddl = "REAL"
            conn.execute(text(f"ALTER TABLE {qualify('installations')} ADD COLUMN {name} {ddl}"))


def ensure_installation_resolution_photo_columns(engine: Engine) -> None:
    """Add installation_resolutions.customer_photo_* columns if missing.

    Idempotent. `create_all` doesn't add columns to existing tables, so we
    ALTER directly. Works on SQLite + Postgres.
    """
    insp = inspect(engine)
    if "installation_resolutions" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    existing = {
        c["name"] for c in insp.get_columns("installation_resolutions", schema=MIGRATION_SCHEMA)
    }
    ts_type = "TIMESTAMP" if engine.dialect.name == "sqlite" else "TIMESTAMPTZ"
    cols = {
        "customer_photo_storage_key": "VARCHAR(500)",
        "customer_photo_captured_at": ts_type,
    }
    missing = {k: v for k, v in cols.items() if k not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(
                text(f"ALTER TABLE {qualify('installation_resolutions')} ADD COLUMN {name} {ddl}")
            )


def ensure_installation_note_attempt_column(engine: Engine) -> None:
    """Add installation_notes.installation_attempt_id if it's missing.

    The installation_attempts table itself is created by `create_all`; this
    idempotent ALTER backfills the FK column on the existing notes table.
    Plain INTEGER (no FK constraint) keeps it portable across SQLite/Postgres.
    """
    insp = inspect(engine)
    if "installation_notes" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    existing = {c["name"] for c in insp.get_columns("installation_notes", schema=MIGRATION_SCHEMA)}
    if "installation_attempt_id" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE {qualify('installation_notes')} ADD COLUMN installation_attempt_id INTEGER")
        )


def _log_event(
    db: Session,
    *,
    installation: Installation,
    actor: Optional[User],
    event_type: str,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    payload: Optional[dict] = None,
    note: Optional[str] = None,
) -> InstallationEvent:
    e = InstallationEvent(
        installation_id=installation.id,
        actor_user_id=actor.id if actor else None,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        payload=payload,
        note=note,
    )
    db.add(e)
    return e


def _require_status(installation: Installation, allowed: set[str]) -> None:
    if installation.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Action not allowed in status {installation.status}. "
                f"Allowed: {sorted(allowed)}"
            ),
        )


def _require_assignee(installation: Installation, actor: User) -> None:
    if installation.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This installation is assigned to a different user",
        )


# --------------------------- transitions --------------------------------- #

def assign(db: Session, installation: Installation, actor: User, engineer_id: int) -> tuple[Installation, User]:
    """Assign or reassign. Admin/Manager only. Allowed in NEW or ASSIGNED."""
    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only Manager or Admin can assign")

    engineer = db.query(User).filter(User.id == engineer_id).one_or_none()
    if engineer is None or not engineer.active:
        raise HTTPException(status_code=400, detail="Invalid assignee")

    _require_status(installation, {InstallationStatus.NEW.value, InstallationStatus.ASSIGNED.value})

    prev_status = installation.status
    is_reassign = (
        installation.assigned_engineer_id is not None
        and installation.assigned_engineer_id != engineer.id
    )

    installation.assigned_engineer_id = engineer.id
    installation.assigned_by_id = actor.id
    installation.assigned_at = datetime.now(timezone.utc)
    installation.status = InstallationStatus.ASSIGNED.value

    _log_event(
        db, installation=installation, actor=actor,
        event_type="REASSIGNED" if is_reassign else "ASSIGNED",
        from_status=prev_status, to_status=installation.status,
        payload={"engineer_id": engineer.id, "engineer_name": engineer.name},
    )
    db.commit()
    db.refresh(installation)
    logger.info(
        "Installation %s %s to %s by %s",
        installation.reference,
        "reassigned" if is_reassign else "assigned",
        engineer.username,
        actor.username,
    )
    return installation, engineer


def add_note(
    db: Session,
    installation: Installation,
    actor: User,
    body: str,
    attachments: Optional[list[dict]] = None,
) -> InstallationNote:
    """Add a work note. Allowed for the assignee, Admin, or Manager.
    Notes can be added while the work is in progress (ASSIGNED status).

    Optional `attachments` is a list of file metadata dicts returned by the
    storage backend (filename, content_type, size_bytes, storage_url).
    """
    from ..models.installation import InstallationNoteAttachment  # local

    can_add = (
        actor.role in ADMIN_MANAGER_ROLES
        or installation.assigned_engineer_id == actor.id
    )
    if not can_add:
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can add notes",
        )
    _require_status(installation, {InstallationStatus.ASSIGNED.value})

    open_attempt = _open_attempt(db, installation)
    if open_attempt is None:
        raise HTTPException(
            status_code=409,
            detail="Start an attempt before adding notes.",
        )

    note = InstallationNote(
        installation_id=installation.id,
        installation_attempt_id=open_attempt.id,
        author_id=actor.id,
        body=body.strip(),
    )
    db.add(note)
    db.flush()

    if attachments:
        for meta in attachments:
            db.add(InstallationNoteAttachment(
                installation_note_id=note.id,
                filename=meta["filename"],
                content_type=meta["content_type"],
                size_bytes=int(meta["size_bytes"]),
                storage_url=meta["storage_url"],
            ))

    _log_event(
        db, installation=installation, actor=actor, event_type="NOTE_ADDED",
        payload={
            "note_preview": body.strip()[:120],
            "attachment_count": len(attachments or []),
        },
    )
    db.commit()
    db.refresh(note)
    return note


def update_invoice(
    db: Session, installation: Installation, actor: User, invoice_number: str
) -> Installation:
    """Edit the invoice number.

    Allowed for the assignee, Admin, or Manager, at any time BEFORE the
    installation is CLOSED. Once CLOSED the invoice is frozen (it has been
    signed off and baked into the generated PDF).
    """
    can_edit = (
        actor.role in ADMIN_MANAGER_ROLES
        or installation.assigned_engineer_id == actor.id
    )
    if not can_edit:
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can edit the invoice number",
        )
    if installation.status == InstallationStatus.CLOSED.value:
        raise HTTPException(
            status_code=409,
            detail="The invoice number can't be changed after the installation is closed.",
        )

    new_invoice = invoice_number.strip()
    if not new_invoice:
        raise HTTPException(status_code=422, detail="Invoice number cannot be empty.")

    prev = installation.invoice_number
    if new_invoice == prev:
        return installation

    installation.invoice_number = new_invoice
    _log_event(
        db, installation=installation, actor=actor, event_type="INVOICE_UPDATED",
        payload={"from": prev, "to": new_invoice},
    )
    db.commit()
    db.refresh(installation)
    logger.info(
        "Installation %s invoice %s → %s by %s",
        installation.reference, prev, new_invoice, actor.username,
    )
    return installation


def _require_invoice_doc_editor(installation: Installation, actor: User) -> None:
    """Same gate as the invoice number: assignee / Admin / Manager, before CLOSED."""
    can_edit = (
        actor.role in ADMIN_MANAGER_ROLES
        or installation.assigned_engineer_id == actor.id
    )
    if not can_edit:
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can change the invoice document",
        )
    if installation.status == InstallationStatus.CLOSED.value:
        raise HTTPException(
            status_code=409,
            detail="The invoice document can't be changed after the installation is closed.",
        )


def set_invoice_document(
    db: Session, installation: Installation, actor: User, meta: dict
) -> Installation:
    """Attach (or replace) the uploaded invoice document.

    `meta` is the storage metadata dict returned by the storage backend
    (filename, content_type, size_bytes, storage_url). Allowed for the
    assignee / Admin / Manager, at any time BEFORE the installation is CLOSED.
    """
    _require_invoice_doc_editor(installation, actor)

    replaced = bool(installation.invoice_document_storage_key)
    installation.invoice_document_filename = meta["filename"]
    installation.invoice_document_content_type = meta["content_type"]
    installation.invoice_document_size_bytes = int(meta["size_bytes"])
    installation.invoice_document_storage_key = meta["storage_url"]
    installation.invoice_document_uploaded_at = datetime.now(timezone.utc)

    _log_event(
        db, installation=installation, actor=actor,
        event_type="INVOICE_DOCUMENT_REPLACED" if replaced else "INVOICE_DOCUMENT_UPLOADED",
        payload={"filename": meta["filename"], "size_bytes": int(meta["size_bytes"])},
    )
    db.commit()
    db.refresh(installation)
    logger.info(
        "Installation %s invoice document %s by %s (%s)",
        installation.reference,
        "replaced" if replaced else "uploaded",
        actor.username,
        meta["filename"],
    )
    return installation


def remove_invoice_document(db: Session, installation: Installation, actor: User) -> Installation:
    """Detach the uploaded invoice document. Assignee / Admin / Manager, before CLOSED."""
    _require_invoice_doc_editor(installation, actor)
    if not installation.invoice_document_storage_key:
        return installation

    prev = installation.invoice_document_filename
    installation.invoice_document_filename = None
    installation.invoice_document_content_type = None
    installation.invoice_document_size_bytes = None
    installation.invoice_document_storage_key = None
    installation.invoice_document_uploaded_at = None

    _log_event(
        db, installation=installation, actor=actor,
        event_type="INVOICE_DOCUMENT_REMOVED",
        payload={"filename": prev},
    )
    db.commit()
    db.refresh(installation)
    logger.info("Installation %s invoice document removed by %s", installation.reference, actor.username)
    return installation


def update_address(
    db: Session, installation: Installation, actor: User, data: dict
) -> Installation:
    """Edit the site address / location.

    Same gate as the invoice number: assignee / Admin / Manager, before CLOSED.
    `data` carries the already-validated address fields (line1..3, city, state,
    pincode, latitude, longitude); blank optional fields arrive as None.
    """
    can_edit = (
        actor.role in ADMIN_MANAGER_ROLES
        or installation.assigned_engineer_id == actor.id
    )
    if not can_edit:
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can edit the address",
        )
    if installation.status == InstallationStatus.CLOSED.value:
        raise HTTPException(
            status_code=409,
            detail="The address can't be changed after the installation is closed.",
        )

    for field in (
        "address_line1", "address_line2", "address_line3",
        "city", "state", "pincode", "latitude", "longitude",
    ):
        setattr(installation, field, data.get(field))

    _log_event(
        db, installation=installation, actor=actor, event_type="ADDRESS_UPDATED",
        payload={
            "city": installation.city,
            "state": installation.state,
            "pincode": installation.pincode,
        },
    )
    db.commit()
    db.refresh(installation)
    logger.info("Installation %s address updated by %s", installation.reference, actor.username)
    return installation


def update_customer(
    db: Session, installation: Installation, actor: User, data: dict
) -> Installation:
    """Edit the customer / contact details.

    Same gate as the invoice number: assignee / Admin / Manager, before CLOSED.
    `data` carries the already-validated fields (business_name, business_category,
    contact_name, phone, email); a blank email arrives as None.
    """
    can_edit = (
        actor.role in ADMIN_MANAGER_ROLES
        or installation.assigned_engineer_id == actor.id
    )
    if not can_edit:
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can edit the customer details",
        )
    if installation.status == InstallationStatus.CLOSED.value:
        raise HTTPException(
            status_code=409,
            detail="The customer details can't be changed after the installation is closed.",
        )

    installation.business_name = data["business_name"].strip()
    installation.business_category = data["business_category"].strip()
    installation.contact_name = data["contact_name"].strip()
    installation.phone = data["phone"]
    email = data.get("email")
    installation.email = (email or "").strip().lower() or None

    _log_event(
        db, installation=installation, actor=actor, event_type="CUSTOMER_UPDATED",
        payload={
            "business_name": installation.business_name,
            "contact_name": installation.contact_name,
        },
    )
    db.commit()
    db.refresh(installation)
    logger.info("Installation %s customer details updated by %s", installation.reference, actor.username)
    return installation


# --------------------------- work attempts ------------------------------- #

def _can_work(installation: Installation, actor: User) -> bool:
    """Assignee, Admin, Manager or Owner may run attempts / notes."""
    return (
        actor.role in ADMIN_MANAGER_ROLES
        or installation.assigned_engineer_id == actor.id
    )


def _open_attempt(db: Session, installation: Installation) -> Optional[InstallationAttempt]:
    """The currently-open attempt (ended_at IS NULL), or None."""
    return (
        db.query(InstallationAttempt)
        .filter(
            InstallationAttempt.installation_id == installation.id,
            InstallationAttempt.ended_at.is_(None),
        )
        .order_by(InstallationAttempt.attempt_number.desc())
        .first()
    )


def start_attempt(db: Session, installation: Installation, actor: User) -> InstallationAttempt:
    """Begin a new on-site work attempt. Assignee / Admin / Manager, ASSIGNED.

    Only one attempt may be open at a time. The new attempt's number is one
    higher than the last.
    """
    if not _can_work(installation, actor):
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can start an attempt",
        )
    _require_status(installation, {InstallationStatus.ASSIGNED.value})
    if _open_attempt(db, installation) is not None:
        raise HTTPException(
            status_code=409,
            detail="An attempt is already in progress. End it before starting another.",
        )

    last = (
        db.query(func.max(InstallationAttempt.attempt_number))
        .filter(InstallationAttempt.installation_id == installation.id)
        .scalar()
    ) or 0
    attempt = InstallationAttempt(
        installation_id=installation.id,
        attempt_number=last + 1,
        started_by_id=actor.id,
    )
    db.add(attempt)
    db.flush()
    _log_event(
        db, installation=installation, actor=actor, event_type="ATTEMPT_STARTED",
        payload={"attempt_number": attempt.attempt_number},
    )
    db.commit()
    db.refresh(attempt)
    logger.info("Installation %s attempt %d started by %s",
                installation.reference, attempt.attempt_number, actor.username)
    return attempt


def end_attempt(db: Session, installation: Installation, actor: User, attempt_id: int) -> InstallationAttempt:
    """Mark an open attempt finished. Assignee / Admin / Manager."""
    if not _can_work(installation, actor):
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can end an attempt",
        )
    attempt = (
        db.query(InstallationAttempt)
        .filter(
            InstallationAttempt.id == attempt_id,
            InstallationAttempt.installation_id == installation.id,
        )
        .one_or_none()
    )
    if attempt is None:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.ended_at is not None:
        raise HTTPException(status_code=409, detail="This attempt has already ended.")

    attempt.ended_at = datetime.now(timezone.utc)
    _log_event(
        db, installation=installation, actor=actor, event_type="ATTEMPT_ENDED",
        payload={"attempt_number": attempt.attempt_number, "note_count": len(attempt.notes)},
    )
    db.commit()
    db.refresh(attempt)
    logger.info("Installation %s attempt %d ended by %s",
                installation.reference, attempt.attempt_number, actor.username)
    return attempt


def close_installation(db: Session, installation: Installation, actor: User) -> tuple[Installation, str]:
    """Engineer (or self-assigned Admin/Manager) marks installation done.

    ASSIGNED → COMPLETED. Requires at least one completed attempt and no attempt
    still open. Creates the InstallationResolution row + signing token, ready for
    customer + engineer signatures.
    """
    from .installation_signing import create_resolution_with_token  # noqa: WPS433

    _require_assignee(installation, actor)
    _require_status(installation, {InstallationStatus.ASSIGNED.value})

    if _open_attempt(db, installation) is not None:
        raise HTTPException(
            status_code=409,
            detail="End the open attempt before finishing the installation.",
        )
    ended_count = (
        db.query(func.count(InstallationAttempt.id))
        .filter(
            InstallationAttempt.installation_id == installation.id,
            InstallationAttempt.ended_at.isnot(None),
        )
        .scalar()
    ) or 0
    if ended_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Log at least one attempt before finishing the installation.",
        )

    prev = installation.status
    installation.status = InstallationStatus.COMPLETED.value
    installation.completed_at = datetime.now(timezone.utc)

    _log_event(
        db, installation=installation, actor=actor, event_type="COMPLETED",
        from_status=prev, to_status=installation.status,
    )

    _, sign_url = create_resolution_with_token(db, installation)

    db.commit()
    db.refresh(installation)
    logger.info("Installation %s completed by %s", installation.reference, actor.username)
    return installation, sign_url
