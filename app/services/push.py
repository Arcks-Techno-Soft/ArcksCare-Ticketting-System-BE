"""Expo push notifications.

Stores device tokens and sends system notifications via Expo's push service
(https://docs.expo.dev/push-notifications/sending-notifications/). We talk to
the public Expo endpoint, which then relays to FCM (Android) / APNs (iOS).

All sends are best-effort: failures are logged but never raised to the caller,
so a push hiccup can't break ticket creation or assignment. These functions are
designed to be run from FastAPI BackgroundTasks alongside the existing email and
WhatsApp notifications.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models.push_token import DevicePushToken
from ..models.user import User, UserRole, ADMIN_MANAGER_ROLES, ADMIN_ROLES, SUPER_ADMIN_ROLES

logger = logging.getLogger("skposcare.push")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_CHUNK = 100  # Expo accepts up to 100 messages per request.


# --------------------------- token storage ------------------------------- #

def register_token(db: Session, user: User, token: str, platform: Optional[str]) -> DevicePushToken:
    """Upsert an Expo push token for a user (idempotent on the token string)."""
    token = token.strip()
    existing = db.query(DevicePushToken).filter(DevicePushToken.token == token).one_or_none()
    if existing is not None:
        # Re-point the token at whoever is logged in now (device handed over,
        # re-login as a different user, etc.).
        existing.user_id = user.id
        if platform:
            existing.platform = platform
        db.commit()
        db.refresh(existing)
        return existing
    row = DevicePushToken(token=token, user_id=user.id, platform=platform)
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Registered push token for user %s (%s)", user.username, platform)
    return row


def unregister_token(db: Session, token: str) -> None:
    """Remove a token (called on logout). Silent if it doesn't exist."""
    db.query(DevicePushToken).filter(DevicePushToken.token == token.strip()).delete()
    db.commit()


def _tokens_for_users(db: Session, user_ids: Sequence[int]) -> list[str]:
    if not user_ids:
        return []
    rows = db.execute(
        select(DevicePushToken.token).where(DevicePushToken.user_id.in_(user_ids))
    ).scalars().all()
    return list(rows)


# --------------------------- low-level send ------------------------------ #

def _send_to_tokens(tokens: Sequence[str], title: str, body: str, data: dict) -> None:
    if not tokens:
        logger.info("No device tokens to notify; skipping push '%s'", title)
        return
    messages = [
        {
            "to": t,
            "title": title,
            "body": body,
            "data": data,
            "sound": "default",
            "priority": "high",
            "channelId": "default",
        }
        for t in tokens
    ]
    try:
        with httpx.Client(timeout=10.0) as client:
            for i in range(0, len(messages), _CHUNK):
                chunk = messages[i : i + _CHUNK]
                resp = client.post(
                    EXPO_PUSH_URL,
                    json=chunk,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                resp.raise_for_status()
                logger.info("Sent %d push message(s): %s", len(chunk), resp.json().get("data", "ok"))
    except Exception as exc:  # noqa: BLE001 — best-effort, never propagate
        logger.warning("Expo push send failed for '%s': %s", title, exc)


# --------------------------- ticket event helpers ------------------------ #
# These open their own DB session because they run as BackgroundTasks after the
# request's session has closed. They take only IDs to stay session-safe.

def notify_new_ticket(ticket_id: int) -> None:
    """New ticket raised → notify all active Admins and Managers."""
    with SessionLocal() as db:
        from ..models.ticket import Ticket  # local import avoids load cycle

        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            return
        staff_ids = db.execute(
            select(User.id).where(
                User.active.is_(True),
                User.role.in_(ADMIN_MANAGER_ROLES),
            )
        ).scalars().all()
        tokens = _tokens_for_users(db, staff_ids)
        _send_to_tokens(
            tokens,
            title="New ticket raised",
            body=f"{ticket.reference} · {ticket.business_name} — {ticket.issue_category}",
            data={"type": "NEW_TICKET", "reference": ticket.reference},
        )


def notify_ticket_assigned(ticket_id: int, engineer_id: int) -> None:
    """Ticket assigned → notify just the assigned engineer."""
    with SessionLocal() as db:
        from ..models.ticket import Ticket  # local import avoids load cycle

        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            return
        tokens = _tokens_for_users(db, [engineer_id])
        _send_to_tokens(
            tokens,
            title="New ticket assigned to you",
            body=f"{ticket.reference} · {ticket.business_name} — {ticket.issue_category}",
            data={"type": "TICKET_ASSIGNED", "reference": ticket.reference},
        )
