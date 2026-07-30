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
from sqlalchemy import func, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..database import MIGRATION_SCHEMA, qualify
from ..models.ticket import (
    PaymentStatus,
    ServiceType,
    Severity,
    Ticket,
    TicketAttempt,
    TicketEvent,
    TicketStatus,
    WarrantyStatus,
    WorkNote,
)
from ..models.user import User, UserRole, ADMIN_MANAGER_ROLES, ADMIN_ROLES, SUPER_ADMIN_ROLES
from .spares import oow_min_service_fee_inr

logger = logging.getLogger("skposcare.workflow")


def ensure_worknote_attempt_column(engine: Engine) -> None:
    """Add work_notes.ticket_attempt_id if it's missing.

    The ticket_attempts table itself is created by `create_all`; this
    idempotent ALTER backfills the FK column on the existing work_notes table.
    Plain INTEGER (no FK constraint) keeps it portable across SQLite/Postgres.
    """
    insp = inspect(engine)
    if "work_notes" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    existing = {c["name"] for c in insp.get_columns("work_notes", schema=MIGRATION_SCHEMA)}
    if "ticket_attempt_id" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE {qualify('work_notes')} ADD COLUMN ticket_attempt_id INTEGER")
        )


def ensure_payment_verification_columns(engine: Engine) -> None:
    """Add tickets.payment_verified_at / payment_verified_by_id if missing.

    Backs the Admin payment-verification step. Both are nullable with no
    backfill: an existing COLLECTED row on a CLOSED ticket is treated as
    verified by Ticket.payment_verified, so historical data needs no rewrite.
    Plain types (no FK constraint) keep this portable across SQLite/Postgres.
    """
    insp = inspect(engine)
    if "tickets" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    existing = {c["name"] for c in insp.get_columns("tickets", schema=MIGRATION_SCHEMA)}
    wanted = {
        "payment_verified_at": "TIMESTAMP WITH TIME ZONE"
        if engine.dialect.name != "sqlite"
        else "DATETIME",
        "payment_verified_by_id": "INTEGER",
    }
    missing = {name: ddl for name, ddl in wanted.items() if name not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(
                text(f"ALTER TABLE {qualify('tickets')} ADD COLUMN {name} {ddl}")
            )


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


def _require_not_held(ticket: Ticket) -> None:
    """Block a workflow action while the ticket is parked.

    Hold is an overlay on `status`, so every transition has to check it
    separately from _require_status. Work notes are deliberately exempt — the
    reason a ticket is waiting usually needs recording while it waits.
    """
    if ticket.held_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This ticket is on hold. A Manager or Admin must resume it "
                "before any further action."
            ),
        )


# --------------------------- transitions --------------------------------- #

def acknowledge(db: Session, ticket: Ticket, actor: User) -> Ticket:
    """OPEN → ACKNOWLEDGED. Manager/Admin only."""
    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only Manager or Admin can acknowledge")
    _require_not_held(ticket)
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
    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only Manager or Admin can assign")
    _require_not_held(ticket)

    engineer = db.query(User).filter(User.id == engineer_id).one_or_none()
    if engineer is None or not engineer.active:
        raise HTTPException(status_code=400, detail="Invalid assignee")

    # Allow assigning from ACKNOWLEDGED, and reassigning while ASSIGNED/ACCEPTED
    # or mid-resolution (RESOLVING) so a manager can hand a ticket to another
    # engineer while work is in progress.
    _require_status(
        ticket,
        {
            TicketStatus.ACKNOWLEDGED.value,
            TicketStatus.ASSIGNED.value,
            TicketStatus.ACCEPTED.value,
            TicketStatus.RESOLVING.value,
        },
    )

    # Warranty must be decided before a ticket can be assigned. It defaults to
    # UNKNOWN on a freshly raised ticket; Admin/Manager set it via PATCH
    # /warranty. Reassignment naturally passes this since warranty is already set.
    if ticket.warranty_status == WarrantyStatus.UNKNOWN.value:
        raise HTTPException(
            status_code=400,
            detail="Set the warranty status (under warranty / out of warranty / AMC) before assigning this ticket.",
        )

    prev_status = ticket.status
    is_reassign = ticket.assigned_engineer_id is not None and ticket.assigned_engineer_id != engineer.id

    # A ticket can't be reassigned while the outgoing engineer still has an
    # attempt in progress — that work would be silently orphaned. The manager
    # is told to have them end the attempt first (only the assignee can end
    # their own attempt, via end_attempt). Open attempts only exist while
    # RESOLVING, so this is a no-op for earlier statuses.
    if is_reassign:
        open_attempt = _open_attempt(db, ticket)
        if open_attempt is not None:
            current_name = (
                ticket.assigned_engineer.name
                if ticket.assigned_engineer is not None
                else "The current engineer"
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{current_name} has an open work attempt "
                    f"(#{open_attempt.attempt_number}) on this ticket. Ask them to "
                    "end the current attempt before reassigning it."
                ),
            )

    ticket.assigned_engineer_id = engineer.id
    ticket.assigned_by_id = actor.id
    ticket.assigned_at = datetime.now(timezone.utc)
    ticket.status = TicketStatus.ASSIGNED.value  # collapses ACCEPTED/RESOLVING→ASSIGNED on reassign

    # The new engineer starts their own lifecycle from scratch (accept →
    # start-work → new attempt), so drop the previous engineer's accept/start
    # timestamps rather than leave them showing on the reassigned ticket.
    if is_reassign:
        ticket.accepted_at = None
        ticket.resolving_started_at = None

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


def add_engineer(
    db: Session, ticket: Ticket, actor: User, engineer_id: int
) -> tuple["TicketEngineer", User]:
    """Add an EXTRA engineer to the ticket for the same site visit. Manager/Admin only.

    The added engineer can view the ticket and is notified, but only the primary
    `assigned_engineer` drives the workflow (accept / notes / resolve). Returns
    (link, engineer) so the caller can fire notifications.
    """
    from ..models.ticket_engineer import TicketEngineer  # local import: load order

    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only Manager or Admin can add an engineer")
    _require_not_held(ticket)

    # A primary engineer must already be assigned — additional engineers are
    # helpers on top of a real assignment, not a way to assign the first one.
    if ticket.assigned_engineer_id is None:
        raise HTTPException(
            status_code=409,
            detail="Assign a primary engineer before adding additional engineers.",
        )

    engineer = db.query(User).filter(User.id == engineer_id).one_or_none()
    if engineer is None or not engineer.active:
        raise HTTPException(status_code=400, detail="Invalid engineer")

    # Can't add the primary assignee as an additional engineer — they're already on it.
    if engineer.id == ticket.assigned_engineer_id:
        raise HTTPException(
            status_code=409,
            detail="That engineer is already the primary assignee on this ticket.",
        )

    existing = (
        db.query(TicketEngineer)
        .filter(
            TicketEngineer.ticket_id == ticket.id,
            TicketEngineer.engineer_id == engineer.id,
        )
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="That engineer is already added to this ticket.",
        )

    link = TicketEngineer(
        ticket_id=ticket.id, engineer_id=engineer.id, added_by_id=actor.id
    )
    db.add(link)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ENGINEER_ADDED",
        payload={"engineer_id": engineer.id, "engineer_name": engineer.name},
    )
    db.commit()
    db.refresh(link)
    logger.info(
        "Additional engineer %s added to %s by %s",
        engineer.username, ticket.reference, actor.username,
    )
    return link, engineer


def remove_engineer(db: Session, ticket: Ticket, actor: User, engineer_id: int) -> Ticket:
    """Remove an additional engineer from the ticket. Manager/Admin only.

    Never touches the primary `assigned_engineer` — use /assign to change that.
    """
    from ..models.ticket_engineer import TicketEngineer  # local import: load order

    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only Manager or Admin can remove an engineer")
    _require_not_held(ticket)

    link = (
        db.query(TicketEngineer)
        .filter(
            TicketEngineer.ticket_id == ticket.id,
            TicketEngineer.engineer_id == engineer_id,
        )
        .one_or_none()
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Engineer is not on this ticket")

    engineer_name = link.engineer.name if link.engineer else None
    db.delete(link)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ENGINEER_REMOVED",
        payload={"engineer_id": engineer_id, "engineer_name": engineer_name},
    )
    db.commit()
    db.refresh(ticket)
    logger.info(
        "Additional engineer %s removed from %s by %s",
        engineer_id, ticket.reference, actor.username,
    )
    return ticket


# --------------------------- admin overrides ---------------------------- #

def _require_super_admin(actor: User, action: str) -> None:
    """Reserved super-admin-only actions (force-close, delete). Plain Admins are
    intentionally excluded — only a Super Admin (or legacy Owner) may do these."""
    if actor.role not in SUPER_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail=f"Only a Super Admin can {action}")


# Back-compat alias in case other modules imported the old name.
_require_admin_or_owner = _require_super_admin


def payment_required(ticket: Ticket) -> bool:
    """Thin wrapper over Ticket.payment_required (the single source of truth)."""
    return ticket.payment_required


def payment_blocks_close(ticket: Ticket) -> bool:
    """Thin wrapper over Ticket.payment_blocks_close — payment required but not
    yet collected in full AND verified by an Admin, so the ticket can't close."""
    return ticket.payment_blocks_close


def signoff_complete(ticket: Ticket) -> bool:
    """Whether the work itself is signed off, ignoring payment entirely.

    Remote support needs no signatures or PDF. Third-party support needs the
    engineer signature + PDF (no customer signature). Site visits need both
    signatures + the PDF.
    """
    if ticket.service_type == ServiceType.REMOTE_SUPPORT.value:
        return True
    res = ticket.resolution
    if res is None or res.pdf_generated_at is None or res.engineer_signed_at is None:
        return False
    if ticket.service_type == ServiceType.THIRD_PARTY_SUPPORT.value:
        return True
    return res.customer_signed_at is not None


def compute_close_pending(ticket: Ticket) -> list[str]:
    """Human-readable list of everything still incomplete on a ticket.

    Shown in the force-close confirmation so the Admin sees exactly what they're
    overriding before closing a ticket that didn't go through the normal flow.
    Returns [] for an already-closed ticket.
    """
    if ticket.status == TicketStatus.CLOSED.value:
        return []

    pending: list[str] = []
    if ticket.acknowledged_at is None:
        pending.append("Ticket not acknowledged")
    if ticket.warranty_status == WarrantyStatus.UNKNOWN.value:
        pending.append("Warranty status not set")
    if ticket.assigned_engineer_id is None:
        pending.append("No engineer assigned")
    elif ticket.accepted_at is None:
        pending.append("Engineer hasn't accepted the assignment")
    if ticket.resolving_started_at is None:
        pending.append("Work not started")
    if ticket.resolved_at is None:
        pending.append("Not marked resolved")

    # Site visits normally close only after signatures + PDF.
    if ticket.service_type == ServiceType.SITE_VISIT.value:
        res = ticket.resolution
        if res is None:
            pending.append("Resolution not completed (no signatures / PDF)")
        else:
            if res.customer_signed_at is None:
                pending.append("Customer signature not collected")
            if res.engineer_signed_at is None:
                pending.append("Engineer signature not collected")
            if res.pdf_generated_at is None:
                pending.append("Resolution PDF not generated")
    # Third-party support closes on the engineer signature alone (no customer
    # signature), but the third-party device name + issue info must be filled in.
    elif ticket.service_type == ServiceType.THIRD_PARTY_SUPPORT.value:
        if not (ticket.third_party_device_name or "").strip():
            pending.append("Third-party device name not entered")
        if not (ticket.third_party_issue_info or "").strip():
            pending.append("Third-party issue info not entered")
        res = ticket.resolution
        if res is None:
            pending.append("Resolution not completed (engineer signature / PDF)")
        else:
            if res.engineer_signed_at is None:
                pending.append("Engineer signature not collected")
            if res.pdf_generated_at is None:
                pending.append("Resolution PDF not generated")

    for se in ticket.sub_engineers:
        if se.fee_inr is None:
            pending.append(f"Fee not recorded for sub-engineer {se.name}")
    if any(sh.delivered_at is None for sh in ticket.shipments):
        pending.append("A parts shipment is not yet marked delivered")
    if payment_blocks_close(ticket):
        if ticket.amount_pending_inr > 0:
            pending.append(
                f"Payment not collected in full (₹{ticket.amount_pending_inr} still pending)"
            )
        else:
            pending.append("Payment collected but not yet verified by an Admin")
    return pending


def force_close(db: Session, ticket: Ticket, actor: User, reason: str) -> Ticket:
    """Admin/Owner override: move a ticket to CLOSED from ANY status, skipping
    signatures/PDF. A reason is required and recorded for audit."""
    _require_super_admin(actor, "close a ticket")
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required to close the ticket.")
    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(status_code=409, detail="Ticket is already closed.")

    prev = ticket.status
    ticket.status = TicketStatus.CLOSED.value
    _log_event(
        db, ticket=ticket, actor=actor, event_type="FORCE_CLOSED",
        from_status=prev, to_status=ticket.status,
        payload={"reason": reason, "pending": compute_close_pending(ticket)},
        note=reason,
    )
    db.commit()
    db.refresh(ticket)
    if ticket.status == TicketStatus.CLOSED.value:
        from .ticket_notify import notify_sales_rep_closed
        notify_sales_rep_closed(ticket.id)
    logger.info("Ticket %s force-closed from %s by %s: %s",
                ticket.reference, prev, actor.username, reason)
    return ticket


def collect_payment(db: Session, ticket: Ticket, actor: User, amount_inr: int) -> Ticket:
    """Record payment collected on a ticket. This NEVER closes the ticket.
    Applies to out-of-warranty tickets and to covered tickets that have charges.
    Allowed for Admin/Owner/Manager or the assigned engineer.

    Paying in full moves the ticket to AWAITING_VERIFICATION and leaves it
    RESOLVED. An Admin/Super Admin then confirms the money actually arrived via
    verify_payment(), and that is what closes the ticket. Partial payments stay
    PENDING with a running balance.
    """
    is_staff = actor.role in ADMIN_MANAGER_ROLES
    if not (is_staff or ticket.assigned_engineer_id == actor.id):
        raise HTTPException(
            status_code=403,
            detail="Only an admin, manager, or the assigned engineer can record payment.",
        )
    if ticket.payment_status is None:
        raise HTTPException(
            status_code=409,
            detail="Payment tracking isn't enabled for this ticket.",
        )
    if not payment_required(ticket):
        raise HTTPException(
            status_code=409,
            detail="No payment is due on this ticket.",
        )
    if ticket.payment_status in (
        PaymentStatus.COLLECTED.value,
        PaymentStatus.AWAITING_VERIFICATION.value,
        PaymentStatus.VERIFIED.value,
    ):
        raise HTTPException(status_code=409, detail="Payment is already recorded in full.")

    # Each collection must be positive — ₹0 can't be collected. This covers the
    # out-of-warranty "must be > 0" rule and applies to partial payments too.
    if amount_inr <= 0:
        raise HTTPException(
            status_code=400,
            detail="Enter an amount greater than zero — ₹0 can't be collected.",
        )
    # Never accept more than what's still owed.
    pending = ticket.amount_pending_inr
    if amount_inr > pending:
        raise HTTPException(
            status_code=400,
            detail=f"Amount exceeds the ₹{pending} still pending — collect ₹{pending} or less.",
        )

    # Accumulate across partial payments. Mark COLLECTED only once the full
    # balance is cleared; otherwise the ticket stays PENDING with a balance and
    # does NOT close.
    ticket.payment_amount_inr = ticket.amount_collected_inr + int(amount_inr)
    ticket.payment_collected_at = datetime.now(timezone.utc)
    ticket.payment_collected_by_id = actor.id
    fully_paid = ticket.amount_pending_inr == 0
    if fully_paid:
        # Collected in full, but NOT closed — an Admin must verify receipt first.
        ticket.payment_status = PaymentStatus.AWAITING_VERIFICATION.value
    _log_event(
        db, ticket=ticket, actor=actor, event_type="PAYMENT_COLLECTED",
        payload={
            "amount_inr": int(amount_inr),
            "collected_total_inr": ticket.amount_collected_inr,
            "due_inr": ticket.amount_due_inr,
            "pending_inr": ticket.amount_pending_inr,
            "partial": not fully_paid,
        },
    )

    # Deliberately NO auto-close here. Paying in full only hands the ticket to
    # an Admin for verification; verify_payment() performs the actual close.
    if fully_paid:
        _log_event(
            db, ticket=ticket, actor=actor, event_type="PAYMENT_AWAITING_VERIFICATION",
            payload={"collected_total_inr": ticket.amount_collected_inr},
            note="Collected in full — awaiting Admin verification before close.",
        )
    db.commit()
    db.refresh(ticket)
    logger.info(
        "Payment ₹%s recorded for %s by %s (collected %s/%s, status=%s, payment=%s)",
        amount_inr, ticket.reference, actor.username,
        ticket.amount_collected_inr, ticket.amount_due_inr,
        ticket.status, ticket.payment_status,
    )
    return ticket


def verify_payment(
    db: Session, ticket: Ticket, actor: User, note: Optional[str] = None
) -> Ticket:
    """Admin/Super Admin confirms the collected money actually arrived, then
    closes the ticket if the work is otherwise signed off.

    This is the only path that closes a ticket which owed money. Restricted to
    ADMIN_ROLES (Admin / Super Admin / Owner) — deliberately NOT Manager and not
    the engineer who collected, so the person banking the cash can't also clear
    it. If sign-off is still outstanding the payment is marked VERIFIED and the
    ticket stays RESOLVED; it then closes when the last signature lands.
    """
    if actor.role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only an Admin or Super Admin can verify a payment.",
        )
    if ticket.payment_status is None or not payment_required(ticket):
        raise HTTPException(
            status_code=409,
            detail="No payment is due on this ticket, so there's nothing to verify.",
        )
    if ticket.payment_verified:
        raise HTTPException(status_code=409, detail="Payment is already verified.")
    if ticket.amount_pending_inr > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"₹{ticket.amount_pending_inr} is still pending — collect the full "
                "amount before verifying."
            ),
        )

    note = (note or "").strip() or None
    ticket.payment_status = PaymentStatus.VERIFIED.value
    ticket.payment_verified_at = datetime.now(timezone.utc)
    ticket.payment_verified_by_id = actor.id
    _log_event(
        db, ticket=ticket, actor=actor, event_type="PAYMENT_VERIFIED",
        payload={
            "verified_amount_inr": ticket.amount_collected_inr,
            "collected_by": (
                ticket.payment_collected_by.username
                if ticket.payment_collected_by is not None
                else None
            ),
        },
        note=note,
    )

    # Verification is the last gate — close now if the work is signed off.
    if ticket.status == TicketStatus.RESOLVED.value and signoff_complete(ticket):
        prev = ticket.status
        ticket.status = TicketStatus.CLOSED.value
        _log_event(
            db, ticket=ticket, actor=actor, event_type="CLOSED",
            from_status=prev, to_status=ticket.status,
            note="Closed on Admin payment verification.",
        )
    db.commit()
    db.refresh(ticket)
    if ticket.status == TicketStatus.CLOSED.value:
        from .ticket_notify import notify_sales_rep_closed
        notify_sales_rep_closed(ticket.id)
    logger.info(
        "Payment ₹%s verified for %s by %s (status=%s)",
        ticket.amount_collected_inr, ticket.reference, actor.username, ticket.status,
    )
    return ticket


def soft_delete_ticket(db: Session, ticket: Ticket, actor: User, reason: str) -> Ticket:
    """Admin/Owner override: soft-delete a ticket in ANY status. The row is
    retained (with deleted_at / deleted_by) but hidden everywhere."""
    _require_super_admin(actor, "delete a ticket")
    if ticket.deleted_at is not None:
        raise HTTPException(status_code=409, detail="Ticket is already deleted.")

    ticket.deleted_at = datetime.now(timezone.utc)
    ticket.deleted_by_id = actor.id
    _log_event(
        db, ticket=ticket, actor=actor, event_type="DELETED",
        payload={"reason": (reason or "").strip() or None},
        note=(reason or "").strip() or None,
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s soft-deleted by %s", ticket.reference, actor.username)
    return ticket


# --------------------------- hold / resume ------------------------------- #

# A ticket can only be parked while field work is still outstanding. RESOLVED
# is excluded on purpose: the visit is done and only the signature + PDF
# remain, so there is nothing left to pause — and it already doesn't count
# toward the engineer's open jobs.
HOLDABLE_TICKET_STATUSES = {
    TicketStatus.OPEN.value,
    TicketStatus.ACKNOWLEDGED.value,
    TicketStatus.ASSIGNED.value,
    TicketStatus.ACCEPTED.value,
    TicketStatus.RESOLVING.value,
}


def hold(db: Session, ticket: Ticket, actor: User, reason: str) -> Ticket:
    """Park a ticket indefinitely. Manager/Admin/Owner only.

    `status` is left untouched — hold is an overlay — so resume() puts the
    ticket back exactly where it stopped. The assignee is kept, but the ticket
    drops out of their open-job count and stops the SLA reminders and breach
    clock for as long as it's held. Repeatable: hold → resume → hold again,
    with every cycle recorded in the event log.
    """
    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(
            status_code=403, detail="Only Manager or Admin can put a ticket on hold"
        )
    if ticket.held_at is not None:
        raise HTTPException(status_code=409, detail="This ticket is already on hold.")
    _require_status(ticket, HOLDABLE_TICKET_STATUSES)

    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=400, detail="A reason is required to put a ticket on hold."
        )

    # Same rule as reassignment: an attempt in flight would be silently
    # orphaned by the freeze, so the engineer has to close it out first.
    open_attempt = _open_attempt(db, ticket)
    if open_attempt is not None:
        engineer_name = (
            ticket.assigned_engineer.name
            if ticket.assigned_engineer is not None
            else "The assigned engineer"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"{engineer_name} has an open work attempt "
                f"(#{open_attempt.attempt_number}) on this ticket. Ask them to end "
                "it before putting the ticket on hold."
            ),
        )

    ticket.held_at = datetime.now(timezone.utc)
    ticket.held_by_id = actor.id
    ticket.hold_reason = reason
    _log_event(
        db, ticket=ticket, actor=actor, event_type="HELD",
        payload={"reason": reason},
        note=reason,
    )
    db.commit()
    db.refresh(ticket)
    logger.info(
        "Ticket %s put on hold by %s (%s)", ticket.reference, actor.username, reason
    )
    return ticket


def resume(db: Session, ticket: Ticket, actor: User, note: Optional[str] = None) -> Ticket:
    """Lift a hold and hand the ticket back to whoever it was assigned to.

    Nothing is restored because nothing was changed — clearing the hold
    columns is the whole operation.
    """
    if actor.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(
            status_code=403, detail="Only Manager or Admin can resume a ticket"
        )
    if ticket.held_at is None:
        raise HTTPException(status_code=409, detail="This ticket is not on hold.")

    note = (note or "").strip() or None
    held_since = ticket.held_at
    ticket.held_at = None
    ticket.held_by_id = None
    ticket.hold_reason = None
    _log_event(
        db, ticket=ticket, actor=actor, event_type="RESUMED",
        payload={"held_since": held_since.isoformat() if held_since else None},
        note=note,
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s resumed by %s", ticket.reference, actor.username)
    return ticket


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
    _require_not_held(ticket)
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
    _require_not_held(ticket)
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
    open_attempt = _open_attempt(db, ticket)
    if open_attempt is None:
        raise HTTPException(
            status_code=409,
            detail="Start an attempt before adding notes.",
        )
    note = WorkNote(
        ticket_id=ticket.id,
        ticket_attempt_id=open_attempt.id,
        author_id=actor.id,
        body=body.strip(),
    )
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


# --------------------------- work attempts ------------------------------- #

def _open_attempt(db: Session, ticket: Ticket) -> Optional[TicketAttempt]:
    """The currently-open attempt (ended_at IS NULL), or None."""
    return (
        db.query(TicketAttempt)
        .filter(
            TicketAttempt.ticket_id == ticket.id,
            TicketAttempt.ended_at.is_(None),
        )
        .order_by(TicketAttempt.attempt_number.desc())
        .first()
    )


def start_attempt(db: Session, ticket: Ticket, actor: User) -> TicketAttempt:
    """Begin a new work attempt. Only the assignee.

    Allowed from ACCEPTED or RESOLVING; starting the first attempt from ACCEPTED
    auto-transitions the ticket into RESOLVING so the engineer needs only one
    tap. Only one attempt may be open at a time.
    """
    _require_assignee(ticket, actor)
    _require_not_held(ticket)
    _require_status(ticket, {TicketStatus.ACCEPTED.value, TicketStatus.RESOLVING.value})

    # First attempt auto-starts work (ACCEPTED → RESOLVING).
    if ticket.status == TicketStatus.ACCEPTED.value:
        prev = ticket.status
        ticket.status = TicketStatus.RESOLVING.value
        ticket.resolving_started_at = datetime.now(timezone.utc)
        _log_event(
            db, ticket=ticket, actor=actor, event_type="RESOLVING_STARTED",
            from_status=prev, to_status=ticket.status,
        )

    if _open_attempt(db, ticket) is not None:
        raise HTTPException(
            status_code=409,
            detail="An attempt is already in progress. End it before starting another.",
        )

    last = (
        db.query(func.max(TicketAttempt.attempt_number))
        .filter(TicketAttempt.ticket_id == ticket.id)
        .scalar()
    ) or 0
    attempt = TicketAttempt(
        ticket_id=ticket.id,
        attempt_number=last + 1,
        started_by_id=actor.id,
    )
    db.add(attempt)
    db.flush()
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ATTEMPT_STARTED",
        payload={"attempt_number": attempt.attempt_number},
    )
    db.commit()
    db.refresh(attempt)
    logger.info("Ticket %s attempt %d started by %s",
                ticket.reference, attempt.attempt_number, actor.username)
    return attempt


def end_attempt(db: Session, ticket: Ticket, actor: User, attempt_id: int) -> TicketAttempt:
    """Mark an open attempt finished. Only the assignee."""
    _require_assignee(ticket, actor)
    attempt = (
        db.query(TicketAttempt)
        .filter(TicketAttempt.id == attempt_id, TicketAttempt.ticket_id == ticket.id)
        .one_or_none()
    )
    if attempt is None:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.ended_at is not None:
        raise HTTPException(status_code=409, detail="This attempt has already ended.")

    attempt.ended_at = datetime.now(timezone.utc)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ATTEMPT_ENDED",
        payload={"attempt_number": attempt.attempt_number, "note_count": len(attempt.notes)},
    )
    db.commit()
    db.refresh(attempt)
    logger.info("Ticket %s attempt %d ended by %s",
                ticket.reference, attempt.attempt_number, actor.username)
    return attempt


def resolve(db: Session, ticket: Ticket, actor: User, summary: str) -> tuple[Ticket, Optional[str]]:
    """RESOLVING → RESOLVED for site visits; RESOLVING → CLOSED for remote support.

    Requires at least one completed attempt and no attempt still open. Site
    visits create a Resolution row + customer signing token and wait for
    signatures. Remote-support tickets need no signatures or PDF, so they close
    in this single step.

    Returns (ticket, customer_sign_url). The URL is None for remote support
    (and the router skips the customer "please sign" email).
    """
    # Local import to avoid module-load cycle.
    from .signing import create_resolution_with_token  # noqa: WPS433

    _require_assignee(ticket, actor)
    _require_not_held(ticket)
    _require_status(ticket, {TicketStatus.RESOLVING.value})

    if _open_attempt(db, ticket) is not None:
        raise HTTPException(
            status_code=409,
            detail="End the open attempt before resolving the ticket.",
        )
    ended_count = (
        db.query(func.count(TicketAttempt.id))
        .filter(
            TicketAttempt.ticket_id == ticket.id,
            TicketAttempt.ended_at.isnot(None),
        )
        .scalar()
    ) or 0
    if ended_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Log at least one attempt before resolving the ticket.",
        )

    is_remote = ticket.service_type == ServiceType.REMOTE_SUPPORT.value

    prev = ticket.status
    ticket.resolved_at = datetime.now(timezone.utc)
    ticket.resolution_summary = summary.strip()

    # Compute mins for the audit payload so the timeline shows turnaround.
    mins = None
    if ticket.resolving_started_at is not None:
        start = ticket.resolving_started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        mins = round((ticket.resolved_at - start).total_seconds() / 60.0, 1)

    if is_remote:
        # No signatures / PDF for remote support. It still can't close while
        # money is owed OR while collected money is unverified — gate it the same
        # as a site visit. Hold the ticket in RESOLVED until an Admin verifies
        # payment; with nothing due at all, close in one step.
        if payment_blocks_close(ticket):
            ticket.status = TicketStatus.RESOLVED.value
            awaiting_verification = ticket.amount_pending_inr == 0
            _log_event(
                db, ticket=ticket, actor=actor, event_type="RESOLVED",
                from_status=prev, to_status=ticket.status,
                payload={"minutes_taken": mins, "service_type": ticket.service_type},
                note=(
                    "Remote support — resolved; awaiting Admin payment verification."
                    if awaiting_verification
                    else "Remote support — resolved; awaiting payment before close."
                ),
            )
            db.commit()
            db.refresh(ticket)
            logger.info("Ticket %s resolved (remote) by %s — held pending payment",
                        ticket.reference, actor.username)
            return ticket, None

        ticket.status = TicketStatus.CLOSED.value
        _log_event(
            db, ticket=ticket, actor=actor, event_type="RESOLVED",
            from_status=prev, to_status=ticket.status,
            payload={"minutes_taken": mins, "service_type": ticket.service_type},
            note="Remote support — resolved and closed without signatures.",
        )
        db.commit()
        db.refresh(ticket)
        if ticket.status == TicketStatus.CLOSED.value:
            from .ticket_notify import notify_sales_rep_closed
            notify_sales_rep_closed(ticket.id)
        logger.info("Ticket %s resolved + closed (remote) by %s in %s min",
                    ticket.reference, actor.username, mins)
        return ticket, None

    ticket.status = TicketStatus.RESOLVED.value
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
    if actor.role not in ADMIN_MANAGER_ROLES:
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


def _maybe_seed_oow_min_fee(ticket: Ticket) -> None:
    """Ensure an out-of-warranty ticket's service fee is at least the
    per-service-type minimum (Remote ₹600, Site visit ₹800 — see
    spares.OOW_MIN_FEE_INR). Only raises a below-minimum fee up to the floor;
    never lowers an already-higher (engineer-entered) fee. No-op for covered or
    third-party tickets, whose minimum is ₹0."""
    min_fee = oow_min_service_fee_inr(ticket)
    if min_fee and int(ticket.service_fee_inr or 0) < min_fee:
        ticket.service_fee_inr = min_fee


def update_warranty(db: Session, ticket: Ticket, actor: User, new_status: str) -> Ticket:
    """Admin or Manager can update warranty at any status."""
    if actor.role not in ADMIN_MANAGER_ROLES:
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
    # Moving out-of-warranty → covered (under-warranty or AMC) clears the
    # service charge: a now-covered ticket defaults to ₹0 (the OOW minimum no
    # longer applies). Staff can still enter a charge afterwards if needed.
    if prev == WarrantyStatus.OUT_OF_WARRANTY.value and new_status in (
        WarrantyStatus.UNDER_WARRANTY.value,
        WarrantyStatus.AMC.value,
    ):
        ticket.service_fee_inr = 0
    # Seed the per-service-type out-of-warranty minimum if now applicable.
    _maybe_seed_oow_min_fee(ticket)
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s warranty %s → %s by %s", ticket.reference, prev, new_status, actor.username)
    return ticket


def set_service_type(db: Session, ticket: Ticket, actor: User, new_type: str) -> Ticket:
    """Admin/Manager or the assigned engineer can switch the service mode.

    Locked once the ticket is RESOLVED or CLOSED — by then the mode has already
    decided whether signatures/PDF were required, so changing it would be
    inconsistent with the finalised record.
    """
    is_staff = actor.role in ADMIN_MANAGER_ROLES
    if not is_staff and ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=403,
            detail="Only Admin/Manager or the assigned engineer can change the service type",
        )
    if new_type not in {s.value for s in ServiceType}:
        raise HTTPException(status_code=400, detail=f"Invalid service type: {new_type}")
    if ticket.status in (TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value):
        raise HTTPException(
            status_code=400,
            detail="Service type can't be changed once the ticket is resolved or closed.",
        )

    prev = ticket.service_type
    if prev == new_type:
        return ticket  # no-op
    ticket.service_type = new_type
    _log_event(
        db, ticket=ticket, actor=actor, event_type="SERVICE_TYPE_UPDATED",
        payload={"from": prev, "to": new_type},
    )
    # Third-party support defaults its service charge to ₹0 (users edit it
    # afterwards); reset any fee carried over from the previous mode.
    if new_type == ServiceType.THIRD_PARTY_SUPPORT.value:
        ticket.service_fee_inr = 0
    # Switching to remote on an out-of-warranty ticket seeds the default fee.
    _maybe_seed_oow_min_fee(ticket)
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s service type %s → %s by %s", ticket.reference, prev, new_type, actor.username)
    return ticket


def set_third_party_info(
    db: Session,
    ticket: Ticket,
    actor: User,
    device_name: Optional[str],
    issue_info: Optional[str],
    ticket_ref: Optional[str],
) -> Ticket:
    """Set the third-party support details (device name / issue info / ticket
    reference). Admin/Manager or the assigned engineer, editable until the
    ticket is CLOSED. Empty strings are stored as NULL.

    Device name + issue info are what let a THIRD_PARTY_SUPPORT ticket close
    (enforced at sign-off / compute_close_pending); this endpoint just records
    them and doesn't itself require them so the engineer can save progressively.
    """
    is_staff = actor.role in ADMIN_MANAGER_ROLES
    if not is_staff and ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=403,
            detail="Only Admin/Manager or the assigned engineer can edit third-party details",
        )
    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(
            status_code=400,
            detail="Third-party details can't be changed once the ticket is closed.",
        )

    def _clean(v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None

    ticket.third_party_device_name = _clean(device_name)
    ticket.third_party_issue_info = _clean(issue_info)
    ticket.third_party_ticket_ref = _clean(ticket_ref)
    _log_event(
        db, ticket=ticket, actor=actor, event_type="THIRD_PARTY_INFO_UPDATED",
        payload={
            "device_name": ticket.third_party_device_name,
            "ticket_ref": ticket.third_party_ticket_ref,
        },
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s third-party details updated by %s", ticket.reference, actor.username)
    return ticket


def _require_details_editor(ticket: Ticket, actor: User, what: str) -> None:
    """Gate for editing customer/address details: Admin/Manager or the assigned
    engineer, and only before the ticket is CLOSED (a closed ticket is signed
    off and baked into the resolution PDF)."""
    is_staff = actor.role in ADMIN_MANAGER_ROLES
    if not is_staff and ticket.assigned_engineer_id != actor.id:
        raise HTTPException(
            status_code=403,
            detail=f"Only Admin/Manager or the assigned engineer can edit the {what}",
        )
    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(
            status_code=409,
            detail=f"The {what} can't be changed after the ticket is closed.",
        )


def update_ticket_customer(db: Session, ticket: Ticket, actor: User, data: dict) -> Ticket:
    """Correct the customer / contact details. `data` carries already-validated
    fields (business_name, contact_name, phone, email, business_type,
    contact_person_profile); blank optional fields arrive as None."""
    _require_details_editor(ticket, actor, "customer details")
    for field in (
        "business_name", "contact_name", "phone", "email",
        "business_type", "contact_person_profile",
    ):
        setattr(ticket, field, data.get(field))
    _log_event(
        db, ticket=ticket, actor=actor, event_type="CUSTOMER_UPDATED",
        payload={"business_name": ticket.business_name, "contact_name": ticket.contact_name},
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s customer details updated by %s", ticket.reference, actor.username)
    return ticket


def update_ticket_address(db: Session, ticket: Ticket, actor: User, data: dict) -> Ticket:
    """Correct the site address / location. `data` carries already-validated
    fields (line1..3, city, state, pincode, latitude, longitude); blank optional
    fields arrive as None."""
    _require_details_editor(ticket, actor, "address")
    for field in (
        "address_line1", "address_line2", "address_line3",
        "city", "state", "pincode", "latitude", "longitude",
    ):
        setattr(ticket, field, data.get(field))
    _log_event(
        db, ticket=ticket, actor=actor, event_type="ADDRESS_UPDATED",
        payload={"city": ticket.city, "state": ticket.state, "pincode": ticket.pincode},
    )
    db.commit()
    db.refresh(ticket)
    logger.info("Ticket %s address updated by %s", ticket.reference, actor.username)
    return ticket
