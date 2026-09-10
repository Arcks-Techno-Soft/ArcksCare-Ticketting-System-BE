"""Pydantic schemas for the quotation endpoints (plan §5.2 / §6).

`QuotationDraft` is what the form sends to `POST /preview` (no DB write) and
later to `POST /` (issue). Money fields are Decimals — the server is the only
place totals are computed.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..services import quotation_brand as brand

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Storage-key prefixes an item image may come from (prevents key injection —
# a client can't make the renderer fetch arbitrary objects from the bucket).
ALLOWED_IMAGE_KEY_PREFIXES = ("quotation-products/", "quotation-items/")

MAX_ITEMS = 50
MAX_TERMS = 12
MAX_TERM_LEN = 400


class RowStyle(str, Enum):
    COMPACT = "COMPACT"
    DETAILED = "DETAILED"


class TotalsLabelSet(str, Enum):
    BASIC = "BASIC"    # TOTAL BASIC PRICE / GST / TOTAL AMOUNT  (POS)
    SIMPLE = "SIMPLE"  # TOTAL / GST / GRAND TOTAL               (CCTV)


class NoteStyle(str, Enum):
    RED_TEXT = "RED_TEXT"
    GREEN_ON_BLACK = "GREEN_ON_BLACK"


class TermsPreset(str, Enum):
    POS = "POS"
    CCTV = "CCTV"


def _blank_to_none(v):
    if isinstance(v, str) and not v.strip():
        return None
    return v


class QuotationItemIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    row_style: RowStyle = RowStyle.DETAILED
    product_id: Optional[int] = None

    brand: Optional[str] = Field(default=None, max_length=80)
    brand_sub_label: Optional[str] = Field(default=None, max_length=80)
    # May contain a newline ("Mighty Series\nM95") — printed on two lines.
    model: Optional[str] = Field(default=None, max_length=120)
    # PRODUCT NAME & DESCRIPTION cell; [[...]] prints red.
    headline: str = Field(min_length=1, max_length=1000)
    # Newline-separated; [[...]] prints red. Only used for DETAILED rows.
    spec_lines: Optional[str] = Field(default=None, max_length=4000)
    warranty_label: Optional[str] = Field(default=None, max_length=80)

    unit_price: Decimal = Field(ge=0, le=Decimal("999999999.99"), decimal_places=2)
    quantity: Decimal = Field(gt=0, le=Decimal("9999999.99"), decimal_places=2)

    include_image: bool = False
    # Either a stored object (uploaded product / one-off image) ...
    image_storage_key: Optional[str] = Field(default=None, max_length=500)
    # ... or one of the bundled seed photos (file name in assets/quotation/products).
    image_asset: Optional[str] = Field(default=None, max_length=120)

    @field_validator(
        "brand", "brand_sub_label", "model", "spec_lines", "warranty_label",
        "image_storage_key", "image_asset", mode="before",
    )
    @classmethod
    def _optional_blank(cls, v):
        return _blank_to_none(v)

    @field_validator("headline")
    @classmethod
    def _headline_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Product name / description is required")
        return v.strip()

    @field_validator("image_storage_key")
    @classmethod
    def _key_prefix(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if ".." in v or v.startswith("/") or not v.startswith(ALLOWED_IMAGE_KEY_PREFIXES):
            raise ValueError(
                "image_storage_key must start with one of "
                + ", ".join(ALLOWED_IMAGE_KEY_PREFIXES)
            )
        return v

    @field_validator("image_asset")
    @classmethod
    def _asset_exists(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in brand.bundled_product_images():
            raise ValueError(f"Unknown bundled product image: {v}")
        return v

    @property
    def has_spec_block(self) -> bool:
        """Whether a DETAILED row needs its second (spec) row at all."""
        return bool(self.spec_lines or self.warranty_label or (self.include_image and self.has_image_source))

    @property
    def has_image_source(self) -> bool:
        return bool(self.image_storage_key or self.image_asset)


class QuotationDraft(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": []})  # filled below

    quotation_date: date = Field(default_factory=date.today)
    reference: Optional[str] = Field(default=None, max_length=40)

    customer_name: str = Field(min_length=1, max_length=200)
    address_lines: List[str] = Field(default_factory=list, max_length=6)
    customer_gstin: Optional[str] = Field(default=None, max_length=15)
    customer_pan: Optional[str] = Field(default=None, max_length=10)
    contact_name: Optional[str] = Field(default=None, max_length=120)
    contact_phone: Optional[str] = Field(default=None, max_length=20)
    contact_email: Optional[str] = Field(default=None, max_length=200)

    subject_line: Optional[str] = Field(default=None, max_length=200)
    signatory_id: int = brand.DEFAULT_SIGNATORY_ID
    validity_days: int = Field(default=15, ge=1, le=365)

    gst_rate: Decimal = Field(default=Decimal("18"), ge=0, le=28, decimal_places=2)
    totals_label_set: TotalsLabelSet = TotalsLabelSet.BASIC

    note_text: Optional[str] = Field(default=None, max_length=300)
    note_style: NoteStyle = NoteStyle.GREEN_ON_BLACK

    terms_preset: Optional[TermsPreset] = None
    # Explicit lines win; when None they're filled from the preset (POS default).
    terms: Optional[List[str]] = Field(default=None, max_length=MAX_TERMS)

    # None = auto: show the Sl No. column when every row is COMPACT.
    show_sl_no: Optional[bool] = None

    items: List[QuotationItemIn] = Field(min_length=1, max_length=MAX_ITEMS)

    @field_validator(
        "reference", "customer_gstin", "customer_pan", "contact_name", "contact_phone",
        "contact_email", "subject_line", "note_text", mode="before",
    )
    @classmethod
    def _optional_blank(cls, v):
        return _blank_to_none(v)

    @field_validator("customer_name", "reference", "subject_line")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("address_lines")
    @classmethod
    def _clean_address(cls, lines: List[str]) -> List[str]:
        cleaned = [l.strip() for l in lines if l and l.strip()]
        for l in cleaned:
            if len(l) > 120:
                raise ValueError("Address lines must be 120 characters or fewer")
        return cleaned

    @field_validator("customer_gstin")
    @classmethod
    def _gstin(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().upper()
        if not GSTIN_RE.match(v):
            raise ValueError("Invalid GSTIN")
        return v

    @field_validator("customer_pan")
    @classmethod
    def _pan(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().upper()
        if not PAN_RE.match(v):
            raise ValueError("Invalid PAN")
        return v

    @field_validator("signatory_id")
    @classmethod
    def _signatory(cls, v: int) -> int:
        if brand.get_signatory(v) is None:
            raise ValueError("Unknown signatory")
        return v

    @field_validator("terms")
    @classmethod
    def _terms(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        cleaned = [l.strip() for l in v if l and l.strip()]
        for l in cleaned:
            if len(l) > MAX_TERM_LEN:
                raise ValueError(f"Each term must be {MAX_TERM_LEN} characters or fewer")
        return cleaned or None

    @model_validator(mode="after")
    def _fill_terms(self) -> "QuotationDraft":
        if not self.terms:
            preset = (self.terms_preset or TermsPreset.POS).value
            self.terms = brand.terms_for(preset, self.validity_days)
        return self

    @property
    def sl_no_visible(self) -> bool:
        if self.show_sl_no is not None:
            return self.show_sl_no
        return all(i.row_style == RowStyle.COMPACT for i in self.items)


# --------------------------- outputs ------------------------------------- #

class TotalsOut(BaseModel):
    subtotal: Decimal
    gst_rate: Decimal
    gst_amount: Decimal
    grand_total: Decimal
    line_totals: List[Decimal]


class SignatoryOut(BaseModel):
    id: int
    name: str
    designation: str
    phones: str
    email: str


class QuotationItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    row_style: str
    product_id: Optional[int] = None
    brand: Optional[str] = None
    brand_sub_label: Optional[str] = None
    model: Optional[str] = None
    headline: str
    spec_lines: Optional[str] = None
    warranty_label: Optional[str] = None
    unit_price: Decimal
    quantity: Decimal
    line_total: Decimal
    include_image: bool
    image_storage_key: Optional[str] = None


class QuotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    status: str
    quotation_date: date
    customer_name: str
    address_lines: Optional[str] = None
    customer_gstin: Optional[str] = None
    customer_pan: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    subject_line: Optional[str] = None
    validity_days: int
    gst_rate: Decimal
    totals_label_set: str
    note_text: Optional[str] = None
    note_style: str
    terms: List[str]
    signatory_name: str
    signatory_designation: Optional[str] = None
    signatory_phones: Optional[str] = None
    signatory_email: Optional[str] = None
    subtotal: Decimal
    gst_amount: Decimal
    grand_total: Decimal
    pdf_url: Optional[str] = None
    duplicated_from_id: Optional[int] = None
    created_by_id: Optional[int] = None
    created_at: Optional[str] = None
    issued_at: Optional[str] = None
    items: List[QuotationItemOut] = []


# The Navapakam golden fixture doubles as the /docs example payload.
from ..services.quotation_fixtures import NAVAPAKAM_DRAFT  # noqa: E402

QuotationDraft.model_config["json_schema_extra"] = {"examples": [NAVAPAKAM_DRAFT]}
