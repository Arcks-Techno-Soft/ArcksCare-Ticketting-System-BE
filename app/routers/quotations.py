"""Quotation endpoints (plan §6). The quotation workflow (and reading the
product catalogue) is open to Sales reps and Managers and above (Super Admin +
Admin + Manager + Sales; legacy OWNER via tier inheritance in `require_role`).
Editing the product catalogue stays with Managers and above.

Phase 1: `POST /preview` (render only). Phase 2: signatories, next-reference,
`POST /` (issue), `GET /{id}`, `GET /{id}/file`, a read-only seed catalogue
and a minimal list for the "Explore" page.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models.quotation import Quotation
from ..models.user import User, UserRole
from ..schemas.quotation import (
    ItemImageOut,
    NextReferenceOut,
    QuotationDraft,
    QuotationListOut,
    QuotationOut,
    QuotationProductIn,
    QuotationProductOut,
    QuotationProductPatch,
    ReorderProductsIn,
    SignatoryOut,
)
from ..services import quotation_brand as brand
from ..services.auth import require_role
from ..services import quotation_catalogue as catalogue
from ..services.quotation_catalogue import product_to_out
from ..services.quotation_pdf import render_quotation_pdf
from ..services.quotation_raster import pdf_page_to_png
from ..services.quotation_service import (
    build_render_model,
    issue_quotation,
    peek_next_reference,
    get_quotation_file,
    quotation_summary,
    quotation_to_draft,
    quotation_to_out,
    update_quotation,
)

logger = logging.getLogger("skposcare.quotation")

router = APIRouter(prefix="/api/v1/admin/quotations", tags=["quotations"])

# Headers the browser is allowed to read cross-origin (also listed in the CORS
# middleware's expose_headers in main.py).
TOTALS_HEADERS = ("X-Subtotal", "X-Gst", "X-Grand-Total")

# The quotation workflow is open to Sales reps and Managers and above.
QuotationUser = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES))
# Editing the product catalogue (shared prices / spec text) stays with Managers
# and above.
CatalogueEditor = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER))


@router.get("/signatories", response_model=list[SignatoryOut], summary="People who can sign a quotation")
def list_signatories(_user: User = QuotationUser):
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
    _user: User = QuotationUser,
):
    sig = brand.get_signatory(signatory_id)
    if sig is None:
        raise HTTPException(status_code=422, detail="Unknown signatory")
    d = date_ or date.today()
    ref, fy, n = peek_next_reference(db, d, sig)
    return NextReferenceOut(reference=ref, fy=fy, next_number=n, date=d)


@router.get("/presets", summary="T&C / note / totals-label presets for the form")
def presets(_user: User = QuotationUser):
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


# ----------------------------- catalogue ---------------------------------- #

@router.get("/products", response_model=list[QuotationProductOut], summary="Product catalogue")
def list_products(
    q: Optional[str] = Query(None, description="name / brand / model contains"),
    active: str = Query("true", pattern="^(true|false|all)$"),
    db: Session = Depends(get_db),
    _user: User = QuotationUser,
):
    return [product_to_out(p) for p in catalogue.list_products(db, q=q, active=active)]


def _parse_payload(payload: str, model):
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())


@router.post("/products", response_model=QuotationProductOut, status_code=status.HTTP_201_CREATED,
             summary="Add a catalogue product (multipart: payload JSON + optional image)")
async def create_product(
    payload: str = Form(..., description="QuotationProductIn as JSON"),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user: User = CatalogueEditor,
):
    body = _parse_payload(payload, QuotationProductIn)
    img = await catalogue.read_image_upload(image) if image is not None and image.filename else None
    return product_to_out(catalogue.create_product(db, body, user.id, img))


@router.post("/products/reorder", response_model=list[QuotationProductOut], summary="Set sort order")
def reorder_products(body: ReorderProductsIn, db: Session = Depends(get_db), _user: User = CatalogueEditor):
    return [product_to_out(p) for p in catalogue.reorder_products(db, body.ids)]


@router.patch("/products/{product_id}", response_model=QuotationProductOut,
              summary="Edit a catalogue product (multipart: payload JSON + optional image)")
async def update_product(
    product_id: int,
    payload: str = Form("{}"),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _user: User = CatalogueEditor,
):
    p = catalogue.get_product_or_404(db, product_id)
    body = _parse_payload(payload, QuotationProductPatch)
    img = await catalogue.read_image_upload(image) if image is not None and image.filename else None
    return product_to_out(catalogue.update_product(db, p, body, img))


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Retire a product (soft delete)")
def delete_product(product_id: int, db: Session = Depends(get_db), _user: User = CatalogueEditor):
    catalogue.retire_product(db, catalogue.get_product_or_404(db, product_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/item-images", response_model=ItemImageOut, status_code=status.HTTP_201_CREATED,
             summary="Upload a one-off item picture")
async def upload_item_image(image: UploadFile = File(...), _user: User = QuotationUser):
    data, ctype, ext = await catalogue.read_image_upload(image)
    key = catalogue.store_item_image(data, ctype, ext)
    return ItemImageOut(storage_key=key, url=catalogue.image_url_for(key, None) or "")


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
    db: Session = Depends(get_db),
    _user: User = QuotationUser,
) -> Response:
    model = build_render_model(draft, db)
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
    user: User = QuotationUser,
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
    _user: User = QuotationUser,
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
def get_quotation(quotation_id: int, db: Session = Depends(get_db), _user: User = QuotationUser):
    return quotation_to_out(_get_or_404(db, quotation_id))


@router.get("/{quotation_id}/file", summary="Download the document (pdf | docx | png | jpeg)", response_class=Response)
def download_quotation_file(
    quotation_id: int,
    format: str = Query("pdf", pattern="^(pdf|docx|png|jpeg)$"),
    db: Session = Depends(get_db),
    _user: User = QuotationUser,
):
    """PDF is the stored document of record. Word (an editable copy), PNG and
    JPEG are rendered on first request and cached back to storage."""
    q = _get_or_404(db, quotation_id)
    data, media_type, filename = get_quotation_file(db, q, format)
    return Response(
        data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/{quotation_id}/draft", response_model=QuotationDraft,
            summary="The quotation as an editable draft")
def quotation_draft(quotation_id: int, db: Session = Depends(get_db), _user: User = QuotationUser):
    """Pre-fills the form for an edit. Unlike /duplicate this keeps the
    reference and the original date — the same quotation, not a copy."""
    return quotation_to_draft(_get_or_404(db, quotation_id))


@router.put("/{quotation_id}", response_model=QuotationOut, summary="Edit an issued quotation")
def edit_quotation(
    quotation_id: int,
    draft: QuotationDraft,
    db: Session = Depends(get_db),
    user: User = QuotationUser,
):
    """Correct a quotation in place. The reference is kept whatever the draft
    says — it identifies the document the customer already holds — and the
    stored PDF is re-rendered so the download always matches the saved data.
    """
    q = update_quotation(db, _get_or_404(db, quotation_id), draft, user)
    return quotation_to_out(q)


@router.post("/{quotation_id}/duplicate", response_model=QuotationDraft, summary="Draft a copy of a quotation")
def duplicate_quotation(quotation_id: int, db: Session = Depends(get_db), _user: User = QuotationUser):
    """Returns a draft to pre-fill the form: reference cleared (a new number
    is assigned on submit), date = today, every item copied, and
    `duplicated_from_id` set so the issued row records its origin."""
    return quotation_to_draft(_get_or_404(db, quotation_id), as_duplicate=True)
