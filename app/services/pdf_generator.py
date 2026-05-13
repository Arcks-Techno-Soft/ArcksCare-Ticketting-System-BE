"""Resolution-document PDF generator (ReportLab).

Composes a one-page PDF with:
  - ArcksCare header + ticket reference
  - Original ticket details (customer, address, product, issue)
  - Engineer's resolution summary
  - Time taken (resolving_started_at → resolved_at)
  - Both signatures (customer + engineer), inserted from PNG bytes

Returns the PDF as bytes — caller persists via the storage backend.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..models.resolution import Resolution
from ..models.ticket import Ticket

logger = logging.getLogger("arckscare.pdf")


# ----------------------------- helpers ----------------------------------- #

def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d %b %Y, %H:%M UTC")


def _fmt_duration(start: Optional[datetime], end: Optional[datetime]) -> str:
    if start is None or end is None:
        return "—"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    secs = max(0, int((end - start).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins, _ = divmod(secs, 60)
    if mins < 60:
        return f"{mins} min"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h {mins}m"
    days, hrs = divmod(hrs, 24)
    return f"{days}d {hrs}h {mins}m"


def _fetch_signature(storage_public_url: str) -> Optional[bytes]:
    """Fetch a signature PNG by URL. Used when storage is HTTPS (Supabase)."""
    if not storage_public_url:
        return None
    if not storage_public_url.startswith("http"):
        return None  # Local mode is handled separately by reading from disk.
    try:
        r = httpx.get(storage_public_url, timeout=15.0)
        r.raise_for_status()
        return r.content
    except Exception:
        logger.exception("Failed to fetch signature image")
        return None


def _read_local_signature(storage_url: str) -> Optional[bytes]:
    """Local storage stores URLs like '/uploads/<ref>/<file>'. Read the file off disk."""
    from pathlib import Path
    from ..config import get_settings

    settings = get_settings()
    if not storage_url.startswith("/uploads/"):
        return None
    relative = storage_url[len("/uploads/"):]
    p = Path(settings.local_upload_dir) / relative
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except Exception:
        logger.exception("Failed to read local signature %s", p)
        return None


# ----------------------------- main entry -------------------------------- #

def generate_resolution_pdf(ticket: Ticket, resolution: Resolution) -> bytes:
    """Render the resolution document and return the bytes."""
    from .storage import get_storage  # local import to avoid cycle

    storage = get_storage()
    # Resolve signature image bytes
    cust_bytes = engineer_bytes = None
    if resolution.customer_signature_storage_key:
        # Try local first (cheap), then fall back to public URL fetch (Supabase)
        cust_bytes = _read_local_signature(resolution.customer_signature_storage_key)
        if cust_bytes is None:
            cust_bytes = _fetch_signature(storage.public_url(resolution.customer_signature_storage_key))
    if resolution.engineer_signature_storage_key:
        engineer_bytes = _read_local_signature(resolution.engineer_signature_storage_key)
        if engineer_bytes is None:
            engineer_bytes = _fetch_signature(storage.public_url(resolution.engineer_signature_storage_key))

    # Build the PDF in memory
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"ArcksCare Resolution — {ticket.reference}",
        author="ArcksCare",
    )

    styles = getSampleStyleSheet()
    h_style = ParagraphStyle(
        "h",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        spaceAfter=2,
        textColor=colors.HexColor("#0A0A0A"),
    )
    sub_style = ParagraphStyle(
        "sub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        textColor=colors.HexColor("#525252"),
        spaceAfter=18,
    )
    section_label = ParagraphStyle(
        "sl",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        textColor=colors.HexColor("#737373"),
        spaceBefore=10,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#0A0A0A"),
    )

    story = []

    # ---- Header ----
    story.append(Paragraph("ArcksCare Service Resolution", h_style))
    story.append(Paragraph(
        f"Ticket {ticket.reference} &middot; Generated {_fmt_dt(datetime.now(timezone.utc))}",
        sub_style,
    ))

    # ---- Customer + Product table ----
    def kv(rows):
        data = [[Paragraph(f"<font color='#737373'>{k}</font>", body), Paragraph(v or "—", body)] for k, v in rows]
        t = Table(data, colWidths=[40 * mm, None])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
        ]))
        return t

    story.append(Paragraph("CUSTOMER", section_label))
    story.append(kv([
        ("Business", f"{ticket.business_name} ({ticket.business_type})"),
        ("Contact", ticket.contact_name),
        ("Phone", ticket.phone),
        ("Email", ticket.email),
    ]))

    addr_lines = [ticket.address_line1]
    if ticket.address_line2:
        addr_lines.append(ticket.address_line2)
    if ticket.address_line3:
        addr_lines.append(ticket.address_line3)
    addr_lines.append(f"{ticket.city}, {ticket.state} - {ticket.pincode}")
    story.append(Paragraph("ADDRESS", section_label))
    story.append(kv([("Service location", "<br/>".join(addr_lines))]))

    story.append(Paragraph("PRODUCT", section_label))
    story.append(kv([
        ("Category", ticket.product_category),
        ("Serial number", ticket.serial_number),
        ("Warranty", _warranty_label(ticket.warranty_status)),
    ]))

    story.append(Paragraph("ISSUE", section_label))
    story.append(kv([
        ("Category", ticket.issue_category),
        ("Severity", ticket.severity),
        ("Description", (ticket.description or "—").replace("\n", "<br/>")),
    ]))

    story.append(Paragraph("ASSIGNMENT", section_label))
    eng_name = ticket.assigned_engineer.name if ticket.assigned_engineer else "—"
    story.append(kv([
        ("Engineer", eng_name),
        ("Assigned", _fmt_dt(ticket.assigned_at)),
        ("Resolving started", _fmt_dt(ticket.resolving_started_at)),
        ("Resolved", _fmt_dt(ticket.resolved_at)),
        ("Time taken", _fmt_duration(ticket.resolving_started_at, ticket.resolved_at)),
    ]))

    story.append(Paragraph("RESOLUTION SUMMARY", section_label))
    summary = (ticket.resolution_summary or "—").replace("\n", "<br/>")
    story.append(Paragraph(summary, body))

    # ---- Signatures ----
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph("SIGNATURES", section_label))

    sig_cells = [
        _signature_cell(
            "Customer",
            ticket.contact_name,
            resolution.customer_signed_at,
            cust_bytes,
            body,
        ),
        _signature_cell(
            "Engineer",
            eng_name,
            resolution.engineer_signed_at,
            engineer_bytes,
            body,
        ),
    ]
    sig_table = Table([sig_cells], colWidths=[None, None])
    sig_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(sig_table)

    # ---- Footer ----
    story.append(Spacer(1, 14 * mm))
    story.append(Paragraph(
        "<font color='#737373'>This document is auto-generated by ArcksCare upon mutual acknowledgement of resolution. "
        "It serves as a record of service completion.</font>",
        body,
    ))

    doc.build(story)
    return buffer.getvalue()


# ----------------------------- internals --------------------------------- #

def _warranty_label(value: str) -> str:
    return {
        "UNDER_WARRANTY": "In warranty",
        "OUT_OF_WARRANTY": "Out of warranty",
        "UNKNOWN": "Not specified",
    }.get(value, value)


def _signature_cell(role: str, name: str, signed_at, png_bytes, body_style):
    """Build a single signature column: image (or '— not signed —'), name + timestamp."""
    rows = []
    if png_bytes:
        try:
            img = Image(io.BytesIO(png_bytes))
            # Constrain to 70mm wide, preserve aspect ratio
            max_w = 70 * mm
            ratio = img.imageWidth / img.imageHeight if img.imageHeight else 1
            img.drawWidth = max_w
            img.drawHeight = max_w / ratio if ratio else 35 * mm
            rows.append([img])
        except Exception:
            logger.exception("Failed to embed signature for %s", role)
            rows.append([Paragraph("<i>signature unreadable</i>", body_style)])
    else:
        rows.append([Paragraph(
            "<font color='#A3A3A3'><i>— not signed —</i></font>",
            body_style,
        )])

    rows.append([Paragraph(
        f"<font color='#737373'>{role}</font><br/><b>{name}</b><br/>"
        f"<font color='#737373' size='8'>{_fmt_dt(signed_at)}</font>",
        body_style,
    )])

    inner = Table(rows, colWidths=[80 * mm])
    inner.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.5, colors.HexColor("#D4D4D4")),
        ("BOTTOMPADDING", (0, 0), (0, 0), 4),
        ("TOPPADDING", (0, 1), (0, 1), 6),
    ]))
    return inner
