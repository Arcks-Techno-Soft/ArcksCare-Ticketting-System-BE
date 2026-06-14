"""Ticket workflow state machine.

Centralises the rules for state transitions so the routers stay thin and
the audit log stays consistent. Every transition:
  - validates the current status
  - updates the ticket
  - writes a TicketEvent row
  - optionally fires a side-effect (e.g. notify engineer)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..models.ticket import (
    Severity,
    Ticket,
    TicketEvent,
    TicketStatus,
    WarrantyStatus,
    WorkNote,
)
from ..models.user import User, UserRole

logger = logging.getLogger("skposcare.workflow")


# --------------------------- helpers ------------------------------------- #

def _log_event(
    db: Session,
    *,
    ticket: Ticket,
    actor: Optional[User],
    event_type: str,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    payload: Optional[dict] = None,
    note: Optional[str] = None,
) -> TicketEvent:
    e = TicketEvent(
        ticket_id=ticket.id,
        actor_user_id=actor.id if actor else None,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        payload=payload,
        note=note,
    )
    db.add(e)
    return e


def _require_status(ticket: Ticket, allowed: set[str]) -> None:
    if ticket.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Action not allowed in status {ticket.status}. "
                f"Allowed: {sorted(allowed)}"
            ),
        )


# --------------------------- transitions --------------------------------- #

def acknowledge(db: Session, ticket: Ticket, actor: User) -> Ticket:
    """OPEN → ACKNOWLEDGED. Manager/Admin only."""
    if actor.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Manager or Admin can acknowledge")
    _require_status(ticket, {TicketStatus.OPEN.value})
    prev = ticket.status
    ticket.status = TicketStatus.ACKNOWLEDGED.value
    ticket.acknowledged_by_id = actor.id
    ticket.acknowledged_at = datetime.now(timezone.utc)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ACKNOWLEDGED",
        from_status=prev, to_status=ticket.status,
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s acknowledged by %s", ticket.reference, actor.username)
    return ticket


def assign_engineer(db: Session, ticket: Ticket, actor: User, engineer_id: int) -> tuple[Ticket, User]:
    """ACKNOWLEDGED → ASSIGNED (or reassign while ASSIGNED/ACCEPTED). Manager/Admin only.

    The assignee can now be ANY active user (Engineer, Manager, or Admin) —
    Admin/Manager may self-assign or be assigned by each other when needed.
    The column is still called `assigned_engineer_id` for historical reasons.

    Returns (ticket, assignee) so the caller can fire a notification email.
    """
    if actor.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Manager or Admin can assign")

    engineer = db.query(User).filter(User.id == engineer_id).one_or_none()
    if engineer is None or not engineer.active:
        raise HTTPException(status_code=400, detail="Invalid assignee")

    # Allow assigning from ACKNOWLEDGED, and reassigning while ASSIGNED/ACCEPTED.
    _require_status(
        ticket,
        {TicketStatus.ACKNOWLEDGED.value, TicketStatus.ASSIGNED.value, TicketStatus.ACCEPTED.value},
    )

    # Warranty must be decided before a ticket can be assigned. It defaults to
    # UNKNOWN on a freshly raised ticket; Admin/Manager set it via PATCH
    # /warranty. Reassignment naturally passes this since warranty is already set.
    if ticket.warranty_status == WarrantyStatus.UNKNOWN.value:
        raise HTTPException(
            status_code=400,
            detail="Set the warranty status (under / out of warranty) before assigning this ticket.",
        )

    prev_status = ticket.status
    is_reassign = ticket.assigned_engineer_id is not None and ticket.assigned_engineer_id != engineer.id

    ticket.assigned_engineer_id = engineer.id
    ticket.assigned_by_id = actor.id
    ticket.assigned_at = datetime.now(timezone.utc)
    ticket.status = TicketStatus.ASSIGNED.value  # collapses ACCEPTED→ASSIGNED on reassign

    _log_event(
        db, ticket=ticket, actor=actor,
        event_type="REASSIGNED" if is_reassign else "ASSIGNED",
        from_status=prev_status, to_status=ticket.status,
        payload={"engineer_id": engineer.id, "engineer_name": engineer.name},
    )
    db.commit()
    db.refresh(ticket)
    logger.info(
        "Ticket %s %s to %s by %s",
        ticket.reference,
        "reassigned" if is_reassign else "assigned",
        engineer.username,
        actor.username,
    )
    return ticket, engineer


# --------------------------- assignee transitions ----------------------- #

def _require_assignee(ticket: Ticket, actor: User) -> None:
    """Only the user the ticket is currently assigned to (regardless of role)
    can run accept / start / resolve / sign-as-engineer. Lets Admin or Manager
    self-assigned tickets run the engineer workflow too."""
    if ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This ticket is assigned to a different user",
        )


# Backwards-compatibility alias for any internal callers we haven't migrated yet.
_require_assigned_engineer = _require_assignee


def accept(db: Session, ticket: Ticket, actor: User) -> Ticket:
    """ASSIGNED → ACCEPTED. Only the assigned engineer."""
    _require_assignee(ticket, actor)
    _require_status(ticket, {TicketStatus.ASSIGNED.value})
    prev = ticket.status
    ticket.status = TicketStatus.ACCEPTED.value
    ticket.accepted_at = datetime.now(timezone.utc)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ACCEPTED",
        from_status=prev, to_status=ticket.status,
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s accepted by %s", ticket.reference, actor.username)
    return ticket


def start_work(db: Session, ticket: Ticket, actor: User) -> Ticket:
    """ACCEPTED → RESOLVING. Only the assigned engineer."""
    _require_assignee(ticket, actor)
    _require_status(ticket, {TicketStatus.ACCEPTED.value})
    prev = ticket.status
    ticket.status = TicketStatus.RESOLVING.value
    ticket.resolving_started_at = datetime.now(timezone.utc)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="RESOLVING_STARTED",
        from_status=prev, to_status=ticket.status,
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s resolving started by %s", ticket.reference, actor.username)
    return ticket


def add_work_note(
    db: Session,
    ticket: Ticket,
    actor: User,
    body: str,
    attachments: Optional[list[dict]] = None,
) -> WorkNote:
    """Add an internal work note. Only the assigned engineer, while RESOLVING.

    `attachments` is an optional list of pre-saved file metadata dicts
    ({filename, content_type, size_bytes, storage_url}) returned by the
    storage backend. Caller is responsible for saving the bytes first.
    """
    from ..models.ticket import WorkNoteAttachment  # local import keeps module load order safe

    _require_assignee(ticket, actor)
    _require_status(ticket, {TicketStatus.RESOLVING.value})
    note = WorkNote(ticket_id=ticket.id, author_id=actor.id, body=body.strip())
    db.add(note)
    db.flush()  # so note.id is available for attachments

    if attachments:
        for meta in attachments:
            db.add(WorkNoteAttachment(
                work_note_id=note.id,
                filename=meta["filename"],
                content_type=meta["content_type"],
                size_bytes=int(meta["size_bytes"]),
                storage_url=meta["storage_url"],
            ))

    _log_event(
        db, ticket=ticket, actor=actor, event_type="NOTE_ADDED",
        payload={
            "note_preview": body.strip()[:120],
            "attachment_count": len(attachments or []),
        },
    )
    db.commit()
    db.refresh(note)
    logger.info(
        "Note added to %s by %s (%d image(s))",
        ticket.reference, actor.username, len(attachments or []),
    )
    return note


def resolve(db: Session, ticket: Ticket, actor: User, summary: str) -> tuple[Ticket, str]:
    """RESOLVING → RESOLVED. Creates the Resolution row + customer signing token.

    Returns (ticket, customer_sign_url) so the router can fire the customer email.
    """
    # Local import to avoid module-load cycle.
    from .signing import create_resolution_with_token  # noqa: WPS433

    _require_assignee(ticket, actor)
    _require_status(ticket, {TicketStatus.RESOLVING.value})

    prev = ticket.status
    ticket.status = TicketStatus.RESOLVED.value
    ticket.resolved_at = datetime.now(timezone.utc)
    ticket.resolution_summary = summary.strip()

    # Compute mins for the audit payload so the timeline shows turnaround.
    mins = None
    if ticket.resolving_started_at is not None:
        start = ticket.resolving_started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        mins = round((ticket.resolved_at - start).total_seconds() / 60.0, 1)

    _log_event(
        db, ticket=ticket, actor=actor, event_type="RESOLVED",
        from_status=prev, to_status=ticket.status,
        payload={"minutes_taken": mins},
    )

    # Create the Resolution row + customer signing token before committing.
    _, customer_sign_url = create_resolution_with_token(db, ticket)

    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s resolved by %s in %s min; customer sign URL: %s",
                ticket.reference, actor.username, mins, customer_sign_url)
    return ticket, customer_sign_url


# --------------------------- owner/manager transitions ------------------ #

def update_severity(db: Session, ticket: Ticket, actor: User, new_severity: str) -> Ticket:
    """Admin/Manager-only. Allowed at any status — triage can happen anytime."""
    if actor.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Manager or Admin can set severity")
    if new_severity not in {s.value for s in Severity}:
        raise HTTPException(status_code=400, detail=f"Invalid severity: {new_severity}")

    prev = ticket.severity
    if prev == new_severity:
        return ticket
    ticket.severity = new_severity
    _log_event(
        db, ticket=ticket, actor=actor, event_type="SEVERITY_UPDATED",
        payload={"from": prev, "to": new_severity},
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s severity %s → %s by %s", ticket.reference, prev, new_severity, actor.username)
    return ticket


def update_warranty(db: Session, ticket: Ticket, actor: User, new_status: str) -> Ticket:
    """Admin or Manager can update warranty at any status."""
    if actor.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Manager or Admin can update warranty")
    if new_status not in {w.value for w in WarrantyStatus}:
        raise HTTPException(status_code=400, detail=f"Invalid warranty status: {new_status}")

    prev = ticket.warranty_status
    if prev == new_status:
        return ticket  # no-op
    ticket.warranty_status = new_status
    _log_event(
        db, ticket=ticket, actor=actor, event_type="WARRANTY_UPDATED",
        payload={"from": prev, "to": new_status},
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s warranty %s → %s by %s", ticket.reference, prev, new_status, actor.username)
    return ticket
