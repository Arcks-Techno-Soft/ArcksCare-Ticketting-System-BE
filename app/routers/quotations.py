"""Quotation endpoints (plan §6). Every route is Admin-level (Super Admin +
Admin; legacy OWNER via tier inheritance in `require_role`).

Phase 1 ships `POST /preview` only: render the draft to a PDF (or a PNG of
page 1 with `?format=png`) without touching the database.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..models.user import User, UserRole
from ..schemas.quotation import QuotationDraft
from ..services.auth import require_role
from ..services.quotation_pdf import render_quotation_pdf
from ..services.quotation_raster import pdf_page_to_png
from ..services.quotation_service import build_render_model

logger = logging.getLogger("skposcare.quotation")

router = APIRouter(prefix="/api/v1/admin/quotations", tags=["quotations"])

# Headers the browser is allowed to read cross-origin (also listed in the CORS
# middleware's expose_headers in main.py).
TOTALS_HEADERS = ("X-Subtotal", "X-Gst", "X-Grand-Total")


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
    _user: User = Depends(require_role(UserRole.ADMIN)),
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
