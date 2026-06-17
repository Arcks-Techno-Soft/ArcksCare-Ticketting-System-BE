"""Installation completion PDF — a slim cousin of resolution PDF.

No spares / invoice / warranty — just the basic install info, work notes, and
both signatures. Reuses the card/header helpers from pdf_generator to keep
the visual style identical.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..models.installation import Installation, InstallationResolution
from .pdf_generator import (
    CARD_BORDER,
    CARD_WIDTH_FULL,
    CARD_WIDTH_HALF,
    ROW_RULE,
    _card,
    _card_header,
    _customer_photo_card,
    _fetch_signature,
    _fmt_dt,
    _grid_card,
    _read_local_signature,
    _signature_cell,
    _text_card,
    _two_column_row,
)

logger = logging.getLogger("skposcare.installation_pdf")


def generate_installation_pdf(
    installation: Installation,
    resolution: InstallationResolution,
) -> bytes:
    from .storage import get_storage  # local import to avoid cycle

    storage = get_storage()
    cust_bytes = engineer_bytes = None
    if resolution.customer_signature_storage_key:
        cust_bytes = _read_local_signature(resolution.customer_signature_storage_key)
        if cust_bytes is None:
            cust_bytes = _fetch_signature(
                storage.public_url(resolution.customer_signature_storage_key)
            )
    if resolution.engineer_signature_storage_key:
        engineer_bytes = _read_local_signature(resolution.engineer_signature_storage_key)
        if engineer_bytes is None:
            engineer_bytes = _fetch_signature(
                storage.public_url(resolution.engineer_signature_storage_key)
            )

    # Optional customer photo captured at sign-off.
    photo_bytes = None
    if resolution.customer_photo_storage_key:
        photo_bytes = _read_local_signature(resolution.customer_photo_storage_key)
        if photo_bytes is None:
            photo_bytes = _fetch_signature(
                storage.public_url(resolution.customer_photo_storage_key)
            )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"SK-POS Support Installation — {installation.reference}",
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

    story.append(Paragraph("SK-POS Support Installation Record", h_style))
    story.append(Paragraph(
        f"Installation {installation.reference} &middot; "
        f"Generated {_fmt_dt(datetime.now(timezone.utc))}",
        sub_style,
    ))

    eng_name = installation.assigned_engineer.name if installation.assigned_engineer else "—"

    customer_card = _card("CUSTOMER", [
        ("Business", f"{installation.business_name} ({installation.business_category})"),
        ("Contact", installation.contact_name),
        ("Phone", installation.phone),
        ("Email", installation.email or "—"),
    ], body)

    invoice_card = _card("INVOICE", [
        ("Invoice number", installation.invoice_number),
        ("Reference", installation.reference),
        ("Created", _fmt_dt(installation.created_at)),
    ], body)
    story.append(_two_column_row(customer_card, invoice_card))
    story.append(Spacer(1, 4 * mm))

    story.append(_grid_card("ASSIGNMENT", [
        ("Engineer", eng_name),
        ("Assigned", _fmt_dt(installation.assigned_at)),
        ("Completed", _fmt_dt(installation.completed_at)),
        ("Closed", _fmt_dt(installation.closed_at)),
    ], body))
    story.append(Spacer(1, 4 * mm))

    # ---- Work notes summary ----
    note_lines = []
    for n in installation.notes:
        when = _fmt_dt(n.created_at)
        author = n.author.name if n.author else "—"
        note_lines.append(
            f"<b>{author}</b> &middot; "
            f"<font color='#737373' size='8.5'>{when}</font><br/>"
            f"{(n.body or '').replace(chr(10), '<br/>')}"
        )
    notes_html = "<br/><br/>".join(note_lines) if note_lines else "—"
    story.append(_text_card("WORK NOTES", notes_html, body))
    story.append(Spacer(1, 6 * mm))

    # ---- Signatures ----
    sig_left = _signature_cell(
        "Customer",
        installation.contact_name,
        resolution.customer_signed_at,
        cust_bytes,
        body,
    )
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

    # ---- Customer photo (optional) ----
    photo_card = _customer_photo_card(photo_bytes, resolution.customer_photo_captured_at, body)
    if photo_card is not None:
        story.append(Spacer(1, 6 * mm))
        story.append(KeepTogether([photo_card]))

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "<font color='#737373' size='8.5'>This document is auto-generated by SK-POS Support "
        "upon mutual acknowledgement of installation completion.</font>",
        body,
    ))

    doc.build(story)
    return buffer.getvalue()
