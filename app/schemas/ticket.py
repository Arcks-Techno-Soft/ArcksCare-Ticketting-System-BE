"""Pydantic request/response schemas for the ticket API.

These dropdown enums also drive the frontend select options - keep them in
sync with `frontend/lib/options.ts`.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class BusinessType(str, Enum):
    RESTAURANT = "Restaurant"
    HOTEL = "Hotel"
    RETAIL_STORE = "Retail Store"
    CAFE = "Cafe"
    CLOUD_KITCHEN = "Cloud Kitchen"
    OTHER = "Other"


class ProductCategory(str, Enum):
    POS = "POS Machine"
    PRINTER = "Printer"
    KDS = "Kitchen Display Screen"
    UPS = "UPS"
    KIOSK = "Kiosk"
    TABLET = "Tablet"
    MONITOR = "Monitor"
    CCTV = "CCTV"
    OTHER = "Other"


class IssueCategory(str, Enum):
    NO_POWER = "Not Powering On"
    DISPLAY = "Display Issue"
    PRINTING = "Printing Issue"
    CONNECTIVITY = "Connectivity"
    SOFTWARE = "Software Crash"
    PHYSICAL_DAMAGE = "Physical Damage"
    OTHER = "Other"


class SeverityIn(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TicketCreate(BaseModel):
    business_name: str = Field(min_length=2, max_length=200)
    contact_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    email: EmailStr
    business_type: BusinessType

    # Address
    address_line1: str = Field(min_length=3, max_length=200)
    address_line2: Optional[str] = Field(default=None, max_length=200)
    address_line3: Optional[str] = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(min_length=2, max_length=80)
    pincode: str = Field(min_length=4, max_length=10)

    # Optional geo - populated if customer drops a pin on the map
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)

    product_category: ProductCategory
    serial_number: str = Field(min_length=3, max_length=120)

    issue_category: IssueCategory
    severity: SeverityIn = SeverityIn.MEDIUM
    description: str = Field(min_length=20, max_length=4000)
    preferred_contact_time: Optional[str] = Field(default=None, max_length=60)

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, v: str) -> str:
        # Strip spaces, dashes, parens; keep leading + and digits.
        cleaned = "".join(ch for ch in v if ch.isdigit() or ch == "+")
        if not cleaned or len(cleaned.lstrip("+")) < 7:
            raise ValueError("Enter a valid phone number")
        return cleaned

    @field_validator("serial_number")
    @classmethod
    def _normalise_serial(cls, v: str) -> str:
        # Serial numbers are matched for dedup; strip whitespace + uppercase.
        return v.strip().upper()

    @field_validator("pincode")
    @classmethod
    def _normalise_pincode(cls, v: str) -> str:
        cleaned = "".join(ch for ch in v if ch.isdigit())
        if len(cleaned) < 4:
            raise ValueError("Enter a valid pincode")
        return cleaned

    @field_validator("address_line2", "address_line3", mode="before")
    @classmethod
    def _empty_string_to_none(cls, v):
        # Treat blank strings as None for optional address lines.
        if isinstance(v, str) and not v.strip():
            return None
        return v


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    storage_url: str
    content_type: str
    size_bytes: int

    @field_validator("storage_url", mode="after")
    @classmethod
    def _resolve_storage_url(cls, v: str) -> str:
        """Turn the raw stored value (URL path or object key) into a viewable URL.

        For local storage this is a no-op (the stored value is already a path).
        For Supabase, this mints a fresh signed URL each time the response is
        serialized.
        """
        # Import here to avoid a circular dependency at module load.
        from ..services.storage import get_storage  # noqa: WPS433

        try:
            return get_storage().public_url(v)
        except Exception:
            return v  # fall back to raw value rather than crashing


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    business_name: str
    contact_name: str
    email: EmailStr
    phone: str
    address_line1: str
    address_line2: Optional[str] = None
    address_line3: Optional[str] = None
    city: str
    state: str
    pincode: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    product_category: str
    serial_number: str
    issue_category: str
    severity: str
    status: str
    created_at: datetime
    attachments: List[AttachmentOut] = []


class TicketDuplicateResponse(BaseModel):
    """Returned when a duplicate ticket (same serial, within window) is detected."""

    duplicate: bool = True
    existing_reference: str
    existing_status: str
    created_at: datetime
    hours_until_new_allowed: float
    message: str
