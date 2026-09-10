"""Quotation business logic: totals, signatory + image resolution.

Phase 1 covers what `POST /preview` needs. Numbering, persistence and the
issue transaction arrive with Phase 2.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from ..config import get_settings
from ..schemas.quotation import QuotationDraft, QuotationItemIn
from . import quotation_brand as brand
from .quotation_money import compute_totals
from .quotation_pdf import RenderModel

logger = logging.getLogger("skposcare.quotation")


# ----------------------------- image resolution -------------------------- #

def _read_storage_bytes(storage_key: str) -> Optional[bytes]:
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
            r = httpx.get(url, timeout=15.0)
            r.raise_for_status()
            return r.content
        except Exception:
            logger.exception("Failed to fetch quotation image %s", storage_key)
            return None
    if url.startswith("/uploads/"):
        p = Path(get_settings().local_upload_dir) / url[len("/uploads/"):]
        if p.is_file():
            return p.read_bytes()
    return None


def load_item_image(item: QuotationItemIn) -> Optional[bytes]:
    """Bytes of the photo to print for an item, or None."""
    if not item.include_image:
        return None
    if item.image_asset:
        path = brand.bundled_product_images().get(item.image_asset)
        if path is not None:
            return path.read_bytes()
    if item.image_storage_key:
        return _read_storage_bytes(item.image_storage_key)
    return None


# ----------------------------- render model ------------------------------ #

def build_render_model(draft: QuotationDraft) -> RenderModel:
    totals = compute_totals(((i.unit_price, i.quantity) for i in draft.items), draft.gst_rate)
    signatory = brand.get_signatory(draft.signatory_id) or brand.SIGNATORIES[0]
    images: List[Optional[bytes]] = [load_item_image(i) for i in draft.items]
    return RenderModel(draft=draft, totals=totals, signatory=signatory, item_images=images)


def build_render_model_from_dict(payload: Dict) -> RenderModel:
    return build_render_model(QuotationDraft.model_validate(payload))
