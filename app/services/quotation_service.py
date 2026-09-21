"""Quotation business logic: totals, signatory + image resolution, reference
numbering and the issue transaction (plan §5.3, §6).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import HTTPException, status
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import MIGRATION_SCHEMA, qualify
from ..models.quotation import Quotation, QuotationItem, QuotationSequence
from ..models.user import User
from ..schemas.quotation import (
    QuotationDraft,
    QuotationItemIn,
    QuotationItemOut,
    QuotationOut,
    QuotationSummaryOut,
)
from . import quotation_brand as brand
from .quotation_money import compute_totals
from .quotation_pdf import RenderModel, render_quotation_pdf

logger = logging.getLogger("skposcare.quotation")


# ----------------------------- column migration -------------------------- #

def ensure_quotation_item_columns(engine: Engine) -> None:
    """Add quotation_items.image_asset if the table pre-dates it. `create_all`
    never adds columns to existing tables; scoped to DB_SCHEMA like the other
    ensure_* migrations so a test backend can't touch production."""
    insp = inspect(engine)
    if "quotation_items" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # fresh DB — create_all includes the column
    existing = {c["name"] for c in insp.get_columns("quotation_items", schema=MIGRATION_SCHEMA)}
    if "image_asset" in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {qualify('quotation_items')} ADD COLUMN image_asset VARCHAR(120)"))
        logger.info("Added quotation_items.image_asset column")



def ensure_quotation_edit_columns(engine: Engine) -> None:
    """Add quotations.updated_at / updated_by_id if the table pre-dates edits.

    Same shape as the other ensure_* migrations: scoped to DB_SCHEMA so a test
    backend can't touch production, and a no-op once the columns exist.
    """
    insp = inspect(engine)
    if "quotations" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # fresh DB — create_all includes them
    existing = {c["name"] for c in insp.get_columns("quotations", schema=MIGRATION_SCHEMA)}
    stamp = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "TIMESTAMP"
    pending = [("updated_at", f"{stamp} NULL"), ("updated_by_id", "INTEGER NULL")]
    with engine.begin() as conn:
        for name, ddl in pending:
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE {qualify('quotations')} ADD COLUMN {name} {ddl}"))
            logger.info("Added quotations.%s column", name)

# ----------------------------- image resolution -------------------------- #

def read_storage_bytes(storage_key: str) -> Optional[bytes]:
    """Fetch an object previously saved through `services.storage`.

    The storage backends expose `public_url()` only (no read API), so we read
    local files straight off disk and HTTP-fetch presigned/public URLs —
    the same approach `pdf_generator` uses for signature images.
    """
    from .storage import get_storage

    try:
        url = get_storage().public_url(storage_key)
    except Exception:
        logger.exception("public_url failed for %s", storage_key)
        return None
    if url.startswith("http"):
        try:
            r = httpx.get(url, timeout=20.0)
            r.raise_for_status()
            return r.content
        except Exception:
            logger.exception("Failed to fetch stored object %s", storage_key)
            return None
    if url.startswith("/uploads/"):
        p = Path(get_settings().local_upload_dir) / url[len("/uploads/"):]
        if p.is_file():
            return p.read_bytes()
    return None


def _image_bytes(storage_key: Optional[str], image_asset: Optional[str]) -> Optional[bytes]:
    if image_asset:
        path = brand.bundled_product_images().get(image_asset)
        if path is not None:
            return path.read_bytes()
    if storage_key:
        return read_storage_bytes(storage_key)
    return None


def load_item_image(item: QuotationItemIn, db: Optional[Session] = None) -> Optional[bytes]:
    """Bytes of the photo to print for an item, or None. A row with neither
    an asset nor a storage key falls back to its catalogue product's picture."""
    if not item.include_image:
        return None
    data = _image_bytes(item.image_storage_key, item.image_asset)
    if data is None and item.product_id is not None and db is not None:
        from .quotation_catalogue import product_image_source

        key, asset = product_image_source(db, item.product_id)
        data = _image_bytes(key, asset)
    return data


# ----------------------------- render model ------------------------------ #

def build_render_model(draft: QuotationDraft, db: Optional[Session] = None) -> RenderModel:
    totals = compute_totals(((i.unit_price, i.quantity) for i in draft.items), draft.gst_rate)
    signatory = brand.get_signatory(draft.signatory_id) or brand.SIGNATORIES[0]
    images: List[Optional[bytes]] = [load_item_image(i, db) for i in draft.items]
    return RenderModel(draft=draft, totals=totals, signatory=signatory, item_images=images)


def build_render_model_from_dict(payload: Dict) -> RenderModel:
    return build_render_model(QuotationDraft.model_validate(payload))


# ----------------------------- reference numbers ------------------------- #

def financial_year(d: date) -> str:
    """Indian FY (April–March): 2026-09-11 → '2026-27', 2027-02-01 → '2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def format_reference(d: date, initials: str, number: int, fy: Optional[str] = None) -> str:
    """`<DDMM><INITIALS><NNN>/<FY>` → 1109SW049/2026-27."""
    return f"{d:%d%m}{initials}{number:03d}/{fy or financial_year(d)}"


def storage_prefix(reference: str) -> str:
    """References contain '/', which is a path separator for every storage
    backend, so keys use '-' instead: quotations/1109SW049-2026-27."""
    return f"quotations/{reference.replace('/', '-')}"


def reference_filename(reference: str, ext: str = "pdf") -> str:
    return f"{reference.replace('/', '-')}.{ext}"


def peek_next_reference(db: Session, d: date, signatory: brand.Signatory) -> tuple:
    """(reference, fy, number) the auto-numbering WOULD assign. No write."""
    fy = financial_year(d)
    seq = db.get(QuotationSequence, fy)
    n = (seq.last_number if seq else 0) + 1
    return format_reference(d, signatory.initials, n, fy), fy, n


def allocate_reference(db: Session, d: date, signatory: brand.Signatory) -> str:
    """Take the next number for the FY inside the caller's transaction.

    The sequence row is locked (`SELECT … FOR UPDATE` on Postgres; SQLite is
    single-writer) so two admins submitting at once get consecutive numbers.
    A hand-typed reference that happens to equal the next auto value is
    skipped over rather than colliding.
    """
    fy = financial_year(d)
    seq = db.query(QuotationSequence).filter(QuotationSequence.fy == fy).with_for_update().one_or_none()
    if seq is None:
        seq = QuotationSequence(fy=fy, last_number=0)
        db.add(seq)
        db.flush()
    for _ in range(50):
        seq.last_number += 1
        ref = format_reference(d, signatory.initials, seq.last_number, fy)
        taken = db.query(Quotation.id).filter(Quotation.reference == ref).first()
        if taken is None:
            db.flush()
            return ref
    raise HTTPException(status_code=500, detail="Could not allocate a quotation reference")


# ----------------------------- issue ------------------------------------- #

def issue_quotation(db: Session, draft: QuotationDraft, user: User) -> Quotation:
    """Validate → assign reference → render → store → insert, in one transaction.

    Order matters: the reference is printed on the document, so it is fixed
    (and the sequence row locked) before rendering. The PDF goes to storage
    before the rows are committed — if storage fails, the transaction rolls
    back and nothing is inserted; if the commit then fails (a reference race),
    the stored file is cleaned up best-effort and the caller gets a 409.
    """
    from .storage import get_storage

    signatory = brand.get_signatory(draft.signatory_id) or brand.SIGNATORIES[0]

    if draft.reference:
        reference = draft.reference
        if db.query(Quotation.id).filter(Quotation.reference == reference).first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Reference {reference} is already used by another quotation",
            )
    else:
        reference = allocate_reference(db, draft.quotation_date, signatory)

    # Snapshot the catalogue picture onto each row so the quotation keeps
    # rendering identically even if the product is edited or retired later.
    from .quotation_catalogue import product_image_source

    items = []
    for item in draft.items:
        if item.include_image and item.product_id is not None and not item.has_image_source:
            key, asset = product_image_source(db, item.product_id)
            item = item.model_copy(update={"image_storage_key": key, "image_asset": asset})
        items.append(item)
    final = draft.model_copy(update={"reference": reference, "items": items})
    model = build_render_model(final, db)
    pdf = render_quotation_pdf(model)

    now = datetime.now(timezone.utc)
    q = Quotation(
        reference=reference,
        status="ISSUED",
        quotation_date=final.quotation_date,
        customer_name=final.customer_name,
        address_lines="\n".join(final.address_lines) or None,
        customer_gstin=final.customer_gstin,
        customer_pan=final.customer_pan,
        contact_name=final.contact_name,
        contact_phone=final.contact_phone,
        contact_email=final.contact_email,
        subject_line=final.subject_line,
        validity_days=final.validity_days,
        gst_rate=final.gst_rate,
        totals_label_set=final.totals_label_set.value,
        note_text=final.note_text,
        note_style=final.note_style.value,
        terms=list(final.terms or []),
        show_sl_no=final.show_sl_no,
        signatory_id=signatory.id,
        signatory_name=signatory.name,
        signatory_designation=signatory.designation,
        signatory_phones=signatory.phones,
        signatory_email=signatory.email,
        subtotal=model.totals.subtotal,
        gst_amount=model.totals.gst_amount,
        grand_total=model.totals.grand_total,
        created_by_id=user.id,
        issued_at=now,
        duplicated_from_id=_existing_quotation_id(db, final.duplicated_from_id),
    )
    for pos, (item, lt) in enumerate(zip(final.items, model.totals.line_totals), start=1):
        q.items.append(QuotationItem(
            position=pos,
            row_style=item.row_style.value,
            product_id=item.product_id,
            brand=item.brand,
            brand_sub_label=item.brand_sub_label,
            model=item.model,
            headline=item.headline,
            spec_lines=item.spec_lines,
            warranty_label=item.warranty_label,
            unit_price=item.unit_price,
            quantity=item.quantity,
            line_total=lt,
            include_image=bool(item.include_image and item.has_image_source),
            image_storage_key=item.image_storage_key,
            image_asset=item.image_asset,
        ))
    db.add(q)
    db.flush()

    storage = get_storage()
    prefix = storage_prefix(reference)
    stored = storage.save_bytes(pdf, "application/pdf", prefix, reference_filename(reference))
    q.pdf_storage_key = stored["storage_url"]

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            storage.cleanup(prefix)
        except Exception:
            logger.warning("Could not clean up %s after a reference conflict", prefix)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Reference {reference} was taken by a concurrent submission — try again",
        )
    db.refresh(q)
    return q



def update_quotation(db: Session, q: Quotation, draft: QuotationDraft, user: User) -> Quotation:
    """Correct an issued quotation in place and re-render its document.

    The reference is the quotation's identity — the customer already has it —
    so it is never reallocated here, whatever the draft carries. Everything
    else, the date and signatory included, is replaced from the draft.

    The stored PDF is overwritten (same storage key), and the lazily-cached
    docx/png/jpeg keys are cleared so the next download re-renders from the
    corrected data instead of serving the superseded document.
    """
    from .storage import get_storage

    signatory = brand.get_signatory(draft.signatory_id) or brand.SIGNATORIES[0]

    from .quotation_catalogue import product_image_source

    items = []
    for item in draft.items:
        if item.include_image and item.product_id is not None and not item.has_image_source:
            key, asset = product_image_source(db, item.product_id)
            item = item.model_copy(update={"image_storage_key": key, "image_asset": asset})
        items.append(item)
    final = draft.model_copy(update={"reference": q.reference, "items": items})
    model = build_render_model(final, db)
    pdf = render_quotation_pdf(model)

    q.quotation_date = final.quotation_date
    q.customer_name = final.customer_name
    q.address_lines = "\n".join(final.address_lines) or None
    q.customer_gstin = final.customer_gstin
    q.customer_pan = final.customer_pan
    q.contact_name = final.contact_name
    q.contact_phone = final.contact_phone
    q.contact_email = final.contact_email
    q.subject_line = final.subject_line
    q.validity_days = final.validity_days
    q.gst_rate = final.gst_rate
    q.totals_label_set = final.totals_label_set.value
    q.note_text = final.note_text
    q.note_style = final.note_style.value
    q.terms = list(final.terms or [])
    q.show_sl_no = final.show_sl_no
    q.signatory_id = signatory.id
    q.signatory_name = signatory.name
    q.signatory_designation = signatory.designation
    q.signatory_phones = signatory.phones
    q.signatory_email = signatory.email
    q.subtotal = model.totals.subtotal
    q.gst_amount = model.totals.gst_amount
    q.grand_total = model.totals.grand_total
    q.updated_at = datetime.now(timezone.utc)
    q.updated_by_id = user.id

    # delete-orphan on the relationship removes the previous rows.
    q.items.clear()
    db.flush()
    for pos, (item, lt) in enumerate(zip(final.items, model.totals.line_totals), start=1):
        q.items.append(QuotationItem(
            position=pos,
            row_style=item.row_style.value,
            product_id=item.product_id,
            brand=item.brand,
            brand_sub_label=item.brand_sub_label,
            model=item.model,
            headline=item.headline,
            spec_lines=item.spec_lines,
            warranty_label=item.warranty_label,
            unit_price=item.unit_price,
            quantity=item.quantity,
            line_total=lt,
            include_image=bool(item.include_image and item.has_image_source),
            image_storage_key=item.image_storage_key,
            image_asset=item.image_asset,
        ))

    storage = get_storage()
    prefix = storage_prefix(q.reference)
    stored = storage.save_bytes(pdf, "application/pdf", prefix, reference_filename(q.reference))
    q.pdf_storage_key = stored["storage_url"]
    # Superseded renders — regenerated on the next download of each format.
    q.docx_storage_key = None
    q.png_storage_key = None
    q.jpeg_storage_key = None

    db.commit()
    db.refresh(q)
    logger.info("Quotation %s edited by %s", q.reference, user.username)
    return q

def _existing_quotation_id(db: Session, quotation_id: Optional[int]) -> Optional[int]:
    if quotation_id is None:
        return None
    return quotation_id if db.get(Quotation, quotation_id) is not None else None


# ----------------------------- drafts from rows --------------------------- #

def quotation_to_draft(q: Quotation, *, as_duplicate: bool = False) -> QuotationDraft:
    """Rebuild the draft that produced a stored quotation. With
    `as_duplicate` the reference is cleared, the date is today and
    `duplicated_from_id` points at the source (plan §6 duplicate)."""
    items = [
        QuotationItemIn(
            row_style=i.row_style, product_id=i.product_id, brand=i.brand,
            brand_sub_label=i.brand_sub_label, model=i.model, headline=i.headline,
            spec_lines=i.spec_lines, warranty_label=i.warranty_label,
            unit_price=i.unit_price, quantity=i.quantity, include_image=bool(i.include_image),
            image_storage_key=i.image_storage_key, image_asset=i.image_asset,
        )
        for i in sorted(q.items, key=lambda x: x.position)
    ]
    return QuotationDraft(
        quotation_date=date.today() if as_duplicate else q.quotation_date,
        reference=None if as_duplicate else q.reference,
        customer_name=q.customer_name,
        address_lines=[l for l in (q.address_lines or "").split("\n") if l],
        customer_gstin=q.customer_gstin, customer_pan=q.customer_pan,
        contact_name=q.contact_name, contact_phone=q.contact_phone, contact_email=q.contact_email,
        subject_line=q.subject_line,
        signatory_id=q.signatory_id if brand.get_signatory(q.signatory_id) else brand.DEFAULT_SIGNATORY_ID,
        validity_days=q.validity_days, gst_rate=q.gst_rate,
        totals_label_set=q.totals_label_set, note_text=q.note_text, note_style=q.note_style,
        terms=list(q.terms or []), show_sl_no=q.show_sl_no, items=items,
        duplicated_from_id=q.id if as_duplicate else q.duplicated_from_id,
    )


# ----------------------------- file formats ------------------------------ #

FILE_FORMATS = {
    # format: (media type, extension, storage-key attribute)
    "pdf": ("application/pdf", "pdf", "pdf_storage_key"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", "docx_storage_key"),
    "png": ("image/png", "png", "png_storage_key"),
    "jpeg": ("image/jpeg", "jpg", "jpeg_storage_key"),
}


def get_quotation_file(db: Session, q: Quotation, fmt: str) -> tuple:
    """(bytes, media_type, filename) for a stored quotation in `fmt`.

    PDF = the stored document of record. The other three are rendered on
    first request — DOCX from the quotation data, PNG/JPEG from the stored
    PDF — and cached back to storage so later downloads are a plain read.
    """
    from .quotation_docx import render_quotation_docx
    from .quotation_raster import pdf_page_to_jpeg, pdf_page_to_png, DOWNLOAD_DPI
    from .storage import get_storage

    if fmt not in FILE_FORMATS:
        raise HTTPException(status_code=422, detail="format must be pdf, docx, png or jpeg")
    media_type, ext, key_attr = FILE_FORMATS[fmt]
    filename = reference_filename(q.reference, ext)

    key = getattr(q, key_attr)
    if key:
        data = read_storage_bytes(key)
        if data is not None:
            return data, media_type, filename
        if fmt == "pdf":
            raise HTTPException(status_code=502, detail="Stored PDF could not be read")
        logger.warning("Cached %s for %s unreadable — re-rendering", fmt, q.reference)
    elif fmt == "pdf":
        raise HTTPException(status_code=404, detail="No PDF stored for this quotation")

    if fmt == "docx":
        data = render_quotation_docx(build_render_model(quotation_to_draft(q)))
    else:
        pdf = read_storage_bytes(q.pdf_storage_key) if q.pdf_storage_key else None
        if pdf is None:
            raise HTTPException(status_code=502, detail="Stored PDF could not be read")
        data = pdf_page_to_png(pdf, dpi=DOWNLOAD_DPI) if fmt == "png" else pdf_page_to_jpeg(pdf)

    try:
        stored = get_storage().save_bytes(data, media_type, storage_prefix(q.reference), filename)
        setattr(q, key_attr, stored["storage_url"])
        db.commit()
    except Exception:
        # Caching is an optimisation; the download itself must still succeed.
        logger.exception("Could not cache %s for %s", fmt, q.reference)
        db.rollback()
    return data, media_type, filename


# ----------------------------- outputs ----------------------------------- #

def pdf_url_for(q: Quotation) -> Optional[str]:
    from .storage import get_storage

    if not q.pdf_storage_key:
        return None
    url = get_storage().public_url(q.pdf_storage_key)
    if not url.startswith("http"):
        # Local-disk mode serves /uploads from the API process (dev only).
        url = f"http://localhost:8000{url}"
    return url


def _user_ref(u) -> Optional[dict]:
    if u is None:
        return None
    return {"id": u.id, "name": getattr(u, "name", None), "username": u.username}


def _created_by(q: Quotation) -> Optional[dict]:
    return _user_ref(q.created_by)


def quotation_summary(q: Quotation) -> QuotationSummaryOut:
    return QuotationSummaryOut(
        id=q.id, reference=q.reference, status=q.status, quotation_date=q.quotation_date,
        customer_name=q.customer_name, subject_line=q.subject_line, grand_total=q.grand_total,
        created_by=_created_by(q), created_at=q.created_at,
        updated_by=_user_ref(q.updated_by), updated_at=q.updated_at,
    )


def quotation_to_out(q: Quotation) -> QuotationOut:
    return QuotationOut(
        **quotation_summary(q).model_dump(),
        address_lines=[l for l in (q.address_lines or "").split("\n") if l],
        customer_gstin=q.customer_gstin, customer_pan=q.customer_pan,
        contact_name=q.contact_name, contact_phone=q.contact_phone, contact_email=q.contact_email,
        validity_days=q.validity_days, gst_rate=q.gst_rate, totals_label_set=q.totals_label_set,
        note_text=q.note_text, note_style=q.note_style, terms=list(q.terms or []),
        show_sl_no=q.show_sl_no,
        signatory_id=q.signatory_id, signatory_name=q.signatory_name,
        signatory_designation=q.signatory_designation, signatory_phones=q.signatory_phones,
        signatory_email=q.signatory_email,
        subtotal=q.subtotal, gst_amount=q.gst_amount,
        pdf_url=pdf_url_for(q), duplicated_from_id=q.duplicated_from_id, issued_at=q.issued_at,
        items=[QuotationItemOut.model_validate(i, from_attributes=True) for i in q.items],
    )
