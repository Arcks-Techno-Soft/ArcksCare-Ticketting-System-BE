"""Resolution-document PDF generator (ReportLab).

Composes a one-page PDF with:
  - SK-POS Support header + ticket reference
  - Original ticket details (customer, address, product, issue)
  - Engineer's resolution summary
  - Time taken (resolving_started_at → resolved_at)
  - Both signatures (customer + engineer), inserted from PNG bytes

Returns the PDF as bytes — caller persists via the storage backend.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..models.resolution import Resolution
from ..models.ticket import Ticket
from .spares import compute_charges

logger = logging.getLogger("skposcare.pdf")


# ----------------------------- helpers ----------------------------------- #

# Asia/Kolkata is a fixed +05:30 offset (no DST in India), so we encode it
# directly to avoid a zoneinfo dependency that's missing on some slim images.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


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
        title=f"SK-POS Support Resolution — {ticket.reference}",
        author="SK-POS Support",
    )

    styles = getSampleStyleSheet()
    h_style = ParagraphStyle(
        "h",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        spaceAfter=2,
        textColor=colors.HexColor("#0A0A0A"),
        alignment=TA_CENTER,
    )
    sub_style = ParagraphStyle(
        "sub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        textColor=colors.HexColor("#525252"),
        spaceAfter=14,
        alignment=TA_CENTER,
    )
    body = ParagraphStyle(
        "body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#0A0A0A"),
    )

    story = []

    # ---- Header ----
    story.append(Paragraph("SK-POS Support Service Resolution", h_style))
    story.append(Paragraph(
        f"Ticket {ticket.reference} &middot; Generated {_fmt_dt(datetime.now(timezone.utc))}",
        sub_style,
    ))

    eng_name = ticket.assigned_engineer.name if ticket.assigned_engineer else "—"

    # ---- Customer info (merged customer + address) ----
    addr_lines = [ticket.address_line1]
    if ticket.address_line2:
        addr_lines.append(ticket.address_line2)
    if ticket.address_line3:
        addr_lines.append(ticket.address_line3)
    addr_lines.append(f"{ticket.city}, {ticket.state} - {ticket.pincode}")
    story.append(_full_card("CUSTOMER INFO", [
        ("Business", f"{ticket.business_name} ({ticket.business_type})"),
        ("Contact", ticket.contact_name),
        ("Phone", ticket.phone),
        ("Email", ticket.email),
        ("Service location", "<br/>".join(addr_lines)),
        ("City", ticket.city),
        ("State", ticket.state),
        ("Pincode", ticket.pincode),
    ], body))
    story.append(Spacer(1, 4 * mm))

    # ---- Issue info (merged product + issue) ----
    story.append(_full_card("ISSUE INFO", [
        ("Product category", ticket.product_category),
        ("Serial number", ticket.serial_number),
        ("Warranty status", _warranty_label(ticket.warranty_status)),
        ("Issue category", ticket.issue_category),
        ("Severity", ticket.severity),
        ("Description", (ticket.description or "—").replace("\n", "<br/>")),
    ], body))
    story.append(Spacer(1, 4 * mm))

    # ---- Assignment: 2-column key/value grid ----
    story.append(_grid_card("ASSIGNMENT", [
        ("Engineer", eng_name),
        ("Assigned", _fmt_dt(ticket.assigned_at)),
        ("Resolving started", _fmt_dt(ticket.resolving_started_at)),
        ("Resolved", _fmt_dt(ticket.resolved_at)),
        ("Time taken", _fmt_duration(ticket.resolving_started_at, ticket.resolved_at)),
    ], body))
    story.append(Spacer(1, 4 * mm))

    # ---- Resolution summary card (full width) ----
    summary = (ticket.resolution_summary or "—").replace("\n", "<br/>")
    story.append(_text_card("RESOLUTION SUMMARY", summary, body))
    story.append(Spacer(1, 4 * mm))

    # ---- Spare parts + service charge ----
    # Label + table + totals are bound together so the entire block moves as
    # one if it doesn't fit on the current page.
    story.extend(_invoice_block(ticket, body))

    # ---- Signatures ----
    story.append(Spacer(1, 6 * mm))
    sig_left = _signature_cell(
        "Customer",
        ticket.contact_name,
        resolution.customer_signed_at,
        cust_bytes,
        body,
    )
    # When the resolution was signed remotely, the second signature belongs to
    # the sub-engineer who collected it on-site — label and name it as such.
    if resolution.engineer_signer_name:
        sig_right = _signature_cell(
            "Sub-Engineer",
            resolution.engineer_signer_name,
            resolution.engineer_signed_at,
            engineer_bytes,
            body,
        )
    else:
        sig_right = _signature_cell(
            "Engineer",
            eng_name,
            resolution.engineer_signed_at,
            engineer_bytes,
            body,
        )
    sig_body = Table([[sig_left, sig_right]], colWidths=[CARD_WIDTH_HALF, CARD_WIDTH_HALF])
    sig_body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.5, CARD_BORDER),
        ("LINEAFTER", (0, 0), (0, 0), 0.3, ROW_RULE),
    ]))
    sig_card = Table(
        [[_card_header("SIGNATURES", width=CARD_WIDTH_FULL)], [sig_body]],
        colWidths=[CARD_WIDTH_FULL],
    )
    sig_card.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(KeepTogether([sig_card]))

    # ---- Footer ----
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "<font color='#737373' size='8.5'>This document is auto-generated by SK-POS Support upon mutual acknowledgement of resolution. "
        "It serves as a record of service completion.</font>",
        body,
    ))

    doc.build(story)
    return buffer.getvalue()


# ----------------------------- internals --------------------------------- #

def _fmt_inr(n: int) -> str:
    """Indian-style grouping (1,23,456). ReportLab doesn't speak Unicode rupees
    in the default Helvetica face, so we spell it 'INR' instead of using '₹'."""
    s = str(int(n))
    if len(s) <= 3:
        return f"INR {s}"
    head, last3 = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.append(head[-2:])
        head = head[:-2]
    if head:
        groups.append(head)
    grouped = ",".join(reversed(groups)) + "," + last3
    return f"INR {grouped}"


def _invoice_block(ticket: Ticket, body_style):
    """Return a list of Flowables for the invoice section.

    Layout: the section label, items table, totals rows, and warranty
    disclaimer are bound into one KeepTogether so the entire invoice moves
    as a unit to the next page if it doesn't fit on the current one.
    """
    charges = compute_charges(ticket)
    items = charges["items"]
    is_warranty = bool(charges["is_warranty"])

    col_widths = [86 * mm, 18 * mm, 30 * mm, 40 * mm]
    header_color = colors.HexColor("#737373")
    rule_color = colors.HexColor("#D4D4D4")
    header_bg = colors.HexColor("#F5F5F5")

    # ---- items table (allowed to split if there are many parts) ---------- #
    item_rows = [[
        Paragraph(f"<font color='{header_color.hexval()}'><b>Part / Service</b></font>", body_style),
        Paragraph(f"<font color='{header_color.hexval()}'><b>Qty</b></font>", body_style),
        Paragraph(f"<font color='{header_color.hexval()}'><b>Unit</b></font>", body_style),
        Paragraph(f"<font color='{header_color.hexval()}'><b>Amount</b></font>", body_style),
    ]]
    if items:
        for it in items:
            if is_warranty:
                line_total_html = (
                    f"<font color='#A3A3A3'><strike>{_fmt_inr(it['line_total_inr'])}</strike></font> "
                    f"<font color='#047857'>Free</font>"
                )
            else:
                line_total_html = _fmt_inr(it["line_total_inr"])
            item_rows.append([
                Paragraph(it["name"], body_style),
                Paragraph(str(it["quantity"]), body_style),
                Paragraph(_fmt_inr(it["unit_price_inr"]), body_style),
                Paragraph(line_total_html, body_style),
            ])
    else:
        item_rows.append([
            Paragraph("<font color='#A3A3A3'><i>No spare parts used.</i></font>", body_style),
            Paragraph("", body_style),
            Paragraph("", body_style),
            Paragraph("", body_style),
        ])

    items_tbl = Table(item_rows, colWidths=col_widths, repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, rule_color),
        # Side walls of the card
        ("LINEBEFORE", (0, 0), (0, -1), 0.5, rule_color),
        ("LINEAFTER", (-1, 0), (-1, -1), 0.5, rule_color),
    ]))

    # ---- totals table (must stay together) ------------------------------- #
    totals_rows = []
    if items:
        if is_warranty:
            subtotal_html = (
                f"<font color='#A3A3A3'><strike>{_fmt_inr(charges['spares_list_price_total_inr'])}</strike></font> "
                f"<font color='#047857'>Free (warranty)</font>"
            )
        else:
            subtotal_html = f"<b>{_fmt_inr(charges['spares_billable_total_inr'])}</b>"
        totals_rows.append([
            Paragraph("<font color='#737373'>Spares subtotal</font>", body_style),
            Paragraph("", body_style),
            Paragraph("", body_style),
            Paragraph(subtotal_html, body_style),
        ])
    if is_warranty:
        service_fee_html = (
            f"<font color='#A3A3A3'><strike>{_fmt_inr(charges['service_fee_inr'])}</strike></font> "
            f"<font color='#047857'>Free (warranty)</font>"
        )
    else:
        service_fee_html = _fmt_inr(charges["service_fee_inr"])
    totals_rows.append([
        Paragraph("Service fee", body_style),
        Paragraph("", body_style),
        Paragraph("", body_style),
        Paragraph(service_fee_html, body_style),
    ])
    totals_rows.append([
        Paragraph("<b>Total payable</b>", body_style),
        Paragraph("", body_style),
        Paragraph("", body_style),
        Paragraph(f"<b>{_fmt_inr(charges['grand_total_inr'])}</b>", body_style),
    ])

    totals_tbl = Table(totals_rows, colWidths=col_widths)
    totals_style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        # Faint separator between service fee and total payable
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#0A0A0A")),
        ("TOPPADDING", (0, -1), (-1, -1), 7),
        # Side + bottom walls — closes the card around items + totals
        ("LINEBEFORE", (0, 0), (0, -1), 0.5, rule_color),
        ("LINEAFTER", (-1, 0), (-1, -1), 0.5, rule_color),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, rule_color),
    ]
    # Top of the totals block — visual continuation of the items table
    totals_style.append(("LINEABOVE", (0, 0), (-1, 0), 0.3, rule_color))
    totals_tbl.setStyle(TableStyle(totals_style))

    totals_block = [totals_tbl]
    if is_warranty:
        totals_block.append(Spacer(1, 3 * mm))
        totals_block.append(Paragraph(
            "<font color='#737373' size='8.5'><i>This service is covered under warranty — "
            "spare parts and the service fee are both billed at zero. Reference prices "
            "shown for transparency.</i></font>",
            body_style,
        ))

    # Bind label + items + totals + disclaimer into one keep-together so the
    # invoice never gets split across pages. For typical ticket sizes
    # (<10 spares) the entire block stays on one page; if it genuinely
    # doesn't fit, ReportLab pushes the whole thing to the next page.
    header = _card_header("SPARE PARTS INFO")
    return [KeepTogether([header, items_tbl, *totals_block])]


CARD_BORDER = colors.HexColor("#D4D4D4")
CARD_HEADER_BG = colors.HexColor("#F5F5F5")
CARD_LABEL = colors.HexColor("#525252")
ROW_RULE = colors.HexColor("#EAEAEA")
KEY_COLOR = colors.HexColor("#737373")

CARD_WIDTH_HALF = 84 * mm
CARD_WIDTH_FULL = 174 * mm


def _card_header(label: str, width: float = CARD_WIDTH_FULL) -> Table:
    """A grey header bar with the section label — used at the top of every card."""
    label_style = ParagraphStyle(
        "card_header",
        fontName="Helvetica-Bold",
        fontSize=8.5,
        textColor=CARD_LABEL,
        leading=10,
    )
    t = Table(
        [[Paragraph(label, label_style)]],
        colWidths=[width],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_HEADER_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, CARD_BORDER),
    ]))
    return t


def _kv_rows(rows, body_style, key_width: float, value_width: float) -> Table:
    """A bordered key/value table with faint row separators."""
    data = [
        [
            Paragraph(f"<font color='{KEY_COLOR.hexval()}'>{k}</font>", body_style),
            Paragraph(v or "—", body_style),
        ]
        for k, v in rows
    ]
    t = Table(data, colWidths=[key_width, value_width])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, CARD_BORDER),
    ]
    # Faint horizontal rules between rows
    for i in range(1, len(data)):
        style.append(("LINEABOVE", (0, i), (-1, i), 0.3, ROW_RULE))
    t.setStyle(TableStyle(style))
    return t


def _full_card(label: str, rows, body_style) -> Table:
    """A full-width card with stacked key/value rows.

    Same look as `_card` but spans the page. Used for grouped sections
    (e.g. "Customer info" merging customer + address fields, or "Issue info"
    merging product + issue fields) so the layout breathes more for fields
    like multi-line addresses and free-text descriptions.
    """
    key_w = 38 * mm
    inner = [
        [_card_header(label, width=CARD_WIDTH_FULL)],
        [_kv_rows(rows, body_style, key_width=key_w, value_width=CARD_WIDTH_FULL - key_w)],
    ]
    outer = Table(inner, colWidths=[CARD_WIDTH_FULL])
    outer.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return outer


def _card(label: str, rows, body_style) -> Table:
    """A half-width card: header bar on top, key/value rows below.

    Both sub-tables sit inside a single 1-column outer Table so the card moves
    as a unit and renders with one continuous border.
    """
    inner = [
        [_card_header(label, width=CARD_WIDTH_HALF)],
        [_kv_rows(rows, body_style, key_width=28 * mm, value_width=CARD_WIDTH_HALF - 28 * mm)],
    ]
    outer = Table(inner, colWidths=[CARD_WIDTH_HALF])
    outer.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return outer


def _two_column_row(left: Table, right: Table) -> Table:
    """Place two cards side-by-side with a small gutter via an empty middle column."""
    gutter = 6 * mm
    t = Table(
        [[left, Spacer(1, 1), right]],
        colWidths=[CARD_WIDTH_HALF, gutter, CARD_WIDTH_HALF],
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _grid_card(label: str, pairs, body_style) -> Table:
    """A full-width card that lays out key/value pairs in a 2-column grid
    (two key/value pairs per row). Used for ASSIGNMENT info."""
    # Build rows of 4 cells each: [k1, v1, k2, v2].
    grid_rows = []
    for i in range(0, len(pairs), 2):
        left_k, left_v = pairs[i]
        right_k, right_v = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        grid_rows.append([
            Paragraph(f"<font color='{KEY_COLOR.hexval()}'>{left_k}</font>", body_style),
            Paragraph(left_v or "—", body_style),
            Paragraph(f"<font color='{KEY_COLOR.hexval()}'>{right_k}</font>", body_style) if right_k else Paragraph("", body_style),
            Paragraph(right_v or "—", body_style) if right_k else Paragraph("", body_style),
        ])

    col_w = CARD_WIDTH_FULL / 2
    key_w = 28 * mm
    val_w = col_w - key_w
    grid = Table(grid_rows, colWidths=[key_w, val_w, key_w, val_w])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, CARD_BORDER),
        # Faint vertical divider between the two column-pairs
        ("LINEAFTER", (1, 0), (1, -1), 0.3, ROW_RULE),
    ]
    for i in range(1, len(grid_rows)):
        style.append(("LINEABOVE", (0, i), (-1, i), 0.3, ROW_RULE))
    grid.setStyle(TableStyle(style))

    outer = Table([[_card_header(label, width=CARD_WIDTH_FULL)], [grid]], colWidths=[CARD_WIDTH_FULL])
    outer.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return outer


def _text_card(label: str, html: str, body_style) -> Table:
    """A full-width card holding a single paragraph of free-form text."""
    body_cell = Table([[Paragraph(html or "—", body_style)]], colWidths=[CARD_WIDTH_FULL])
    body_cell.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.5, CARD_BORDER),
    ]))
    outer = Table([[_card_header(label, width=CARD_WIDTH_FULL)], [body_cell]], colWidths=[CARD_WIDTH_FULL])
    outer.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return outer


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
            # Fit inside the half-width card cell (84mm - L/R padding)
            max_w = 65 * mm
            ratio = img.imageWidth / img.imageHeight if img.imageHeight else 1
            img.drawWidth = max_w
            img.drawHeight = max_w / ratio if ratio else 30 * mm
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

    inner = Table(rows, colWidths=[70 * mm])
    inner.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.5, colors.HexColor("#D4D4D4")),
        ("BOTTOMPADDING", (0, 0), (0, 0), 4),
        ("TOPPADDING", (0, 1), (0, 1), 6),
    ]))
    return inner
