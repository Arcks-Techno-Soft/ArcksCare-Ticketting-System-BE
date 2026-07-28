"""Pydantic schemas for the installation endpoints."""
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import SubEngineerOut, UserOut


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
    # Products to install — free text, one per line (name + quantity). Required.
    products_for_installation: str = Field(min_length=2, max_length=4000)
    # Optional date the installation is planned for on site. Drives the
    # upcoming-installation WhatsApp reminder to Super Admin/Admin/Managers.
    expected_installation_date: Optional[date] = None
    # Optional initial assignment. If supplied, the installation is created
    # already ASSIGNED to this user (engineer / self-assign).
    assigned_engineer_id: Optional[int] = None
    # Optional sales rep credited with sourcing this installation. Must be the
    # id of an active SALES user; validated in the create endpoint.
    sales_rep_id: Optional[int] = None

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

    @field_validator("expected_installation_date", mode="before")
    @classmethod
    def _empty_date_to_none(cls, v):
        # The form posts "" when the user leaves the date picker blank.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("products_for_installation")
    @classmethod
    def _require_products(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 2:
            raise ValueError("List at least one product to install")
        return cleaned


class InstallationAssignRequest(BaseModel):
    engineer_id: int


class InstallationCustomerUpdate(BaseModel):
    """Editable customer / contact details. Assignee / Admin / Manager may
    correct these before the installation is CLOSED. Mirrors the customer half
    of InstallationCreate, reusing the same normalisers."""

    business_name: str = Field(min_length=2, max_length=200)
    business_category: str = Field(min_length=2, max_length=80)
    contact_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    email: Optional[str] = Field(default=None, max_length=200)

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


class InstallationInvoiceUpdate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=80)


class InstallationExpectedDateUpdate(BaseModel):
    """Set or clear the planned on-site date. Pass null to clear it."""

    expected_installation_date: Optional[date] = None

    @field_validator("expected_installation_date", mode="before")
    @classmethod
    def _empty_date_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class InstallationSalesRepUpdate(BaseModel):
    # Pass null to clear the credited sales rep, or an active SALES user's id.
    sales_rep_id: Optional[int] = None


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


class InstallationInvoiceDocumentOut(BaseModel):
    """The optional uploaded invoice document. `storage_url` is resolved into a
    viewable link (signed URL on Supabase, /uploads path locally)."""

    model_config = ConfigDict(from_attributes=True)

    filename: str
    content_type: str
    size_bytes: int
    storage_url: str
    uploaded_at: Optional[datetime] = None

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


class InstallationAttemptOut(BaseModel):
    """A work attempt with its notes (and their photos) nested inside."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    attempt_number: int
    started_at: datetime
    ended_at: Optional[datetime] = None
    started_by: Optional[UserOut] = None
    notes: List[InstallationNoteOut] = []


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
    # Set when an optional customer photo was captured at sign-off.
    customer_photo_captured_at: Optional[datetime] = None
    # Viewable URL for the customer photo, resolved from the stored key.
    customer_photo_url: Optional[str] = Field(
        default=None, validation_alias="customer_photo_storage_key"
    )
    engineer_signed_at: Optional[datetime] = None
    pdf_generated_at: Optional[datetime] = None
    customer_sign_token: str

    @field_validator("customer_photo_url", mode="after")
    @classmethod
    def _resolve_photo_url(cls, v: Optional[str]) -> Optional[str]:
        """Turn the stored key into a viewable URL (no-op locally, signed URL on
        Supabase). Falls back to the raw value rather than crashing."""
        if not v:
            return v
        from ..services.storage import get_storage  # noqa: WPS433

        try:
            return get_storage().public_url(v)
        except Exception:
            return v


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
    products_for_installation: Optional[str] = None
    expected_installation_date: Optional[date] = None
    invoice_document: Optional[InstallationInvoiceDocumentOut] = None
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
    sales_rep: Optional[UserOut] = None
    assigned_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    # On hold — an overlay on `status`, so `status` still reads NEW/ASSIGNED
    # while parked. `on_hold` is the flag the UIs gate on.
    on_hold: bool = False
    held_at: Optional[datetime] = None
    held_by: Optional[UserOut] = None
    hold_reason: Optional[str] = None

    created_at: datetime

    resolution: Optional[InstallationResolutionOut] = None
    attempts: List[InstallationAttemptOut] = []
    # Off-field contractors attending this installation.
    sub_engineers: List[SubEngineerOut] = []


class InstallationListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    business_name: str
    business_category: str
    contact_name: str
    phone: str
    invoice_number: str
    expected_installation_date: Optional[date] = None
    status: str
    city: Optional[str] = None
    state: Optional[str] = None
    created_by: Optional[UserOut] = None
    assigned_engineer: Optional[UserOut] = None
    sales_rep: Optional[UserOut] = None
    # On-hold overlay — the list renders a badge next to the status pill.
    on_hold: bool = False
    hold_reason: Optional[str] = None
    created_at: datetime


class InstallationListPage(BaseModel):
    items: List[InstallationListItem]
    total: int
    limit: int
    offset: int
