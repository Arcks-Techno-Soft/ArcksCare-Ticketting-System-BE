"""WhatsApp notifications to the credited sales rep on an installation.

Two events notify the installation's ``sales_rep``:

* ASSIGNED — a sales rep is credited on an installation (at create time, or
  when an Admin/Manager sets/changes the rep via the sales-rep endpoint).
* CLOSED   — the installation they sourced is completed and closed.

Fire-and-forget: callers invoke :func:`notify_sales_rep_assigned` /
:func:`notify_sales_rep_closed`, which spawn a daemon thread so the request
never blocks on Twilio. Each send opens its own DB session (the request's
session may already be closed/committed by the time the thread runs) and
reuses the Twilio plumbing in ``services.whatsapp``.

Silent no-op when Twilio isn't configured, the installation/rep is missing,
or the rep has no phone on file. Uses an approved content template when the
matching ``TWILIO_INSTALL_*_CONTENT_SID`` is set (required for a production
sender outside the 24h window), otherwise plain text (Twilio Sandbox).
"""
from __future__ import annotations

import json
import logging
import threading

from ..config import get_settings
from ..database import SessionLocal
from ..models.installation import Installation
from .whatsapp import (
    _normalise_phone,
    _send_one,
    _twilio_configured,
    _twilio_endpoint,
)

logger = logging.getLogger("skposcare.installation_notify")

KIND_ASSIGNED = "ASSIGNED"
KIND_CLOSED = "CLOSED"


def _build_bodies(kind: str, rep_name: str, reference: str, where: str) -> tuple[list[str], str]:
    """Return ``(template_params, plain_text)`` for the given event.

    Template variable order MUST match the approved template's
    {{1}}..{{3}} = rep name, installation reference, customer.
    """
    params = [rep_name, reference, where]
    if kind == KIND_ASSIGNED:
        plain = (
            "*New Installation Assigned*\n\n"
            f"Hi {rep_name}, you've been credited as the sales rep for a new "
            "installation.\n\n"
            f"Installation: {reference}\n"
            f"Customer: {where}\n\n"
            "Open the ArcksCare app to view the details."
        )
    else:  # KIND_CLOSED
        plain = (
            "*Installation Closed*\n\n"
            f"Hi {rep_name}, the installation you sourced has been completed and "
            "closed.\n\n"
            f"Installation: {reference}\n"
            f"Customer: {where}\n\n"
            "Thank you. Open the ArcksCare app for the full record."
        )
    return params, plain


def _content_sid(settings, kind: str) -> str:
    return (
        settings.twilio_install_assign_content_sid
        if kind == KIND_ASSIGNED
        else settings.twilio_install_closed_content_sid
    )


def _notify(installation_id: int, kind: str) -> None:
    """Send one WhatsApp message to the installation's sales rep."""
    if not _twilio_configured():
        logger.debug(
            "Installation notify: Twilio not configured — skipping %s for id=%s",
            kind, installation_id,
        )
        return

    settings = get_settings()
    with SessionLocal() as db:
        inst = db.get(Installation, installation_id)
        if inst is None:
            logger.warning("Installation notify: id=%s not found", installation_id)
            return
        rep = inst.sales_rep
        if rep is None:
            return  # no rep credited — nothing to notify
        phone = _normalise_phone(rep.phone)
        if not phone:
            logger.info(
                "Installation notify %s: sales rep %s has no phone — skipping",
                inst.reference, rep.username,
            )
            return

        rep_name = rep.name or rep.username
        where = inst.business_name + (f", {inst.city}" if inst.city else "")
        params, plain = _build_bodies(kind, rep_name, inst.reference, where)

        url, auth, from_addr = _twilio_endpoint()
        to_addr = f"whatsapp:{phone}"
        sid = _content_sid(settings, kind)
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

        ok = _send_one(url, auth, data, (phone, rep_name))
        logger.info(
            "Installation %s sales-rep %s alert -> %s (%s): %s",
            inst.reference, kind, rep.username, phone, "sent" if ok else "failed",
        )


def _dispatch(installation_id: int, kind: str) -> None:
    """Run :func:`_notify` in a daemon thread so the request never blocks."""
    threading.Thread(
        target=_notify,
        args=(installation_id, kind),
        name=f"install-notify-{kind.lower()}-{installation_id}",
        daemon=True,
    ).start()


def notify_sales_rep_assigned(installation_id: int) -> None:
    """Notify the credited sales rep that they've been assigned an installation."""
    _dispatch(installation_id, KIND_ASSIGNED)


def notify_sales_rep_closed(installation_id: int) -> None:
    """Notify the credited sales rep that their installation has been closed."""
    _dispatch(installation_id, KIND_CLOSED)
