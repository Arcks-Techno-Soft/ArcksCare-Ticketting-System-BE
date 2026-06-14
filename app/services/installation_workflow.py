"""Installation workflow state machine.

Mirrors ticket_workflow.py but for installations. Keeps the router thin and
the audit log consistent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models.installation import (
    Installation,
    InstallationEvent,
    InstallationNote,
    InstallationStatus,
)
from ..models.user import User, UserRole

logger = logging.getLogger("skposcare.installation_workflow")


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
    if actor.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
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
        actor.role in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value)
        or installation.assigned_engineer_id == actor.id
    )
    if not can_add:
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can add notes",
        )
    _require_status(installation, {InstallationStatus.ASSIGNED.value})

    note = InstallationNote(
        installation_id=installation.id,
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
        actor.role in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value)
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


def close_installation(db: Session, installation: Installation, actor: User) -> tuple[Installation, str]:
    """Engineer (or self-assigned Admin/Manager) marks installation done.

    ASSIGNED → COMPLETED. Creates the InstallationResolution row + signing
    token, ready for customer + engineer signatures.
    """
    from .installation_signing import create_resolution_with_token  # noqa: WPS433

    _require_assignee(installation, actor)
    _require_status(installation, {InstallationStatus.ASSIGNED.value})

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
