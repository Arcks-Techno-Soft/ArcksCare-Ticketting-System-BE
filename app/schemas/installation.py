"""Pydantic schemas for the installation endpoints."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import UserOut


class InstallationAddressUpdate(BaseModel):
    """Site address / location. Line 1, city, state and pincode are required;
    line 2/3 and the geo coordinates are optional. Mirrors TicketCreate."""

    address_line1: str = Field(min_length=3, max_length=200)
    address_line2: Optional[str] = Field(default=None, max_length=200)
    address_line3: Optional[str] = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(min_length=2, max_length=80)
    pincode: str = Field(min_length=4, max_length=10)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)

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


class InstallationCreate(InstallationAddressUpdate):
    business_name: str = Field(min_length=2, max_length=200)
    business_category: str = Field(min_length=2, max_length=80)
    contact_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    email: Optional[str] = Field(default=None, max_length=200)
    invoice_number: str = Field(min_length=1, max_length=80)
    # Optional initial assignment. If supplied, the installation is created
    # already ASSIGNED to this user (engineer / self-assign).
    assigned_engineer_id: Optional[int] = None

    @field_validator("phone")
    @classmethod
    def _clean_phone(cls, v: str) -> str:
        cleaned = "".join(ch for ch in v if ch.isdigit() or ch == "+")
        if not cleaned or len(cleaned.lstrip("+")) < 7:
            raise ValueError("Enter a valid phone number")
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class InstallationAssignRequest(BaseModel):
    engineer_id: int


class InstallationInvoiceUpdate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=80)


class InstallationNoteIn(BaseModel):
    body: str = Field(min_length=2, max_length=4000)


class InstallationNoteAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    content_type: str
    size_bytes: int
    storage_url: str

    @field_validator("storage_url", mode="after")
    @classmethod
    def _resolve_storage_url(cls, v: str) -> str:
        from ..services.storage import get_storage  # noqa: WPS433
        try:
            return get_storage().public_url(v)
        except Exception:
            return v


class InstallationNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    body: str
    created_at: datetime
    author: UserOut
    attachments: List[InstallationNoteAttachmentOut] = []


class InstallationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    payload: Optional[dict] = None
    note: Optional[str] = None
    created_at: datetime
    actor: Optional[UserOut] = None


class InstallationResolutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_signer_name: Optional[str] = None
    customer_signed_at: Optional[datetime] = None
    engineer_signed_at: Optional[datetime] = None
    pdf_generated_at: Optional[datetime] = None
    customer_sign_token: str


class InstallationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    business_name: str
    business_category: str
    contact_name: str
    phone: str
    email: Optional[str] = None
    invoice_number: str
    status: str

    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    address_line3: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    created_by: Optional[UserOut] = None
    assigned_by: Optional[UserOut] = None
    assigned_engineer: Optional[UserOut] = None
    assigned_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    created_at: datetime

    resolution: Optional[InstallationResolutionOut] = None


class InstallationListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    business_name: str
    business_category: str
    contact_name: str
    phone: str
    invoice_number: str
    status: str
    city: Optional[str] = None
    state: Optional[str] = None
    created_by: Optional[UserOut] = None
    assigned_engineer: Optional[UserOut] = None
    created_at: datetime


class InstallationListPage(BaseModel):
    items: List[InstallationListItem]
    total: int
    limit: int
    offset: int
