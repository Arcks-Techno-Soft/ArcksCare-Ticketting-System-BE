"""Email notification service.

Sends a richly-formatted HTML email to the company support inbox whenever a
new ticket is created. Uses async SMTP (aiosmtplib) so it doesn't block the
request thread.

If SMTP credentials are not configured (dev mode), we log the email instead
of sending - this keeps `python -m uvicorn` runnable with zero setup.
"""
from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Iterable, Optional

import aiosmtplib

from ..config import get_settings
from ..models.ticket import Ticket

logger = logging.getLogger("arckscare.email")


def _format_html(ticket: Ticket, support_url_base: Optional[str] = None) -> str:
    severity_color = {
        "LOW": "#16A34A",
        "MEDIUM": "#CA8A04",
        "HIGH": "#EA580C",
        "CRITICAL": "#DC2626",
    }.get(ticket.severity, "#525252")

    attachments_html = ""
    if ticket.attachments:
        items = "".join(
            f'<li><a href="{a.storage_url}" style="color:#0A0A0A;">{a.filename}</a> '
            f"({a.size_bytes // 1024} KB)</li>"
            for a in ticket.attachments
        )
        attachments_html = f"<h3 style='margin-top:24px'>Attachments</h3><ul>{items}</ul>"

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                max-width:640px;margin:0 auto;color:#0A0A0A;background:#FFFFFF;
                padding:32px;border:1px solid #E5E5E5;border-radius:12px;">
      <div style="border-bottom:1px solid #E5E5E5;padding-bottom:16px;margin-bottom:24px;">
        <h1 style="margin:0;font-size:22px;letter-spacing:-0.01em;">New Support Ticket</h1>
        <p style="margin:6px 0 0;color:#525252;font-size:14px;">
          Reference: <strong>{ticket.reference}</strong> &middot;
          Severity: <span style="color:{severity_color};font-weight:600;">{ticket.severity}</span>
        </p>
      </div>

      <h3 style="margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.06em;color:#737373;">Customer</h3>
      <p style="margin:0 0 4px;"><strong>{ticket.business_name}</strong> &middot; {ticket.business_type}</p>
      <p style="margin:0 0 4px;">{ticket.contact_name}</p>
      <p style="margin:0 0 12px;">{ticket.email} &middot; {ticket.phone}</p>

      <h3 style="margin:16px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.06em;color:#737373;">Address</h3>
      <p style="margin:0 0 4px;color:#0A0A0A;">{ticket.address_line1}</p>
      {f'<p style="margin:0 0 4px;color:#0A0A0A;">{ticket.address_line2}</p>' if ticket.address_line2 else ''}
      {f'<p style="margin:0 0 4px;color:#0A0A0A;">{ticket.address_line3}</p>' if ticket.address_line3 else ''}
      <p style="margin:0 0 8px;color:#525252;">{ticket.city}, {ticket.state} - {ticket.pincode}</p>
      {f'<p style="margin:0 0 20px;font-size:13px;"><a href="https://www.google.com/maps?q={ticket.latitude},{ticket.longitude}" style="color:#0A0A0A;">View on map &rarr;</a> <span style="color:#737373;">({ticket.latitude:.5f}, {ticket.longitude:.5f})</span></p>' if ticket.latitude is not None and ticket.longitude is not None else '<div style="margin-bottom:20px;"></div>'}

      <h3 style="margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.06em;color:#737373;">Product</h3>
      <p style="margin:0 0 4px;"><strong>{ticket.product_category}</strong></p>
      <p style="margin:0 0 20px;color:#525252;">Serial: <code>{ticket.serial_number}</code></p>

      <h3 style="margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.06em;color:#737373;">Issue</h3>
      <p style="margin:0 0 4px;"><strong>{ticket.issue_category}</strong></p>
      <p style="margin:0;white-space:pre-wrap;line-height:1.55;">{ticket.description}</p>

      {f'<p style="margin:16px 0 0;color:#525252;font-size:14px;">Preferred contact time: <strong>{ticket.preferred_contact_time}</strong></p>' if ticket.preferred_contact_time else ''}

      {attachments_html}

      <hr style="border:none;border-top:1px solid #E5E5E5;margin:28px 0 16px;" />
      <p style="margin:0;color:#737373;font-size:12px;">
        This notification was sent automatically by ArcksCare.
      </p>
    </div>
    """


def _format_text(ticket: Ticket) -> str:
    addr_parts = [ticket.address_line1]
    if ticket.address_line2:
        addr_parts.append(ticket.address_line2)
    if ticket.address_line3:
        addr_parts.append(ticket.address_line3)
    addr_parts.append(f"{ticket.city}, {ticket.state} - {ticket.pincode}")
    address_block = "\n".join(f"  {p}" for p in addr_parts)

    geo_line = ""
    if ticket.latitude is not None and ticket.longitude is not None:
        geo_line = (
            f"  Map: https://www.google.com/maps?q={ticket.latitude},{ticket.longitude}"
        )

    lines = [
        f"New Support Ticket - {ticket.reference}",
        f"Severity: {ticket.severity}",
        "",
        "CUSTOMER",
        f"  {ticket.business_name} ({ticket.business_type})",
        f"  {ticket.contact_name}",
        f"  {ticket.email}  {ticket.phone}",
        "",
        "ADDRESS",
        address_block,
    ]
    if geo_line:
        lines.append(geo_line)
    lines += [
        "",
        "PRODUCT",
        f"  {ticket.product_category}",
        f"  Serial: {ticket.serial_number}",
        "",
        "ISSUE",
        f"  {ticket.issue_category}",
        f"  {ticket.description}",
    ]
    if ticket.preferred_contact_time:
        lines.append("")
        lines.append(f"Preferred contact time: {ticket.preferred_contact_time}")
    return "\n".join(lines)


async def send_ticket_notification(ticket: Ticket, to: Optional[Iterable[str]] = None) -> bool:
    """Send the notification email. Returns True on success, False on failure.

    Failure to send email never blocks ticket creation - we log and move on.
    """
    settings = get_settings()
    recipients = list(to) if to else [settings.support_inbox]

    if not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            "SMTP not configured. Would have emailed %s about ticket %s",
            recipients,
            ticket.reference,
        )
        # In dev with no SMTP, also log a preview
        logger.info("Email preview:\n%s", _format_text(ticket))
        return False

    msg = EmailMessage()
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email or settings.smtp_user}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"[{ticket.severity}] {ticket.reference} - {ticket.issue_category} ({ticket.product_category})"
    msg.set_content(_format_text(ticket))
    msg.add_alternative(_format_html(ticket), subtype="html")

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=True,
            timeout=15,
        )
        logger.info("Sent ticket notification for %s to %s", ticket.reference, recipients)
        return True
    except Exception as exc:  # noqa: BLE001 - email is best-effort
        logger.exception("Failed to send ticket notification for %s: %s", ticket.reference, exc)
        return False
