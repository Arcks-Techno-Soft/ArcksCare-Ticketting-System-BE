"""SLA reminder scheduler — looping WhatsApp nudges for stuck tickets.

Sends repeating WhatsApp reminders while a ticket sits at an early workflow
stage, then goes quiet once a per-stage cap is hit (so a single stuck ticket
can never blast an unbounded number of messages — see the cost analysis).

Three stages, each with its own cadence and audience:

    Stage    Condition                          Every    Recipients
    ------   --------------------------------   ------   ----------------------------
    ACK      OPEN, not acknowledged             10 min   owner + admin + managers
    ASSIGN   ACKNOWLEDGED, not assigned         10 min   owner + admin + managers
    ACCEPT   ASSIGNED, engineer not accepted    30 min   managers + assigned engineer

All three are capped at ``settings.reminder_cap`` (default 5) reminders per
ticket per stage. After the cap the ticket goes silent — surface it in the
open-backlog view instead of continuing to ping.

Tickets on hold are skipped entirely: the wait is deliberate, so nudging
leadership about it every 10 minutes is pure noise. Reminder progress is left
untouched while held, so a resumed ticket picks its cadence back up rather
than starting over.

How it runs: a daemon thread (started from the FastAPI startup hook) wakes up
every ``reminder_tick_seconds`` and calls :func:`run_reminder_tick`, which is
itself a plain, side-effect-contained function you can also invoke directly
from a test or a one-off script. Progress is persisted in ``ticket_reminders``
so counts and cadence survive restarts.

Reuses the Twilio plumbing in ``services.whatsapp`` (endpoint, auth, single
send, phone normalisation, staff lookup, smart-link) so message formatting and
sandbox/template behaviour stay consistent with the other alerts.

NOTE: run this on a SINGLE web instance/worker. The check-then-send is not
coordinated across processes, so multiple concurrent schedulers could
double-send. Keep REMINDER_SCHEDULER_ENABLED=true on one instance only.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..database import SessionLocal
from ..models.ticket import Ticket, TicketStatus
from ..models.ticket_reminder import (
    STAGE_ACCEPT,
    STAGE_ACK,
    STAGE_ASSIGN,
    TicketReminder,
)
from .whatsapp import (
    _build_link,
    _normalise_phone,
    _resolve_link_base,
    _send_one,
    _staff_phones,
    _twilio_configured,
    _twilio_endpoint,
)

logger = logging.getLogger("skposcare.reminders")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive DB timestamp to timezone-aware UTC.

    SQLite round-trips ``DateTime(timezone=True)`` as naive; Postgres keeps the
    tz. Treating naive values as UTC keeps the arithmetic correct on both.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Recipient resolution
# --------------------------------------------------------------------------- #

def _dedupe(recipients: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for phone, name in recipients:
        if phone and phone not in seen:
            seen.add(phone)
            out.append((phone, name))
    return out


def _leadership(db) -> list[tuple[str, str]]:
    """Owner + admin + managers with a phone on file."""
    return _dedupe(_staff_phones(db))


def _leadership_plus_engineer(db, ticket: Ticket) -> list[tuple[str, str]]:
    """Managers plus the assigned engineer (ACCEPT stage audience)."""
    out = list(_staff_phones(db))
    eng = ticket.assigned_engineer
    if eng is not None:
        phone = _normalise_phone(eng.phone)
        if phone:
            out.append((phone, eng.name or eng.username))
    return _dedupe(out)


# --------------------------------------------------------------------------- #
# Message body
# --------------------------------------------------------------------------- #

def _send_reminder(
    settings,
    recipients: list[tuple[str, str]],
    *,
    reference: str,
    where: str,
    stage_line: str,
    link: str,
) -> int:
    """Send one reminder to every recipient. Returns count of successful sends.

    Uses an approved content template when ``TWILIO_REMINDER_CONTENT_SID`` is
    set (production sender), otherwise a plain-text body (Twilio Sandbox).
    Template vars {{1}}..{{4}} = stage-line, reference, where, link.
    """
    url, auth, from_addr = _twilio_endpoint()
    use_template = bool(settings.twilio_reminder_content_sid)
    params = [stage_line, reference, where, link or "-"]
    plain = (
        f"⏰ *SLA Reminder — {reference}*\n\n"
        f"{stage_line}\n"
        f"\U0001f4cd {where}"
        + (f"\n\n\U0001f449 Open: {link}" if link else "")
    )
    ok = 0
    for phone, name in recipients:
        to_addr = f"whatsapp:{phone}"
        if use_template:
            data = {
                "From": from_addr,
                "To": to_addr,
                "ContentSid": settings.twilio_reminder_content_sid,
                "ContentVariables": json.dumps(
                    {str(i + 1): v for i, v in enumerate(params)}
                ),
            }
        else:
            data = {"From": from_addr, "To": to_addr, "Body": plain}
        if _send_one(url, auth, data, (phone, name)):
            ok += 1
    return ok


# --------------------------------------------------------------------------- #
# Core tick
# --------------------------------------------------------------------------- #

def _get_or_create_row(db, ticket_id: int, stage: str) -> TicketReminder:
    row = (
        db.query(TicketReminder)
        .filter(TicketReminder.ticket_id == ticket_id, TicketReminder.stage == stage)
        .one_or_none()
    )
    if row is None:
        row = TicketReminder(ticket_id=ticket_id, stage=stage, sent_count=0)
        db.add(row)
        db.flush()
    return row


def _process_stage(
    db,
    settings,
    *,
    stage: str,
    tickets: list[Ticket],
    base_getter,
    interval_minutes: int,
    recipients_getter,
    stage_line_getter,
    link_base: str | None,
) -> int:
    """Send at most one due reminder per ticket for one stage. Returns sends."""
    cap = max(0, int(settings.reminder_cap))
    if cap == 0 or interval_minutes <= 0:
        return 0
    interval = timedelta(minutes=interval_minutes)
    now = datetime.now(timezone.utc)
    total_ok = 0

    for t in tickets:
        base = _aware(base_getter(t)) or _aware(t.created_at)
        if base is None:
            continue
        row = _get_or_create_row(db, t.id, stage)
        if row.sent_count >= cap:
            continue
        # How many reminders SHOULD have fired by now (capped).
        due = min(cap, int((now - base) // interval))
        if row.sent_count >= due:
            continue
        # Spacing guard: one send per tick, and never closer than ~one interval
        # apart even if the loop catches up after downtime.
        last = _aware(row.last_sent_at)
        if last is not None and (now - last) < interval * 0.9:
            continue

        recipients = recipients_getter(db, t)
        if not recipients:
            # No phone numbers configured yet — leave the counter untouched so
            # the reminder still goes out once numbers exist.
            continue

        reminder_no = row.sent_count + 1
        elapsed_min = int((now - base).total_seconds() // 60)
        stage_line = stage_line_getter(t, elapsed_min, reminder_no, cap)
        where = f"{t.business_name}, {t.city}"
        link = _build_link(t.reference, link_base) if link_base else ""

        ok = _send_reminder(
            settings,
            recipients,
            reference=t.reference,
            where=where,
            stage_line=stage_line,
            link=link,
        )
        # Count the cycle as sent even if some recipients failed, so we don't
        # re-blast the ones that succeeded. Failures are logged in _send_one.
        row.sent_count = reminder_no
        row.last_sent_at = now
        total_ok += ok
        logger.info(
            "Reminder %s #%d/%d for %s -> %d/%d recipient(s)",
            stage, reminder_no, cap, t.reference, ok, len(recipients),
        )

    db.commit()
    return total_ok


# --- per-stage stage-line text ------------------------------------------- #

def _ack_line(t, mins, n, cap):
    return f"Not acknowledged for {mins} min — please acknowledge. (reminder {n}/{cap})"


def _assign_line(t, mins, n, cap):
    return (
        f"Acknowledged but not assigned for {mins} min — please assign an "
        f"engineer. (reminder {n}/{cap})"
    )


def _accept_line(t, mins, n, cap):
    eng = t.assigned_engineer
    eng_name = (eng.name or eng.username) if eng is not None else "the engineer"
    return (
        f"Assigned but not accepted for {mins} min — {eng_name}, please accept. "
        f"(reminder {n}/{cap})"
    )


def run_reminder_tick() -> int:
    """Run one pass over all three stages. Returns total messages sent.

    Safe to call directly (tests, manual runs). No-op when the scheduler is
    disabled or Twilio isn't configured.
    """
    settings = get_settings()
    if not settings.reminder_scheduler_enabled:
        return 0
    if not _twilio_configured():
        logger.debug("Reminders: Twilio not configured — skipping tick")
        return 0

    link_base = _resolve_link_base(settings)
    total = 0
    with SessionLocal() as db:
        # Stage 1 — OPEN, not acknowledged.
        open_tickets = (
            db.query(Ticket)
            .filter(Ticket.status == TicketStatus.OPEN.value)
            .filter(Ticket.deleted_at.is_(None))
            .filter(Ticket.held_at.is_(None))
            .all()
        )
        total += _process_stage(
            db, settings,
            stage=STAGE_ACK,
            tickets=open_tickets,
            base_getter=lambda t: t.created_at,
            interval_minutes=settings.reminder_ack_interval_minutes,
            recipients_getter=lambda db, t: _leadership(db),
            stage_line_getter=_ack_line,
            link_base=link_base,
        )

        # Stage 2 — ACKNOWLEDGED, not assigned.
        ack_tickets = (
            db.query(Ticket)
            .filter(Ticket.status == TicketStatus.ACKNOWLEDGED.value)
            .filter(Ticket.deleted_at.is_(None))
            .filter(Ticket.held_at.is_(None))
            .all()
        )
        total += _process_stage(
            db, settings,
            stage=STAGE_ASSIGN,
            tickets=ack_tickets,
            base_getter=lambda t: t.acknowledged_at,
            interval_minutes=settings.reminder_assign_interval_minutes,
            recipients_getter=lambda db, t: _leadership(db),
            stage_line_getter=_assign_line,
            link_base=link_base,
        )

        # Stage 3 — ASSIGNED, engineer hasn't accepted.
        assigned_tickets = (
            db.query(Ticket)
            .filter(Ticket.status == TicketStatus.ASSIGNED.value)
            .filter(Ticket.deleted_at.is_(None))
            .filter(Ticket.held_at.is_(None))
            .all()
        )
        total += _process_stage(
            db, settings,
            stage=STAGE_ACCEPT,
            tickets=assigned_tickets,
            base_getter=lambda t: t.assigned_at,
            interval_minutes=settings.reminder_accept_interval_minutes,
            recipients_getter=_leadership_plus_engineer,
            stage_line_getter=_accept_line,
            link_base=link_base,
        )
    return total


# --------------------------------------------------------------------------- #
# Background thread
# --------------------------------------------------------------------------- #

_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def start_reminder_scheduler() -> None:
    """Start the daemon loop if enabled. Idempotent; safe to call once at boot."""
    global _scheduler_thread
    settings = get_settings()
    if not settings.reminder_scheduler_enabled:
        logger.info(
            "SLA reminder scheduler disabled (set REMINDER_SCHEDULER_ENABLED=true "
            "to enable)"
        )
        return
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return

    tick = max(15, int(settings.reminder_tick_seconds))

    def _loop() -> None:
        logger.info(
            "SLA reminder scheduler running (tick=%ss, cap=%s, ack=%smin, "
            "assign=%smin, accept=%smin)",
            tick, settings.reminder_cap,
            settings.reminder_ack_interval_minutes,
            settings.reminder_assign_interval_minutes,
            settings.reminder_accept_interval_minutes,
        )
        # First run after one tick so the DB/seed has settled.
        while not _scheduler_stop.wait(tick):
            try:
                sent = run_reminder_tick()
                if sent:
                    logger.info("SLA reminders: %d message(s) sent this tick", sent)
            except Exception:  # noqa: BLE001 — never let the loop die
                logger.exception("SLA reminder tick failed; will retry next tick")

    _scheduler_thread = threading.Thread(target=_loop, name="sla-reminders", daemon=True)
    _scheduler_thread.start()


def stop_reminder_scheduler() -> None:
    """Signal the loop to stop (used in tests / graceful shutdown)."""
    _scheduler_stop.set()
