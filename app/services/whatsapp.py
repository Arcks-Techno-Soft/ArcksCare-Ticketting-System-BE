"""WhatsApp Cloud API (Meta) notification service.

Sends a templated WhatsApp message to every active OWNER or MANAGER user
whose `phone` column is populated whenever a new ticket is created.

Silent no-op if any of the required env vars (`WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_TEMPLATE_NAME`) is missing, so the app
deploys cleanly before the Meta side has been configured.

Designed to be invoked from a FastAPI BackgroundTask — opens its own DB
session (the request session is already closed by the time it runs).
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings
from ..database import SessionLocal
from ..models.ticket import Ticket
from ..models.user import User, UserRole

logger = logging.getLogger("skposcare.whatsapp")

GRAPH_API_VERSION = "v21.0"
WHATSAPP_GRAPH_URL = "https://graph.facebook.com/{ver}/{phone_id}/messages"


def _normalise_phone(raw: str | None) -> str | None:
    """Meta expects E.164 digits with no leading '+'. Strip everything else."""
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return digits or None


def _staff_phones(db) -> list[tuple[str, str]]:
    """Return [(phone, display_name), …] for every active OWNER / MANAGER."""
    users = (
        db.query(User)
        .filter(User.role.in_([UserRole.OWNER.value, UserRole.MANAGER.value]))
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


def _build_template_payload(
    to: str,
    template_name: str,
    language: str,
    body_params: list[str],
) -> dict:
    """Body-only template payload — no header/buttons."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            ],
        },
    }


def send_new_ticket_alert(ticket_id: int) -> None:
    """Background task: notify owners + managers about a new ticket on WhatsApp."""
    settings = get_settings()

    if not (
        settings.whatsapp_phone_number_id
        and settings.whatsapp_access_token
        and settings.whatsapp_template_name
    ):
        logger.debug(
            "WhatsApp not configured — skipping notification for ticket id=%s",
            ticket_id,
        )
        return

    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            logger.warning("WhatsApp notify: ticket id=%s not found", ticket_id)
            return

        recipients = _staff_phones(db)
        if not recipients:
            logger.info(
                "WhatsApp notify %s: no OWNER/MANAGER user has a phone set",
                ticket.reference,
            )
            return

        link_base = (
            settings.whatsapp_link_base
            or settings.cors_origins_list[0]
            if settings.cors_origins_list
            else ""
        )
        if not link_base:
            logger.warning(
                "WhatsApp notify %s: WHATSAPP_LINK_BASE not set and CORS empty; "
                "skipping",
                ticket.reference,
            )
            return

        link = _build_link(ticket.reference, link_base)

        # Order MUST match the template's {{1}}..{{5}} placeholders.
        body_params = [
            ticket.reference,
            f"{ticket.business_name}, {ticket.city}",
            f"{ticket.product_category} — {ticket.issue_category}",
            (ticket.severity or "").title(),
            link,
        ]

        url = WHATSAPP_GRAPH_URL.format(
            ver=GRAPH_API_VERSION,
            phone_id=settings.whatsapp_phone_number_id,
        )
        headers = {
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

        ok = 0
        for phone, name in recipients:
            payload = _build_template_payload(
                phone,
                settings.whatsapp_template_name,
                settings.whatsapp_template_language,
                body_params,
            )
            try:
                resp = httpx.post(url, headers=headers, json=payload, timeout=10.0)
                if resp.status_code >= 400:
                    logger.warning(
                        "WhatsApp send failed for %s (%s): HTTP %s — %s",
                        name,
                        phone,
                        resp.status_code,
                        resp.text[:300],
                    )
                else:
                    ok += 1
            except Exception as exc:  # noqa: BLE001 — last-ditch logging
                logger.warning(
                    "WhatsApp send raised for %s (%s): %s", name, phone, exc
                )

        logger.info(
            "WhatsApp notify %s: sent to %d/%d staff",
            ticket.reference,
            ok,
            len(recipients),
        )
