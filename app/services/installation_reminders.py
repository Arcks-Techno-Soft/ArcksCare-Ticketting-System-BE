"""Upcoming-installation WhatsApp reminders for Super Admin / Admin / Managers.

When an installation carries an ``expected_installation_date``, leadership gets
a single heads-up WhatsApp a couple of days before that date so the job can be
staffed and the customer confirmed.

    Who     Super Admin (incl. legacy Owner) + Admin + Manager, active, with a
            phone on file — the same audience as the SLA reminders
            (``whatsapp._staff_phones`` / ``ADMIN_MANAGER_ROLES``).
    When    ``INSTALL_REMINDER_DAYS_BEFORE`` days before the expected date
            (default 2), but never before ``INSTALL_REMINDER_SEND_AFTER_HOUR``
            local time (default 9 AM) so a late-night create can't wake anyone.
    How often  Exactly once per installation. ``expected_date_reminder_sent_at``
            is the marker; changing the expected date clears it so the new date
            gets its own reminder.

Catch-up behaviour: an installation booked *inside* the lead window (created
today for the day after tomorrow, say) still gets its reminder on the next tick
rather than being silently skipped — the query matches every un-reminded
installation whose expected date falls between today and the target day.

Installations that are already COMPLETED or CLOSED are skipped: the work is
done, so a "coming up" nudge would be noise.

How it runs: a daemon thread started from the FastAPI startup hook wakes every
``INSTALL_REMINDER_TICK_SECONDS`` and calls :func:`run_installation_reminder_tick`,
which is a plain function you can also call directly from a test or a one-off
script. Reuses the Twilio plumbing in ``services.whatsapp``.

NOTE: like the SLA scheduler, the check-then-send is not coordinated across
processes. Keep INSTALL_REMINDER_SCHEDULER_ENABLED=true on ONE instance only.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import get_settings
from ..database import SessionLocal
from ..models.installation import Installation, InstallationStatus
from .whatsapp import (
    _send_one,
    _staff_phones,
    _twilio_configured,
    _twilio_endpoint,
)

logger = logging.getLogger("skposcare.installation_reminders")

# Statuses still worth a heads-up. COMPLETED/CLOSED are done deals.
_PENDING_STATUSES = (
    InstallationStatus.NEW.value,
    InstallationStatus.ASSIGNED.value,
)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def _local_tz(settings) -> ZoneInfo:
    """The configured business timezone, falling back to IST if it's bogus."""
    try:
        return ZoneInfo(settings.install_reminder_timezone)
    except Exception:  # noqa: BLE001 — bad env value must not kill the loop
        logger.warning(
            "Invalid INSTALL_REMINDER_TIMEZONE %r — falling back to Asia/Kolkata",
            settings.install_reminder_timezone,
        )
        return ZoneInfo("Asia/Kolkata")


def _when_phrase(days: int) -> str:
    """Human wording for how far off the installation is."""
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _format_date(d: date) -> str:
    """e.g. 'Mon, 20 Jul 2026' — unambiguous for an Indian audience."""
    return d.strftime("%a, %d %b %Y")


# --------------------------------------------------------------------------- #
# Message body
# --------------------------------------------------------------------------- #

def _build_bodies(
    *, reference: str, where: str, date_text: str, when_text: str, engineer: str
) -> tuple[list[str], str]:
    """Return ``(template_params, plain_text)``.

    Template variable order MUST match the approved template's
    {{1}}..{{5}} = reference, customer, expected date, when-phrase, engineer.
    """
    params = [reference, where, date_text, when_text, engineer]
    plain = (
        "*Upcoming Installation*\n\n"
        f"Installation {reference} is scheduled for {date_text} ({when_text}).\n\n"
        f"Customer: {where}\n"
        f"Engineer: {engineer}\n\n"
        "Open the ArcksCare app to confirm the schedule."
    )
    return params, plain


def _send_to_staff(
    settings,
    recipients: list[tuple[str, str]],
    params: list[str],
    plain: str,
) -> int:
    """Send one reminder to every recipient. Returns successful sends."""
    url, auth, from_addr = _twilio_endpoint()
    sid = settings.twilio_install_upcoming_content_sid
    ok = 0
    for phone, name in recipients:
        to_addr = f"whatsapp:{phone}"
        if sid:
            data = {
                "From": from_addr,
                "To": to_addr,
                "ContentSid": sid,
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

def run_installation_reminder_tick() -> int:
    """Run one pass. Returns the number of messages sent.

    Safe to call directly (tests, manual runs). No-op when the scheduler is
    disabled or Twilio isn't configured.
    """
    settings = get_settings()
    if not settings.install_reminder_scheduler_enabled:
        return 0
    if not _twilio_configured():
        logger.debug("Installation reminders: Twilio not configured — skipping tick")
        return 0

    tz = _local_tz(settings)
    now_local = datetime.now(tz)
    # Quiet hours: hold everything until the send-hour window opens. The next
    # tick after that hour picks the backlog up, so nothing is lost.
    after_hour = max(0, min(23, int(settings.install_reminder_send_after_hour)))
    if now_local.hour < after_hour:
        return 0

    today = now_local.date()
    lead = max(0, int(settings.install_reminder_days_before))
    target = today + timedelta(days=lead)

    total = 0
    with SessionLocal() as db:
        due = (
            db.query(Installation)
            .filter(Installation.expected_installation_date.isnot(None))
            # `<= target` catches anything booked inside the lead window;
            # `>= today` skips dates that have already gone by.
            .filter(Installation.expected_installation_date <= target)
            .filter(Installation.expected_installation_date >= today)
            .filter(Installation.expected_date_reminder_sent_at.is_(None))
            .filter(Installation.status.in_(_PENDING_STATUSES))
            .all()
        )
        if not due:
            return 0

        recipients = _staff_phones(db)
        if not recipients:
            # Nobody has a phone on file yet — leave the markers untouched so
            # the reminders still go out once numbers exist.
            logger.info(
                "Installation reminders: %d due but no staff phone numbers on file",
                len(due),
            )
            return 0

        now_utc = datetime.now(timezone.utc)
        for inst in due:
            days_away = (inst.expected_installation_date - today).days
            eng = inst.assigned_engineer
            engineer = (eng.name or eng.username) if eng is not None else "Not assigned yet"
            where = inst.business_name + (f", {inst.city}" if inst.city else "")
            params, plain = _build_bodies(
                reference=inst.reference,
                where=where,
                date_text=_format_date(inst.expected_installation_date),
                when_text=_when_phrase(days_away),
                engineer=engineer,
            )
            ok = _send_to_staff(settings, recipients, params, plain)
            # Mark as sent even when some recipients failed, so the ones who did
            # get it aren't re-blasted. Individual failures are logged in _send_one.
            inst.expected_date_reminder_sent_at = now_utc
            total += ok
            logger.info(
                "Upcoming-installation reminder for %s (%s, %s) -> %d/%d recipient(s)",
                inst.reference,
                inst.expected_installation_date,
                _when_phrase(days_away),
                ok,
                len(recipients),
            )
        db.commit()
    return total


# --------------------------------------------------------------------------- #
# Background thread
# --------------------------------------------------------------------------- #

_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def start_installation_reminder_scheduler() -> None:
    """Start the daemon loop if enabled. Idempotent; safe to call once at boot."""
    global _scheduler_thread
    settings = get_settings()
    if not settings.install_reminder_scheduler_enabled:
        logger.info(
            "Upcoming-installation reminder scheduler disabled (set "
            "INSTALL_REMINDER_SCHEDULER_ENABLED=true to enable)"
        )
        return
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return

    tick = max(60, int(settings.install_reminder_tick_seconds))

    def _loop() -> None:
        logger.info(
            "Upcoming-installation reminder scheduler running (tick=%ss, "
            "%s day(s) before, from %02d:00 %s)",
            tick,
            settings.install_reminder_days_before,
            settings.install_reminder_send_after_hour,
            settings.install_reminder_timezone,
        )
        # First run after one tick so the DB/seed has settled.
        while not _scheduler_stop.wait(tick):
            try:
                sent = run_installation_reminder_tick()
                if sent:
                    logger.info(
                        "Upcoming-installation reminders: %d message(s) sent this tick",
                        sent,
                    )
            except Exception:  # noqa: BLE001 — never let the loop die
                logger.exception(
                    "Upcoming-installation reminder tick failed; will retry next tick"
                )

    _scheduler_thread = threading.Thread(
        target=_loop, name="install-reminders", daemon=True
    )
    _scheduler_thread.start()


def stop_installation_reminder_scheduler() -> None:
    """Signal the loop to stop (used in tests / graceful shutdown)."""
    _scheduler_stop.set()
