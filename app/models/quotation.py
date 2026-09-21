"""Quotation tables (plan §5.1).

  quotation_products   — the catalogue (upload a product once, reuse forever)
  quotations           — one issued quotation (header + totals + PDF). Editable
                         in place: the reference is fixed, everything else can
                         be corrected and the document re-rendered.
  quotation_items      — its printed rows, in order
  quotation_sequences  — per-financial-year running number for auto references

All new tables → created by `Base.metadata.create_all` on boot once this
module is imported in `main._bootstrap_db`. The edit columns arrived later,
so they get an idempotent ALTER (`ensure_quotation_edit_columns`).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class QuotationProduct(Base):
    __tablename__ = "quotation_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    name: Mapped[str] = mapped_column(String(200), index=True)

    # Printed "PRODUCT NAME & DESCRIPTION" cell; [[...]] prints red.
    headline: Mapped[str] = mapped_column(Text)
    # Newline-separated spec lines; [[...]] prints red.
    spec_lines: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    brand_sub_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    warranty_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    default_unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    image_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    image_content_type: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    # Bundled seed photo (file under assets/quotation/products) for the three
    # products shipped with the app; uploads use image_storage_key instead.
    image_asset: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    default_row_style: Mapped[str] = mapped_column(String(10), default="DETAILED")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Quotation(Base):
    __tablename__ = "quotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(12), default="ISSUED", index=True)
    quotation_date: Mapped[date] = mapped_column(Date, index=True)

    customer_name: Mapped[str] = mapped_column(String(200), index=True)
    # Newline-separated lines as printed.
    address_lines: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    customer_gstin: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    customer_pan: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    contact_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    subject_line: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    validity_days: Mapped[int] = mapped_column(Integer, default=15)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("18.00"))
    totals_label_set: Mapped[str] = mapped_column(String(10), default="BASIC")
    note_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    note_style: Mapped[str] = mapped_column(String(16), default="GREEN_ON_BLACK")
    # list[str]; line index 1 (taxes) prints red by convention.
    terms: Mapped[list] = mapped_column(JSON, default=list)
    show_sl_no: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Snapshot of the signatory at issue time — an old quotation must keep
    # printing the person who issued it even if the list changes later.
    signatory_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    signatory_name: Mapped[str] = mapped_column(String(120))
    signatory_designation: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    signatory_phones: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    signatory_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    # Document of record + lazily rendered/cached alternates.
    pdf_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    docx_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    png_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    jpeg_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    duplicated_from_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("quotations.id"), nullable=True
    )
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    issued_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when an issued quotation is corrected in place (reference kept, the
    # document re-rendered). NULL means it still reads exactly as issued.
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    items: Mapped[List["QuotationItem"]] = relationship(
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by="QuotationItem.position",
    )
    created_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    updated_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[updated_by_id], lazy="joined"
    )


class QuotationItem(Base):
    __tablename__ = "quotation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    row_style: Mapped[str] = mapped_column(String(10), default="DETAILED")
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("quotation_products.id"), nullable=True
    )

    brand: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    brand_sub_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    headline: Mapped[str] = mapped_column(Text)
    spec_lines: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    warranty_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    include_image: Mapped[bool] = mapped_column(Boolean, default=False)
    # Resolved at issue time: row override, else the product's image.
    image_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # Bundled seed photo (file name under assets/quotation/products) — used by
    # the seed catalogue until product images live in storage (Phase 4).
    image_asset: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    quotation: Mapped["Quotation"] = relationship(back_populates="items")


class QuotationSequence(Base):
    """Running number per Indian financial year for auto references (§5.3)."""

    __tablename__ = "quotation_sequences"

    fy: Mapped[str] = mapped_column(String(7), primary_key=True)  # "2026-27"
    last_number: Mapped[int] = mapped_column(Integer, default=0)


from .user import User  # noqa: E402,F401
