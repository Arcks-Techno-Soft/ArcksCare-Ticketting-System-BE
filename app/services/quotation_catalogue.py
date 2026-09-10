"""Quotation product catalogue + image uploads (plan §3, Phase 4).

Products are uploaded once and reused: "Add from catalogue" in the form copies
the row into the quotation, so editing or retiring a product later never
changes an issued document. The three products from the samples are seeded
on boot with `image_asset` pointing at the bundled photos.
"""
from __future__ import annotations

import io
import logging
import uuid
from typing import Iterable, List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from PIL import Image as PILImage, UnidentifiedImageError
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..database import MIGRATION_SCHEMA, qualify
from ..models.quotation import QuotationProduct
from ..schemas.quotation import QuotationProductIn, QuotationProductOut, QuotationProductPatch
from . import quotation_brand as brand
from .quotation_fixtures import SEED_PRODUCTS
from .quotation_pdf import _prepare_image_bytes

logger = logging.getLogger("skposcare.quotation")

PRODUCT_KEY_PREFIX = "quotation-products"
ITEM_KEY_PREFIX = "quotation-items"
# Path the bundled seed photos are served from (mounted in main.py).
STATIC_PRODUCT_IMAGES = "/static/quotation-products"
MAX_IMAGE_BYTES = 5 * 1024 * 1024


# ----------------------------- column migration -------------------------- #

def ensure_quotation_product_columns(engine: Engine) -> None:
    """quotation_products.image_asset for tables created before Phase 4."""
    insp = inspect(engine)
    if "quotation_products" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return
    existing = {c["name"] for c in insp.get_columns("quotation_products", schema=MIGRATION_SCHEMA)}
    if "image_asset" in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {qualify('quotation_products')} ADD COLUMN image_asset VARCHAR(120)"))
        logger.info("Added quotation_products.image_asset column")


# ----------------------------- seeding ----------------------------------- #

def seed_quotation_products(db: Session) -> int:
    """Insert the sample products once (matched by name). Returns how many
    rows were added — 0 on every boot after the first."""
    existing = {n for (n,) in db.query(QuotationProduct.name).all()}
    added = 0
    for order, p in enumerate(SEED_PRODUCTS):
        if p["name"] in existing:
            continue
        db.add(QuotationProduct(
            brand=p["brand"], brand_sub_label=p["brand_sub_label"], model=p["model"], name=p["name"],
            headline=p["headline"], spec_lines=p["spec_lines"], warranty_label=p["warranty_label"],
            default_unit_price=p["default_unit_price"], default_row_style=p["default_row_style"],
            image_asset=p["image_asset"], active=True, sort_order=order,
        ))
        added += 1
    if added:
        db.commit()
        logger.info("Seeded %d quotation catalogue products", added)
    return added


# ----------------------------- images ------------------------------------ #

async def read_image_upload(upload: UploadFile) -> Tuple[bytes, str, str]:
    """Validate + re-encode an uploaded picture.

    Returns (bytes, content_type, extension). Anything Pillow can't decode is
    a 422 — this also strips EXIF and defeats polyglot files, and downsizes
    to ≤ 800 px (plenty for a ≤ 130 pt photo on paper).
    """
    mime = (upload.content_type or "").lower()
    name = upload.filename or ""
    if not mime.startswith("image/") and name.rsplit(".", 1)[-1].lower() not in {"png", "jpg", "jpeg", "webp", "gif", "bmp"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Please upload an image (PNG or JPEG)")
    raw = await upload.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image must be 5 MB or smaller")
    if not raw:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty upload")
    try:
        with PILImage.open(io.BytesIO(raw)) as im:
            im.verify()
        data = _prepare_image_bytes(raw)
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="That file is not a valid image")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png", "png"
    return data, "image/jpeg", "jpg"


def store_product_image(product_id: int, data: bytes, content_type: str, ext: str) -> str:
    from .storage import get_storage

    stored = get_storage().save_bytes(data, content_type, f"{PRODUCT_KEY_PREFIX}/{product_id}", f"product.{ext}")
    return stored["storage_url"]


def store_item_image(data: bytes, content_type: str, ext: str) -> str:
    from .storage import get_storage

    stored = get_storage().save_bytes(data, content_type, f"{ITEM_KEY_PREFIX}/{uuid.uuid4().hex}", f"image.{ext}")
    return stored["storage_url"]


def image_url_for(storage_key: Optional[str], image_asset: Optional[str]) -> Optional[str]:
    """Viewable URL for a picture: presigned/absolute for S3, API-relative
    for local disk (/uploads/…) and bundled assets (/static/…)."""
    from .storage import get_storage

    if storage_key:
        try:
            return get_storage().public_url(storage_key)
        except Exception:
            logger.exception("public_url failed for %s", storage_key)
            return None
    if image_asset and image_asset in brand.bundled_product_images():
        return f"{STATIC_PRODUCT_IMAGES}/{image_asset}"
    return None


# ----------------------------- CRUD -------------------------------------- #

def product_to_out(p: QuotationProduct) -> QuotationProductOut:
    return QuotationProductOut(
        id=p.id, brand=p.brand, brand_sub_label=p.brand_sub_label, model=p.model, name=p.name,
        headline=p.headline, spec_lines=p.spec_lines, warranty_label=p.warranty_label,
        default_unit_price=p.default_unit_price, default_row_style=p.default_row_style,
        image_asset=p.image_asset, image_storage_key=p.image_storage_key,
        image_url=image_url_for(p.image_storage_key, p.image_asset),
        active=bool(p.active), sort_order=p.sort_order or 0,
    )


def list_products(db: Session, *, q: Optional[str], active: str) -> List[QuotationProduct]:
    query = db.query(QuotationProduct)
    if active == "true":
        query = query.filter(QuotationProduct.active.is_(True))
    elif active == "false":
        query = query.filter(QuotationProduct.active.is_(False))
    if q:
        like = f"%{q.strip()}%"
        from sqlalchemy import or_
        query = query.filter(or_(
            QuotationProduct.name.ilike(like), QuotationProduct.brand.ilike(like), QuotationProduct.model.ilike(like),
        ))
    return query.order_by(QuotationProduct.sort_order.asc(), QuotationProduct.id.asc()).all()


def get_product_or_404(db: Session, product_id: int) -> QuotationProduct:
    p = db.get(QuotationProduct, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return p


def create_product(db: Session, body: QuotationProductIn, user_id: Optional[int],
                   image: Optional[Tuple[bytes, str, str]]) -> QuotationProduct:
    if body.sort_order is None:
        last = db.query(QuotationProduct.sort_order).order_by(QuotationProduct.sort_order.desc()).first()
        body.sort_order = (last[0] + 1) if last and last[0] is not None else 0
    p = QuotationProduct(
        brand=body.brand, brand_sub_label=body.brand_sub_label, model=body.model, name=body.name,
        headline=body.headline, spec_lines=body.spec_lines, warranty_label=body.warranty_label,
        default_unit_price=body.default_unit_price, default_row_style=body.default_row_style.value,
        sort_order=body.sort_order, active=True, created_by_id=user_id,
    )
    db.add(p)
    db.flush()
    if image:
        data, ctype, ext = image
        p.image_storage_key = store_product_image(p.id, data, ctype, ext)
        p.image_content_type = ctype
    db.commit()
    db.refresh(p)
    return p


def update_product(db: Session, p: QuotationProduct, body: QuotationProductPatch,
                   image: Optional[Tuple[bytes, str, str]]) -> QuotationProduct:
    for field in ("name", "brand", "brand_sub_label", "model", "headline", "spec_lines",
                  "warranty_label", "default_unit_price", "sort_order", "active"):
        value = getattr(body, field)
        if field in body.model_fields_set:
            setattr(p, field, value.strip() if isinstance(value, str) and field in ("name", "headline") else value)
    if body.default_row_style is not None:
        p.default_row_style = body.default_row_style.value
    if body.remove_image:
        p.image_storage_key = None
        p.image_content_type = None
        p.image_asset = None
    if image:
        data, ctype, ext = image
        p.image_storage_key = store_product_image(p.id, data, ctype, ext)
        p.image_content_type = ctype
        p.image_asset = None
    db.commit()
    db.refresh(p)
    return p


def retire_product(db: Session, p: QuotationProduct) -> None:
    p.active = False
    db.commit()


def reorder_products(db: Session, ids: Iterable[int]) -> List[QuotationProduct]:
    ids = list(ids)
    rows = {p.id: p for p in db.query(QuotationProduct).filter(QuotationProduct.id.in_(ids)).all()}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown product ids: {missing}")
    for order, pid in enumerate(ids):
        rows[pid].sort_order = order
    db.commit()
    return list_products(db, q=None, active="all")


def product_image_source(db: Optional[Session], product_id: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """(image_storage_key, image_asset) of a catalogue product, if any."""
    if db is None or product_id is None:
        return None, None
    p = db.get(QuotationProduct, product_id)
    if p is None:
        return None, None
    return p.image_storage_key, p.image_asset
