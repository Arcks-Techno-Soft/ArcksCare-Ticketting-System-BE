"""Quotation PDF renderer (ReportLab) — plan §8.

The layout is a pixel-measured recreation of the Excel template behind the
three sample quotations. Every dimension below is written in *sample points*
(the samples are 522 × 756 pt pages with a 491 pt wide content box, measured
with PyMuPDF) and multiplied by `S` to land on A4. Keeping the numbers in
sample units means they can be checked directly against the measurements.

Structure (single Frame, A4):

    [logo] [tagline]
    [header box: Bill To | subject | meta]
    [items table: header row + per item (row A: boxed headline row,
                  row B: unboxed spec row with warranty box + photo)]
    KeepTogether[ totals + note | terms & conditions | bank + signature | footer ]

Rules: only three colours (black, #FF0000, #00B050); `[[…]]` in text prints
red; boxes are 1 pt, the outer frame and major boxes 1.5 pt. The outer frame
is drawn per page by the document template and hugs the content on the last
page. `Page x of y` prints bottom-right from page 2 on.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from ..schemas.quotation import NoteStyle, QuotationDraft, QuotationItemIn, RowStyle
from . import quotation_brand as brand
from .quotation_money import Totals, fmt_inr, fmt_qty, fmt_rate

# ----------------------------- scale & colours --------------------------- #

# sample-pt → A4-pt. 491 pt of sample content → ~550 pt on A4.
S = 1.11
PAGE_W, PAGE_H = A4
CONTENT_W = 491.0 * S
LEFT_MARGIN = (PAGE_W - CONTENT_W) / 2
TOP_MARGIN = 18.0
BOTTOM_MARGIN = 13.0

BLACK = colors.black
WHITE = colors.white
RED = colors.HexColor("#FF0000")
GREEN = colors.HexColor("#00B050")

THIN = 1.0
THICK = 1.5

# Bitter (the Lucida Fax stand-in) sets ~8% narrower than Lucida Fax Demibold
# at the same nominal size; nudge the serif sizes up so the text carries the
# same visual weight and wraps at roughly the same points as the samples.
SERIF_SCALE = 1.03

F_TITLE = "Q-Title"
F_SANS = "Q-Sans"
F_SERIF = "Q-Serif"
F_RUPEE = "Q-Rupee"

# ----------------------------- geometry (sample pt) ---------------------- #

# Item-table columns. With the Sl No. column the others give up 17 pt.
COLS_NO_SL = [("BRAND", 48.3), ("MODEL", 82.8), ("DESC", 158.6),
              ("PRICE", 90.5), ("QTY", 47.9), ("TOTAL", 62.9)]
COLS_SL = [("SL", 17.0), ("BRAND", 42.3), ("MODEL", 79.8), ("DESC", 153.6),
           ("PRICE", 87.5), ("QTY", 47.9), ("TOTAL", 62.9)]
COL_TITLES = {"SL": "Sl No.", "BRAND": "BRAND", "MODEL": "MODEL",
              "DESC": "PRODUCT NAME &amp; DESCRIPTION",
              "PRICE": "SPECIAL PRICE / UNIT", "QTY": "QUANTITY", "TOTAL": "TOTAL"}

HEADER_COLS = (129.8, 205.5, 155.7)        # Bill To | subject | meta
HEADER_MIN_H = 68.4
SUBJECT_ROW_H = 17.2

TABLE_HEADER_H = 15.8
ROW_A_MIN_H = 31.0
ROW_A_PAD = 7.0
# All-compact quotations (the CCTV variant) use the tight Excel row pitch.
COMPACT_ROW_MIN_H = 17.2
COMPACT_ROW_PAD = 4.0
WARRANTY_BOX_H = 13.3
IMG_MAX_W, IMG_MAX_H, IMG_MIN_H = 116.0, 100.0, 40.0
RUPEE_SUB_W = 14.0

NOTE_W = 289.8
TOTALS_COLS = (92.8, 62.9)
TOTALS_ROW_H = 17.5

TC_COLS = (48.3, 442.7)
TC_HEADER_H = 9.2

BANK_W, BANK_SIG_GAP, SIG_W = 289.7, 45.6, 155.7
BANK_KV_COLS = (58.0, 6.0, 146.0)
BAND_H = 10.0
BANK_ROW_H = 9.8

LOGO_W, LOGO_H = 168.4, 49.4


def _s(v: float) -> float:
    return v * S


# ----------------------------- text helpers ------------------------------ #

_RED_RE = re.compile(r"\[\[(.+?)\]\]", re.S)


def rich(text: str, red_size: Optional[float] = None) -> str:
    """Escape for Paragraph mini-HTML; `[[…]]` → red (optionally bigger);
    newlines → <br/>. This is the ONLY markup admins can use."""
    out: List[str] = []
    pos = 0
    for m in _RED_RE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        inner = escape(m.group(1))
        if red_size:
            out.append(f'<font color="#FF0000" size="{_s(red_size):.2f}">{inner}</font>')
        else:
            out.append(f'<font color="#FF0000">{inner}</font>')
        pos = m.end()
    out.append(escape(text[pos:]))
    return "".join(out).replace("\n", "<br/>")


def _is_all_red(line: str) -> bool:
    m = _RED_RE.fullmatch(line.strip())
    return m is not None


def _style(name: str, font: str, size: float, *, leading: Optional[float] = None,
           align=TA_LEFT, color=BLACK) -> ParagraphStyle:
    if font == F_SERIF:
        size *= SERIF_SCALE
        if leading is not None:
            leading *= SERIF_SCALE
    return ParagraphStyle(
        name,
        fontName=font,
        fontSize=_s(size),
        leading=_s(leading if leading is not None else size * 1.2),
        alignment=align,
        textColor=color,
        splitLongWords=True,
    )


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def _height(flowables: Sequence[Flowable], width: float) -> float:
    total = 0.0
    for f in flowables:
        _, h = f.wrap(width, 100000)
        total += h
    return total


# Styles (sample sizes; scaled in _style).
ST = {
    "tagline": _style("tagline", F_SERIF, 4.8, leading=5.6, align=TA_CENTER),
    "title": _style("title", F_TITLE, 11.4, leading=15.0),
    "billto": _style("billto", F_TITLE, 6.0, leading=7.6),
    "customer": _style("customer", F_SANS, 9.7, leading=11.0),
    "address": _style("address", F_SANS, 7.0, leading=8.5),
    "meta": _style("meta", F_SANS, 7.0, leading=8.7),
    "subject": _style("subject", F_SERIF, 9.7, leading=11.0),
    "th": _style("th", F_SERIF, 7.0, leading=8.2, align=TA_CENTER),
    "th_desc": _style("th_desc", F_SERIF, 7.6, leading=8.8, align=TA_CENTER),
    "th_sl": _style("th_sl", F_SERIF, 5.6, leading=6.4, align=TA_CENTER),
    "sl": _style("sl", F_SERIF, 6.5, leading=7.8, align=TA_CENTER),
    "brand": _style("brand", F_SERIF, 7.2, leading=8.6, align=TA_CENTER),
    "brand_sub": _style("brand_sub", F_SERIF, 5.4, leading=6.6, align=TA_CENTER),
    "model": _style("model", F_SERIF, 7.0, leading=8.4, align=TA_CENTER),
    "model_small": _style("model_small", F_SERIF, 6.5, leading=7.8, align=TA_CENTER),
    "model_big": _style("model_big", F_SERIF, 9.7, leading=11.2, align=TA_CENTER),
    "headline": _style("headline", F_SERIF, 7.0, leading=8.6, align=TA_CENTER),
    "spec": _style("spec", F_SERIF, 4.8, leading=6.0),
    "spec_red": _style("spec_red", F_SERIF, 7.0, leading=8.3, color=RED),
    "spec_mixed": _style("spec_mixed", F_SERIF, 4.8, leading=8.3),
    "warranty": _style("warranty", F_SERIF, 7.0, leading=8.4, align=TA_CENTER),
    "qty": _style("qty", F_SERIF, 6.5, leading=7.8, align=TA_CENTER),
    "amount": _style("amount", F_SERIF, 6.5, leading=7.8, align=TA_RIGHT),
    "rupee": _style("rupee", F_RUPEE, 6.5, leading=7.8, align=TA_LEFT),
    "totals_label": _style("totals_label", F_SERIF, 6.5, leading=7.8, align=TA_CENTER),
    "note_green": _style("note_green", F_SERIF, 7.6, leading=9.0, color=GREEN),
    "note_red": _style("note_red", F_SERIF, 6.7, leading=8.0, align=TA_CENTER, color=RED),
    "tc_head": _style("tc_head", F_SERIF, 6.5, leading=7.8),
    "tc_head_c": _style("tc_head_c", F_SERIF, 6.5, leading=7.8, align=TA_CENTER),
    "tc_num": _style("tc_num", F_SERIF, 6.0, leading=7.3, align=TA_CENTER),
    "tc_line": _style("tc_line", F_SERIF, 5.4, leading=6.4),
    "tc_line_red": _style("tc_line_red", F_SERIF, 7.0, leading=7.6, color=RED),
    "band": _style("band", F_SERIF, 6.5, leading=7.8, align=TA_CENTER, color=WHITE),
    "bank_kv": _style("bank_kv", F_SERIF, 6.0, leading=7.2),
    "sig_name": _style("sig_name", F_SERIF, 7.0, leading=8.4, align=TA_CENTER),
    "sig_small": _style("sig_small", F_SERIF, 5.4, leading=6.6, align=TA_CENTER),
    "website": _style("website", F_SANS, 9.7, leading=11.0, align=TA_CENTER),
    "office": _style("office", F_SANS, 9.7, leading=11.0, align=TA_CENTER),
    "pageno": _style("pageno", F_SANS, 7.0, leading=8.0, align=TA_RIGHT),
}

_NOPAD = [
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 0),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
]


# ----------------------------- render model ------------------------------ #

@dataclass
class RenderModel:
    draft: QuotationDraft
    totals: Totals
    signatory: brand.Signatory
    item_images: List[Optional[bytes]]  # parallel to draft.items (None = no photo)


# ----------------------------- images ------------------------------------ #

def _fit(natural_w: float, natural_h: float, max_w: float, max_h: float) -> Tuple[float, float]:
    scale = min(max_w / natural_w, max_h / natural_h)
    return natural_w * scale, natural_h * scale


# Longest side (px) an embedded picture is downscaled to. Product photos
# extracted from the samples are 500–1800 px; at ≤ 130 pt on paper anything
# above ~800 px only inflates the PDF (2.8 MB → ~300 KB).
IMG_MAX_PX = 800


@lru_cache(maxsize=32)
def _prepared_asset(path: str) -> bytes:
    return _prepare_image_bytes(Path(path).read_bytes())


def _prepare_image_bytes(data: bytes) -> bytes:
    """Re-encode through Pillow: strips EXIF/polyglot payloads, downscales to
    IMG_MAX_PX, keeps alpha as PNG and flattens everything else to JPEG."""
    with PILImage.open(io.BytesIO(data)) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        limit = 500 if has_alpha else IMG_MAX_PX  # the stamp prints at ~50 pt
        if max(im.size) > limit:
            im.thumbnail((limit, limit), PILImage.LANCZOS)
        out = io.BytesIO()
        if has_alpha:
            im.convert("RGBA").save(out, format="PNG", optimize=True)
        else:
            im.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()


def _image(src, max_w: float, max_h: float, *, mask="auto") -> Image:
    """Image flowable fitted (aspect preserved) inside max_w × max_h sample pt.
    `src` is a bundled asset path (str/Path) or raw bytes."""
    if isinstance(src, (str, Path)):
        data = _prepared_asset(str(src))
    else:
        data = _prepare_image_bytes(bytes(src))
    nat_w, nat_h = ImageReader(io.BytesIO(data)).getSize()
    w, h = _fit(nat_w, nat_h, _s(max_w), _s(max_h))
    return Image(io.BytesIO(data), width=w, height=h, mask=mask)


# ----------------------------- rupee cell -------------------------------- #

def _rupee_cell(amount_text: str, col_w: float, style_amount=None) -> Table:
    """`[₹ | amount]` — ₹ left-aligned in a narrow sub-column, amount right."""
    sub = _s(RUPEE_SUB_W)
    t = Table(
        [[_para("₹", ST["rupee"]), _para(amount_text, style_amount or ST["amount"])]],
        colWidths=[sub, _s(col_w) - sub],
    )
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), _s(5.0)),
        ("RIGHTPADDING", (1, 0), (1, 0), _s(0.8)),
    ]))
    return t


# ----------------------------- header ------------------------------------ #

def _bill_to_block(d: QuotationDraft) -> List[Flowable]:
    out = [
        _para("<u>QUOTATION</u>", ST["title"]),
        _para("Bill To,", ST["billto"]),
        _para(escape(d.customer_name), ST["customer"]),
    ]
    for line in d.address_lines:
        out.append(_para(escape(line), ST["address"]))
    if d.customer_gstin:
        out.append(_para(f"GSTIN: {escape(d.customer_gstin)}", ST["address"]))
    elif d.customer_pan:
        out.append(_para(f"PAN : {escape(d.customer_pan)}", ST["address"]))
    return out


def _meta_table(d: QuotationDraft, sig: brand.Signatory) -> Table:
    rows = [
        ("GSTIN", brand.COMPANY_GSTIN),
        ("Reference No", d.reference or "—"),
        ("Date", d.quotation_date.strftime("%d/%m/%Y")),
        ("Email", sig.email),
        ("Mobile", sig.mobile),
        ("WhatsApp No.", brand.COMPANY_WHATSAPP),
        ("Website", brand.COMPANY_WEBSITE),
    ]
    data = [[_para(escape(k), ST["meta"]), _para(":", ST["meta"]), _para(escape(v), ST["meta"])]
            for k, v in rows]
    key_w, colon_w = _s(62.0), _s(5.0)
    t = Table(data, colWidths=[key_w, colon_w, _s(HEADER_COLS[2]) - key_w - colon_w],
              rowHeights=[_s(8.7)] * len(rows))
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), _s(1.5)),
        ("LEFTPADDING", (2, 0), (2, -1), _s(1.0)),
    ]))
    return t


def _header_block(d: QuotationDraft, sig: brand.Signatory) -> Table:
    bill = _bill_to_block(d)
    bill_h = _height(bill, _s(HEADER_COLS[0]) - _s(2.0)) + _s(1.5)
    meta = _meta_table(d, sig)
    meta_h = _s(8.7) * 7 + _s(1.0)
    total_h = max(_s(HEADER_MIN_H), bill_h, meta_h)
    subj_h = _s(SUBJECT_ROW_H)
    subject = _para(escape(d.subject_line), ST["subject"]) if d.subject_line else ""

    data = [[bill, "", meta], ["", subject, ""]]
    t = Table(data, colWidths=[_s(w) for w in HEADER_COLS],
              rowHeights=[total_h - subj_h, subj_h])
    cmds = _NOPAD + [
        ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (2, 0), (2, 1)),
        ("VALIGN", (0, 0), (0, 1), "TOP"),
        ("VALIGN", (2, 0), (2, 1), "TOP"),
        ("VALIGN", (1, 1), (1, 1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 1), _s(1.2)),
        ("TOPPADDING", (0, 0), (0, 1), _s(0.5)),
        ("TOPPADDING", (2, 0), (2, 1), _s(1.0)),
        ("LEFTPADDING", (1, 1), (1, 1), _s(2.0)),
        ("BOX", (0, 0), (-1, -1), THICK, BLACK),
        ("LINEAFTER", (0, 0), (0, 1), THIN, BLACK),
        ("LINEAFTER", (1, 0), (1, 1), THIN, BLACK),
    ]
    if d.subject_line:
        cmds.append(("LINEABOVE", (1, 1), (1, 1), THIN, BLACK))
    t.setStyle(TableStyle(cmds))
    return t


# ----------------------------- items table ------------------------------- #

def _model_flowables(model: Optional[str]) -> List[Flowable]:
    if not model:
        return []
    lines = [l for l in model.split("\n") if l.strip()]
    if len(lines) <= 1:
        return [_para(rich(lines[0]), ST["model"])] if lines else []
    out = [_para(rich(lines[0]), ST["model_small"])]
    out += [_para(rich(l), ST["model_big"]) for l in lines[1:]]
    return out


def _brand_flowables(item: QuotationItemIn) -> List[Flowable]:
    if not item.brand:
        return []
    out = [_para(rich(item.brand), ST["brand"])]
    if item.brand_sub_label:
        out.append(_para(rich(item.brand_sub_label), ST["brand_sub"]))
    return out


def _spec_flowables(spec: Optional[str]) -> List[Flowable]:
    """One Paragraph per spec line so each line gets its own size/leading:
    black lines are small (4.8), all-red lines are big (7.0), mixed lines keep
    the small base with big red spans."""
    if not spec:
        return []
    out: List[Flowable] = []
    for line in spec.split("\n"):
        if not line.strip():
            continue
        if _is_all_red(line):
            out.append(_para(rich(line), ST["spec_red"]))
        elif "[[" in line:
            out.append(_para(rich(line, red_size=7.0), ST["spec_mixed"]))
        else:
            out.append(_para(rich(line), ST["spec"]))
    return out


def _warranty_box(label: str, width: float, height: float) -> Table:
    t = Table([[_para(rich(label), ST["warranty"])]], colWidths=[width], rowHeights=[height])
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), THIN, BLACK),
    ]))
    return t


@dataclass
class _ItemBlock:
    """The 1–2 table rows of one item; style row indices are block-relative."""
    rows: List[list]
    heights: List[float]
    cmds: List[tuple]

    @property
    def height(self) -> float:
        return sum(self.heights)


def _shift(cmds: Sequence[tuple], off: int) -> List[tuple]:
    out = []
    for c in cmds:
        name, (c0, r0), (c1, r1), *rest = c
        out.append((name, (c0, r0 + off), (c1, r1 + off), *rest))
    return out


def _item_block(item: QuotationItemIn, n: int, img_bytes: Optional[bytes], line_total,
                cols, widths: List[float], ix: Dict[str, int], compact_layout: bool) -> _ItemBlock:
    ncol = len(cols)
    has_sl = "SL" in ix
    row_min_h, row_pad = (COMPACT_ROW_MIN_H, COMPACT_ROW_PAD) if compact_layout else (ROW_A_MIN_H, ROW_A_PAD)
    rows: List[list] = []
    heights: List[float] = []
    cmds: List[tuple] = []

    # ---------------- row A: boxed headline row ----------------
    r = 0
    cells: List = [""] * ncol
    if has_sl:
        cells[ix["SL"]] = _para(str(n), ST["sl"])
    brand_fl = _brand_flowables(item)
    model_fl = _model_flowables(item.model)
    headline = _para(rich(item.headline, red_size=7.6), ST["headline"])

    span_from = None
    if not brand_fl and not model_fl:
        span_from = "BRAND"
    elif not model_fl:
        span_from = "MODEL"

    if span_from:
        c0, c1 = ix[span_from], ix["DESC"]
        cells[c0] = headline
        if brand_fl and span_from == "MODEL":
            cells[ix["BRAND"]] = brand_fl
        cmds.append(("SPAN", (c0, r), (c1, r)))
        desc_w = sum(widths[c0:c1 + 1]) - _s(4.0)
    else:
        cells[ix["BRAND"]] = brand_fl
        cells[ix["MODEL"]] = model_fl
        cells[ix["DESC"]] = headline
        desc_w = widths[ix["DESC"]] - _s(4.0)

    cells[ix["PRICE"]] = _rupee_cell(fmt_inr(item.unit_price), cols[ix["PRICE"]][1])
    cells[ix["QTY"]] = _para(fmt_qty(item.quantity), ST["qty"])
    cells[ix["TOTAL"]] = _rupee_cell(fmt_inr(line_total), cols[ix["TOTAL"]][1])

    content_h = max(
        _height([headline], desc_w),
        _height(brand_fl, widths[ix["BRAND"]] - _s(4.0)) if brand_fl else 0,
        _height(model_fl, widths[ix["MODEL"]] - _s(4.0)) if model_fl else 0,
    )
    rows.append(cells)
    heights.append(max(_s(row_min_h), content_h + 2 * _s(row_pad)))
    cmds.append(("BOX", (0, r), (-1, r), THIN, BLACK))
    for k in ("PRICE", "TOTAL"):
        cmds.append(("LEFTPADDING", (ix[k], r), (ix[k], r), 0))
        cmds.append(("RIGHTPADDING", (ix[k], r), (ix[k], r), 0))

    # ---------------- row B: spec block (detailed rows only) ----------------
    show_img = bool(item.include_image and img_bytes)
    if item.row_style != RowStyle.DETAILED or not (item.spec_lines or item.warranty_label or show_img):
        return _ItemBlock(rows, heights, cmds)

    r = 1
    cells = [""] * ncol
    spec_fl = _spec_flowables(item.spec_lines)
    spec_w = widths[ix["DESC"]] - _s(1.0)
    spec_h = _height(spec_fl, spec_w) if spec_fl else 0.0

    # Warranty label box spans (SL+)BRAND+MODEL, full width of the span.
    wb_c0, wb_c1 = 0, ix["MODEL"]
    wb_w = sum(widths[wb_c0:wb_c1 + 1])
    if item.warranty_label:
        cells[wb_c0] = _warranty_box(item.warranty_label, wb_w, _s(WARRANTY_BOX_H))
    cmds.append(("SPAN", (wb_c0, r), (wb_c1, r)))
    cmds.append(("LEFTPADDING", (wb_c0, r), (wb_c0, r), 0))
    cmds.append(("RIGHTPADDING", (wb_c0, r), (wb_c0, r), 0))

    cells[ix["DESC"]] = spec_fl
    cmds.append(("VALIGN", (ix["DESC"], r), (ix["DESC"], r), "TOP"))
    cmds.append(("LEFTPADDING", (ix["DESC"], r), (ix["DESC"], r), _s(0.5)))
    cmds.append(("RIGHTPADDING", (ix["DESC"], r), (ix["DESC"], r), _s(0.5)))
    cmds.append(("TOPPADDING", (ix["DESC"], r), (ix["DESC"], r), _s(2.0)))

    img_h = 0.0
    ic0, ic1 = ix["PRICE"], ix["TOTAL"]
    cmds.append(("SPAN", (ic0, r), (ic1, r)))
    if show_img:
        # Photo height follows the spec block so a short spec gets a small
        # photo (as in the sample), capped at IMG_MAX_H.
        max_h = IMG_MAX_H if not spec_fl else max(IMG_MIN_H, min(IMG_MAX_H, spec_h / S + 4.0))
        img = _image(img_bytes, IMG_MAX_W, max_h)
        img.hAlign = "CENTER"
        cells[ic0] = img
        img_h = img.drawHeight
        cmds.append(("ALIGN", (ic0, r), (ic0, r), "CENTER"))
        cmds.append(("LEFTPADDING", (ic0, r), (ic0, r), _s(30.0)))

    rows.append(cells)
    heights.append(max(
        spec_h + _s(4.0),
        img_h + _s(8.0),
        (_s(WARRANTY_BOX_H) + _s(8.0)) if item.warranty_label else 0,
    ))
    return _ItemBlock(rows, heights, cmds)


def _items_flowables(m: RenderModel, first_avail: float, later_avail: float) -> List[Flowable]:
    """The item table(s). Pagination is done here rather than by ReportLab's
    row splitter so an item's headline row and its spec row are never split
    across pages: one Table per page, each starting with the header row."""
    d = m.draft
    cols = COLS_SL if d.sl_no_visible else COLS_NO_SL
    keys = [k for k, _ in cols]
    widths = [_s(w) for _, w in cols]
    ix = {k: i for i, k in enumerate(keys)}
    compact_layout = all(i.row_style == RowStyle.COMPACT for i in d.items)

    header = []
    for k in keys:
        st = ST["th_desc"] if k == "DESC" else ST["th_sl"] if k == "SL" else ST["th"]
        header.append(_para(COL_TITLES[k], st))
    header_h = _s(TABLE_HEADER_H)
    base_cmds = _NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), _s(2.0)),
        ("RIGHTPADDING", (0, 0), (-1, -1), _s(2.0)),
        ("GRID", (0, 0), (-1, 0), THIN, BLACK),
        ("LINEABOVE", (0, 0), (-1, 0), THICK, BLACK),
    ]

    blocks = [
        _item_block(item, n, img, lt, cols, widths, ix, compact_layout)
        for n, (item, img, lt) in enumerate(zip(d.items, m.item_images, m.totals.line_totals), start=1)
    ]

    # Greedy pagination on the exact row heights.
    pages: List[List[_ItemBlock]] = [[]]
    remaining = first_avail - header_h
    for b in blocks:
        if pages[-1] and b.height > remaining:
            pages.append([])
            remaining = later_avail - header_h
        pages[-1].append(b)
        remaining -= b.height

    out: List[Flowable] = []
    for pi, page_blocks in enumerate(pages):
        rows: List[list] = [header]
        heights: List[float] = [header_h]
        cmds = list(base_cmds)
        for b in page_blocks:
            cmds += _shift(b.cmds, len(rows))
            rows += b.rows
            heights += b.heights
        # repeatRows=1 still covers the rare block taller than a whole page.
        t = Table(rows, colWidths=widths, rowHeights=heights, repeatRows=1)
        t.setStyle(TableStyle(cmds))
        if pi > 0:
            out.append(PageBreak())
        out.append(t)
    return out


# ----------------------------- totals + note ----------------------------- #

def _totals_table(m: RenderModel) -> Table:
    d = m.draft
    sub_l, gst_l, grand_l = brand.TOTALS_LABELS[d.totals_label_set.value]
    gst_l = gst_l.format(rate=fmt_rate(d.gst_rate))
    aw = TOTALS_COLS[1]
    data = [
        [_para(escape(sub_l), ST["totals_label"]), _rupee_cell(fmt_inr(m.totals.subtotal), aw)],
        [_para(escape(gst_l), ST["totals_label"]), _rupee_cell(fmt_inr(m.totals.gst_amount), aw)],
        [_para(escape(grand_l), ST["totals_label"]), _rupee_cell(fmt_inr(m.totals.grand_total), aw)],
    ]
    t = Table(data, colWidths=[_s(w) for w in TOTALS_COLS], rowHeights=[_s(TOTALS_ROW_H)] * 3)
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), THICK, BLACK),
        ("INNERGRID", (0, 0), (-1, -1), THIN, BLACK),
    ]))
    return t


def _note_cell(d: QuotationDraft) -> Flowable:
    if not d.note_text:
        return ""
    if d.note_style == NoteStyle.GREEN_ON_BLACK:
        rh = _s(TOTALS_ROW_H)
        t = Table([[""], [_para(rich(d.note_text), ST["note_green"])], [""]],
                  colWidths=[_s(NOTE_W)], rowHeights=[rh, rh, rh])
        t.setStyle(TableStyle(_NOPAD + [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 1), (0, 1), BLACK),
            ("LEFTPADDING", (0, 1), (0, 1), _s(1.5)),
        ]))
        return t
    # RED_TEXT: a boxed cell two totals-rows tall (as in the CCTV sample).
    t = Table([[_para(rich(d.note_text), ST["note_red"])]],
              colWidths=[_s(NOTE_W)], rowHeights=[_s(TOTALS_ROW_H) * 2])
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), _s(3.0)),
        ("RIGHTPADDING", (0, 0), (-1, -1), _s(3.0)),
        ("BOX", (0, 0), (-1, -1), THIN, BLACK),
    ]))
    return t


def _totals_block(m: RenderModel) -> Table:
    gap = CONTENT_W - _s(NOTE_W) - _s(sum(TOTALS_COLS))
    t = Table([[_note_cell(m.draft), "", _totals_table(m)]],
              colWidths=[_s(NOTE_W), gap, _s(sum(TOTALS_COLS))])
    t.setStyle(TableStyle(_NOPAD + [("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


# ----------------------------- terms & conditions ------------------------ #

def _terms_table(d: QuotationDraft) -> Table:
    data = [[_para("<u>Sl No.</u>", ST["tc_head_c"]), _para("<u>Terms &amp; Conditions</u>", ST["tc_head"])]]
    for i, line in enumerate(d.terms or []):
        style = ST["tc_line_red"] if i == brand.RED_TERM_INDEX else ST["tc_line"]
        data.append([_para(str(i + 1), ST["tc_num"]), _para(rich(line), style)])
    t = Table(data, colWidths=[_s(w) for w in TC_COLS],
              rowHeights=[_s(TC_HEADER_H)] + [None] * (len(data) - 1))
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), THICK, BLACK),
        ("LEFTPADDING", (1, 0), (1, -1), _s(1.4)),
        ("RIGHTPADDING", (1, 0), (1, -1), _s(1.0)),
        ("TOPPADDING", (0, 1), (-1, -1), _s(0.2)),
        ("BOTTOMPADDING", (0, 1), (-1, -1), _s(0.2)),
        ("BOTTOMPADDING", (0, -1), (-1, -1), _s(1.5)),
        ("TOPPADDING", (0, 0), (-1, 0), _s(1.0)),
    ]))
    return t


# ----------------------------- bank + signature -------------------------- #

def _bank_box() -> Table:
    kv = [[_para(escape(k), ST["bank_kv"]), _para(":", ST["bank_kv"]), _para(escape(v), ST["bank_kv"])]
          for k, v in brand.BANK_DETAILS]
    kv_t = Table(kv, colWidths=[_s(w) for w in BANK_KV_COLS], rowHeights=[_s(BANK_ROW_H)] * len(kv))
    kv_t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), _s(1.2)),
        ("LEFTPADDING", (2, 0), (2, -1), _s(2.0)),
    ]))
    qr = _image(str(brand.QR_SCAN_PAY), 70.0, 56.0)
    kv_w = _s(sum(BANK_KV_COLS))
    body_h = _s(BANK_ROW_H) * len(kv) + _s(1.5)
    t = Table([[_para(brand.BANK_BAND_TEXT, ST["band"]), ""], [kv_t, qr]],
              colWidths=[kv_w, _s(BANK_W) - kv_w], rowHeights=[_s(BAND_H), body_h])
    t.setStyle(TableStyle(_NOPAD + [
        ("SPAN", (0, 0), (1, 0)),
        ("BACKGROUND", (0, 0), (1, 0), BLACK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (1, 1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), THIN, BLACK),
    ]))
    return t


def _signature_box(sig: brand.Signatory) -> Table:
    stamp = _image(str(brand.STAMP_SIGNATURE), 44.0, 37.0)
    stamp.hAlign = "CENTER"
    data = [
        [_para(escape(brand.SIGNATURE_BAND_TEXT), ST["band"])],
        [stamp],
        [_para(escape(sig.name), ST["sig_name"])],
        [_para(escape(sig.designation), ST["sig_small"])],
        [_para(escape(sig.phones), ST["sig_small"])],
    ]
    t = Table(data, colWidths=[_s(SIG_W)],
              rowHeights=[_s(BAND_H), _s(38.5), _s(9.0), _s(7.0), _s(7.0)])
    t.setStyle(TableStyle(_NOPAD + [
        ("BACKGROUND", (0, 0), (0, 0), BLACK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (0, 1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), THIN, BLACK),
    ]))
    return t


def _bank_signature_block(sig: brand.Signatory) -> Table:
    t = Table([[_bank_box(), "", _signature_box(sig)]],
              colWidths=[_s(BANK_W), _s(BANK_SIG_GAP), _s(SIG_W)])
    t.setStyle(TableStyle(_NOPAD + [("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _footer_block() -> Table:
    t = Table(
        [[_para(f"<u>{brand.COMPANY_WEBSITE}</u>", ST["website"])],
         [_para(escape(brand.COMPANY_OFFICE_LINE), ST["office"])]],
        colWidths=[CONTENT_W], rowHeights=[_s(10.5), _s(12.0)],
    )
    t.setStyle(TableStyle(_NOPAD + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, 1), (0, 1), THICK, BLACK),
        ("LINEBELOW", (0, 1), (0, 1), THICK, BLACK),
    ]))
    return t


# ----------------------------- document template ------------------------- #

class _NumberedCanvas(rl_canvas.Canvas):
    """Draws `Page x of y` (bottom-right) on every page after the first."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states: List[dict] = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            if total > 1 and self._pageNumber > 1:
                self.setFont(F_SANS, _s(7.0))
                self.drawRightString(
                    LEFT_MARGIN + CONTENT_W, BOTTOM_MARGIN - _s(9.0),
                    f"Page {self._pageNumber} of {total}",
                )
            super().showPage()
        super().save()


class _QuotationDoc(BaseDocTemplate):
    """Single-frame A4 template that draws the 1.5 pt outer frame per page.

    On a full page the frame runs to the bottom margin; on the last page it
    stops at the bottom of the footer row, like the Excel print area.
    """

    def __init__(self, buf, *, title: str):
        super().__init__(
            buf, pagesize=A4,
            leftMargin=LEFT_MARGIN, rightMargin=LEFT_MARGIN,
            topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
            title=title, author=brand.COMPANY_NAME, subject="Quotation",
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height,
                      id="main", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="main", frames=[frame])])
        self._pending: list = []

    def build(self, flowables, **kwargs):  # type: ignore[override]
        self._pending = flowables
        super().build(flowables, canvasmaker=_NumberedCanvas)

    def handle_pageEnd(self):
        self._draw_frame()
        super().handle_pageEnd()

    def _draw_frame(self):
        f = self.frame
        if f is None:
            return
        top = f._y2 - f._topPadding
        last_page = not self._pending
        bottom = f._y if last_page else f._y1p
        if bottom >= top:
            return
        c = self.canv
        c.saveState()
        c.setStrokeColor(BLACK)
        c.setLineWidth(THICK)
        c.rect(self.leftMargin, bottom, self.width, top - bottom, stroke=1, fill=0)
        c.restoreState()


# ----------------------------- entry point ------------------------------- #

def render_quotation_pdf(m: RenderModel) -> bytes:
    """Render the quotation and return the PDF bytes (caller stores/streams)."""
    brand.register_fonts()
    d = m.draft

    logo = _image(str(brand.LOGO_BANNER), LOGO_W, LOGO_H)
    logo.hAlign = "CENTER"

    top: List[Flowable] = [
        Spacer(1, _s(1.4)),
        logo,
        Spacer(1, _s(1.0)),
        _para(escape(brand.COMPANY_TAGLINE), ST["tagline"]),
        _header_block(d, m.signatory),
    ]
    frame_h = PAGE_H - TOP_MARGIN - BOTTOM_MARGIN
    first_avail = frame_h - _height(top, CONTENT_W) - 0.5
    story: List[Flowable] = top + _items_flowables(m, first_avail, frame_h - 0.5) + [
        KeepTogether([
            Spacer(1, _s(3.0)),
            _totals_block(m),
            _terms_table(d),
            Spacer(1, _s(2.0)),
            _bank_signature_block(m.signatory),
            _footer_block(),
        ]),
    ]

    buf = io.BytesIO()
    title = f"Quotation {d.reference}" if d.reference else "Quotation"
    doc = _QuotationDoc(buf, title=title)
    doc.build(story)
    return buf.getvalue()


def render_sample(name: str = "navapakam") -> bytes:
    """Render a golden fixture (plan §12) — the visual regression check."""
    from .quotation_service import build_render_model_from_dict
    from .quotation_fixtures import FIXTURES

    return render_quotation_pdf(build_render_model_from_dict(FIXTURES[name]))
