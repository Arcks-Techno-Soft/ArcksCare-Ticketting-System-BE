"""Quotation endpoints (plan §6). Every route is Admin-level (Super Admin +
Admin; legacy OWNER via tier inheritance in `require_role`).

Phase 1: `POST /preview` (render only). Phase 2: signatories, next-reference,
`POST /` (issue), `GET /{id}`, `GET /{id}/file`, a read-only seed catalogue
and a minimal list for the "Explore" page.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models.quotation import Quotation
from ..models.user import User, UserRole
from ..schemas.quotation import (
    NextReferenceOut,
    QuotationDraft,
    QuotationListOut,
    QuotationOut,
    QuotationProductOut,
    SignatoryOut,
)
from ..services import quotation_brand as brand
from ..services.auth import require_role
from ..services.quotation_fixtures import SEED_PRODUCTS
from ..services.quotation_pdf import render_quotation_pdf
from ..services.quotation_raster import pdf_page_to_png
from ..services.quotation_service import (
    build_render_model,
    issue_quotation,
    peek_next_reference,
    quotation_summary,
    quotation_to_out,
    read_storage_bytes,
    reference_filename,
)

logger = logging.getLogger("skposcare.quotation")

router = APIRouter(prefix="/api/v1/admin/quotations", tags=["quotations"])

# Headers the browser is allowed to read cross-origin (also listed in the CORS
# middleware's expose_headers in main.py).
TOTALS_HEADERS = ("X-Subtotal", "X-Gst", "X-Grand-Total")

AdminUser = Depends(require_role(UserRole.ADMIN))


@router.get("/signatories", response_model=list[SignatoryOut], summary="People who can sign a quotation")
def list_signatories(_user: User = AdminUser):
    return [
        SignatoryOut(id=s.id, name=s.name, designation=s.designation, phones=s.phones,
                     email=s.email, initials=s.initials)
        for s in brand.SIGNATORIES
    ]


@router.get("/next-reference", response_model=NextReferenceOut, summary="Preview the auto reference")
def next_reference(
    date_: Optional[date] = Query(None, alias="date"),
    signatory_id: int = Query(brand.DEFAULT_SIGNATORY_ID),
    db: Session = Depends(get_db),
    _user: User = AdminUser,
):
    sig = brand.get_signatory(signatory_id)
    if sig is None:
        raise HTTPException(status_code=422, detail="Unknown signatory")
    d = date_ or date.today()
    ref, fy, n = peek_next_reference(db, d, sig)
    return NextReferenceOut(reference=ref, fy=fy, next_number=n, date=d)


@router.get("/presets", summary="T&C / note / totals-label presets for the form")
def presets(_user: User = AdminUser):
    """Server-defined presets so the form never carries its own copy. Terms
    line 1 has a `{days}` placeholder the form fills from validity days."""
    return {
        "terms": brand.TERMS_PRESETS,
        "red_term_index": brand.RED_TERM_INDEX,
        "notes": {k: {"style": v[0], "text": v[1]} for k, v in brand.NOTE_PRESETS.items()},
        "totals_labels": {k: {"subtotal": v[0], "gst": v[1], "grand_total": v[2]} for k, v in brand.TOTALS_LABELS.items()},
        "default_validity_days": 15,
        "default_gst_rate": "18",
    }


@router.get("/products", response_model=list[QuotationProductOut], summary="Product catalogue")
def list_products(q: Optional[str] = Query(None), _user: User = AdminUser):
    """Read-only seed catalogue (the three products from the samples) until
    the DB-backed catalogue lands in Phase 4 — same response shape."""
    rows = [QuotationProductOut(**p) for p in SEED_PRODUCTS]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in f"{r.brand or ''} {r.model or ''} {r.name}".lower()]
    return rows


@router.post(
    "/preview",
    summary="Render a draft quotation (no DB write)",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}, "image/png": {}},
            "description": "The rendered document. Totals in X-Subtotal / X-Gst / X-Grand-Total.",
        }
    },
)
def preview_quotation(
    draft: QuotationDraft,
    format: str = Query("pdf", pattern="^(pdf|png)$"),
    _user: User = AdminUser,
) -> Response:
    model = build_render_model(draft)
    try:
        pdf = render_quotation_pdf(model)
    except Exception:
        logger.exception("Quotation preview render failed")
        raise HTTPException(status_code=500, detail="Could not render the quotation")

    t = model.totals
    headers = {
        "X-Subtotal": f"{t.subtotal:.2f}",
        "X-Gst": f"{t.gst_amount:.2f}",
        "X-Grand-Total": f"{t.grand_total:.2f}",
        "Access-Control-Expose-Headers": ", ".join(TOTALS_HEADERS + ("Content-Disposition",)),
        "Cache-Control": "no-store",
    }
    if format == "png":
        headers["Content-Disposition"] = 'inline; filename="quotation-preview.png"'
        return Response(pdf_page_to_png(pdf), media_type="image/png", headers=headers)
    headers["Content-Disposition"] = 'inline; filename="quotation-preview.pdf"'
    return Response(pdf, media_type="application/pdf", headers=headers)


@router.post("", response_model=QuotationOut, status_code=status.HTTP_201_CREATED, summary="Issue a quotation")
def create_quotation(
    draft: QuotationDraft,
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    q = issue_quotation(db, draft, user)
    return quotation_to_out(q)


@router.get("", response_model=QuotationListOut, summary="Issued quotations")
def list_quotations(
    q: Optional[str] = Query(None, description="reference / customer / subject contains"),
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = AdminUser,
):
    query = db.query(Quotation)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            Quotation.reference.ilike(like),
            Quotation.customer_name.ilike(like),
            Quotation.subject_line.ilike(like),
        ))
    if from_:
        query = query.filter(Quotation.quotation_date >= from_)
    if to:
        query = query.filter(Quotation.quotation_date <= to)
    total = query.count()
    rows = (
        query.order_by(Quotation.created_at.desc(), Quotation.id.desc())
        .limit(limit).offset(offset).all()
    )
    return QuotationListOut(items=[quotation_summary(r) for r in rows], total=total, limit=limit, offset=offset)


def _get_or_404(db: Session, quotation_id: int) -> Quotation:
    q = (
        db.query(Quotation)
        .options(selectinload(Quotation.items))
        .filter(Quotation.id == quotation_id)
        .one_or_none()
    )
    if q is None:
        raise HTTPException(status_code=404, detail="Quotation not found")
    return q


@router.get("/{quotation_id}", response_model=QuotationOut, summary="Quotation detail")
def get_quotation(quotation_id: int, db: Session = Depends(get_db), _user: User = AdminUser):
    return quotation_to_out(_get_or_404(db, quotation_id))


@router.get("/{quotation_id}/file", summary="Download the stored document", response_class=Response)
def download_quotation_file(
    quotation_id: int,
    format: str = Query("pdf", pattern="^(pdf)$"),
    db: Session = Depends(get_db),
    _user: User = AdminUser,
):
    q = _get_or_404(db, quotation_id)
    if not q.pdf_storage_key:
        raise HTTPException(status_code=404, detail="No PDF stored for this quotation")
    data = read_storage_bytes(q.pdf_storage_key)
    if data is None:
        raise HTTPException(status_code=502, detail="Stored PDF could not be read")
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{reference_filename(q.reference)}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
