"""WhatsApp notification service via Twilio.

Sends a WhatsApp message to every active ADMIN or MANAGER user whose
``phone`` column is populated whenever a new ticket is created.

Two send paths, both hitting the same Twilio Messages REST endpoint:

* If ``TWILIO_CONTENT_SID`` is set, the message is sent as an approved
  content template with positional ``ContentVariables`` — required for
  production WhatsApp senders outside the 24-hour user-initiated window.
* Otherwise the message is sent as a plain text body. This is the path
  used with the Twilio WhatsApp Sandbox, where each recipient has
  already opted in by sending "join {sandbox-keyword}" from their
  WhatsApp to the sandbox number.

Silent no-op if any of ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, or
``TWILIO_WHATSAPP_FROM`` is missing, so the app deploys cleanly before
Twilio has been configured.

Designed to be invoked from a FastAPI BackgroundTask — opens its own DB
session because the request session is already closed by the time the
task runs.
"""

from __future__ import annotations

import json
import logging

import httpx

from ..config import get_settings
from ..database import SessionLocal
from ..models.ticket import Ticket
from ..models.user import User, UserRole

logger = logging.getLogger("skposcare.whatsapp")

TWILIO_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _normalise_phone(raw: str | None) -> str | None:
    """Twilio expects E.164 with a leading '+' for WhatsApp recipients."""
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return f"+{digits}" if digits else None


def _staff_phones(db) -> list[tuple[str, str]]:
    """Return ``[(phone, display_name), …]`` for every active ADMIN / MANAGER."""
    users = (
        db.query(User)
        .filter(User.role.in_([UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value]))
        .filter(User.active.is_(True))
        .all()
    )
    out: list[tuple[str, str]] = []
    for u in users:
        phone = _normalise_phone(u.phone)
        if phone:
            out.append((phone, u.name or u.username))
    return out


def _build_link(reference: str, base: str) -> str:
    return f"{base.rstrip('/')}/r/{reference}"


def _build_plain_body(body_params: list[str]) -> str:
    """Plain-text formatter for the new-ticket alert (owners + managers).

    Severity intentionally omitted — the public raise-ticket form doesn't
    ask the customer for it, so every customer-created ticket arrives with
    the backend default (``MEDIUM``). Rendering that in the alert would
    misleadingly suggest the customer rated the urgency. The assigned
    engineer's alert keeps severity, since by that point an owner or
    manager has had a chance to triage it.
    """
    ref, where, what, link = body_params
    return (
        f"\U0001f195 *New Support Ticket — {ref}*\n\n"
        f"\U0001f4cd {where}\n"
        f"\U0001f527 {what}\n\n"
        f"A new ticket has been raised and needs triage.\n"
        f"\U0001f449 View & assign: {link}"
    )


def _build_assignment_body(body_params: list[str]) -> str:
    """Plain-text formatter for the engineer-assignment alert.

    Different layout than the new-ticket alert — leads with the action
    (assigned), names the assigner, so the engineer immediately knows
    they have a new job and who handed it to them.
    """
    ref, where, what, severity, assigned_by, link = body_params
    return (
        f"\U0001f527 *Ticket Assigned to You — {ref}*\n"
        f"\U000026a0\U0000fe0f Severity: {severity}\n\n"
        f"\U0001f4cd {where}\n"
        f"\U0001f6e0\U0000fe0f {what}\n"
        f"\U0001f464 Assigned by: {assigned_by}\n\n"
        f"Please acknowledge and attend.\n"
        f"\U0001f449 Open: {link}"
    )


def _send_one(url: str, auth, data: dict, who: tuple[str, str]) -> bool:
    """POST one message to Twilio. Returns True on 2xx, False otherwise.

    `who` is ``(phone, display_name)`` — used only for log lines.
    """
    phone, name = who
    try:
        resp = httpx.post(url, data=data, auth=auth, timeout=10.0)
        if resp.status_code >= 400:
            logger.warning(
                "Twilio WhatsApp send failed for %s (%s): HTTP %s — %s",
                name, phone, resp.status_code, resp.text[:300],
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — last-ditch logging
        logger.warning(
            "Twilio WhatsApp send raised for %s (%s): %s", name, phone, exc
        )
        return False


def _twilio_configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_whatsapp_from)


def _twilio_endpoint() -> tuple[str, tuple[str, str], str]:
    """Return ``(url, basic_auth, from_address)`` for outgoing Twilio calls."""
    s = get_settings()
    url = TWILIO_API_URL.format(sid=s.twilio_account_sid)
    auth = (s.twilio_account_sid, s.twilio_auth_token)
    from_addr = s.twilio_whatsapp_from
    if not from_addr.startswith("whatsapp:"):
        from_addr = f"whatsapp:{from_addr}"
    return url, auth, from_addr


def _resolve_link_base(settings) -> str | None:
    """Pick the smart-link host, preferring the explicit WHATSAPP_LINK_BASE."""
    link_base = settings.whatsapp_link_base or (
        settings.cors_origins_list[0] if settings.cors_origins_list else ""
    )
    return link_base or None


def send_new_ticket_alert(ticket_id: int) -> None:
    """Background task: notify owners + managers about a new ticket on WhatsApp."""
    if not _twilio_configured():
        logger.debug(
            "Twilio not configured — skipping new-ticket alert for ticket id=%s",
            ticket_id,
        )
        return

    settings = get_settings()
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            logger.warning("WhatsApp notify: ticket id=%s not found", ticket_id)
            return

        recipients = _staff_phones(db)
        if not recipients:
            logger.info(
                "WhatsApp notify %s: no ADMIN/MANAGER user has a phone set",
                ticket.reference,
            )
            return

        link_base = _resolve_link_base(settings)
        if not link_base:
            logger.warning(
                "WhatsApp notify %s: WHATSAPP_LINK_BASE not set and CORS empty; "
                "skipping",
                ticket.reference,
            )
            return

        link = _build_link(ticket.reference, link_base)

        # Order MUST match the template's {{1}}..{{4}} placeholders when a
        # ContentSid is in use; the plain-body formatter uses the same order.
        # Severity is intentionally omitted here — see _build_plain_body docs.
        body_params = [
            ticket.reference,
            f"{ticket.business_name}, {ticket.city}",
            f"{ticket.product_category} — {ticket.issue_category}",
            link,
        ]

        url, auth, from_addr = _twilio_endpoint()
        use_template = bool(settings.twilio_content_sid)

        ok = 0
        for phone, name in recipients:
            to_addr = f"whatsapp:{phone}"
            if use_template:
                data = {
                    "From": from_addr,
                    "To": to_addr,
                    "ContentSid": settings.twilio_content_sid,
                    # Twilio expects a JSON-encoded mapping of "1","2",… → text.
                    "ContentVariables": json.dumps(
                        {str(i + 1): v for i, v in enumerate(body_params)}
                    ),
                }
            else:
                data = {
                    "From": from_addr,
                    "To": to_addr,
                    "Body": _build_plain_body(body_params),
                }
            if _send_one(url, auth, data, (phone, name)):
                ok += 1

        logger.info(
            "WhatsApp notify %s: sent to %d/%d staff",
            ticket.reference,
            ok,
            len(recipients),
        )


def send_engineer_assignment_alert(
    ticket_id: int, engineer_id: int, assigned_by_id: int
) -> None:
    """Background task: WhatsApp the engineer when a ticket is assigned to them.

    Fires from the `/admin/tickets/{ref}/assign` endpoint. Looks up the
    engineer's phone, builds an assignment-flavoured message body, and
    POSTs it through the same Twilio plumbing as `send_new_ticket_alert`.

    Silent no-op when:
      * Twilio isn't configured (env vars blank)
      * The ticket / engineer / assigner has been deleted between
        request handling and background dispatch
      * The engineer has no phone on file
      * The smart-link host (WHATSAPP_LINK_BASE) is unresolved
    """
    if not _twilio_configured():
        logger.debug(
            "Twilio not configured — skipping assignment alert for ticket id=%s",
            ticket_id,
        )
        return

    settings = get_settings()
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        engineer = db.get(User, engineer_id)
        assigned_by = db.get(User, assigned_by_id)
        if ticket is None or engineer is None:
            logger.warning(
                "Assignment alert: ticket id=%s or engineer id=%s missing",
                ticket_id, engineer_id,
            )
            return

        phone = _normalise_phone(engineer.phone)
        if not phone:
            logger.info(
                "Assignment alert %s: engineer %s has no phone — skipping",
                ticket.reference, engineer.username,
            )
            return

        link_base = _resolve_link_base(settings)
        if not link_base:
            logger.warning(
                "Assignment alert %s: WHATSAPP_LINK_BASE not set; skipping",
                ticket.reference,
            )
            return

        link = _build_link(ticket.reference, link_base)
        assigned_by_label = (
            (assigned_by.name or assigned_by.username) if assigned_by else "Manager"
        )

        # Order MUST match the assignment template's {{1}}..{{6}} placeholders
        # if/when one is approved. The plain-body formatter uses the same order.
        body_params = [
            ticket.reference,
            f"{ticket.business_name}, {ticket.city}",
            f"{ticket.product_category} — {ticket.issue_category}",
            (ticket.severity or "").title(),
            assigned_by_label,
            link,
        ]

        url, auth, from_addr = _twilio_endpoint()
        to_addr = f"whatsapp:{phone}"
        # Use the dedicated assignment content template when one is configured
        # (required for a production WhatsApp sender to reach the engineer
        # outside the 24h session window). The variable order MUST match the
        # template's {{1}}..{{6}} placeholders — same order as body_params.
        # Falls back to plain text (Sandbox) when no assignment SID is set.
        if settings.twilio_assignment_content_sid:
            data = {
                "From": from_addr,
                "To": to_addr,
                "ContentSid": settings.twilio_assignment_content_sid,
                "ContentVariables": json.dumps(
                    {str(i + 1): v for i, v in enumerate(body_params)}
                ),
            }
        else:
            data = {
                "From": from_addr,
                "To": to_addr,
                "Body": _build_assignment_body(body_params),
            }
        ok = _send_one(url, auth, data, (phone, engineer.name or engineer.username))
        logger.info(
            "Assignment alert %s -> %s (%s): %s",
            ticket.reference,
            engineer.username,
            phone,
            "sent" if ok else "failed",
        )
