"""Pydantic request/response schemas for the ticket API.

These dropdown enums also drive the frontend select options - keep them in
sync with `frontend/lib/options.ts`.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .auth import AdditionalEngineerOut, SubEngineerOut, TicketAttemptOut, UserOut


class BusinessType(str, Enum):
    """Canonical/suggested business categories shown in the dropdown. Kept in
    sync with the frontend BUSINESS_TYPES list. NOTE: `business_type` on
    TicketCreate is a free string (not enum-validated) so that customers who
    pick "Other" can type their own category — these are the suggested values."""
    RESTAURANT = "Restaurant"
    HOTEL = "Hotel"
    RETAIL_STORE = "Retail Store"
    CAFE = "Cafe"
    CLOUD_KITCHEN = "Cloud Kitchen"
    FOOD_COURT = "Food Court"
    ICE_CREAM_PARLOUR = "Ice Cream Parlour"
    PARTNER = "Partner"
    OTHER = "Other"


class ContactPersonProfile(str, Enum):
    """Suggested roles for the contact person shown in the dropdown. Kept in
    sync with the frontend CONTACT_PERSON_PROFILES list. NOTE:
    `contact_person_profile` on TicketCreate is a free string (not enum-validated)
    so that a contact who picks "Other" can type their own role."""
    OWNER = "Owner"
    MANAGER = "Manager"
    CASHIER = "Cashier"
    CHEF = "Chef"
    CAPTAIN_WAITER = "Captain/Waiter"
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
    phone: str = Field(min_length=10, max_length=20)
    email: Optional[EmailStr] = None
    # Free text (not enum-validated): the form offers BusinessType as suggestions,
    # but a customer who picks "Other" types their own category, which is stored
    # here verbatim.
    business_type: str = Field(min_length=2, max_length=60)
    # The contact person's role. Free text (not enum-validated): the form offers
    # ContactPersonProfile as suggestions, but a contact who picks "Other" types
    # their own role, stored here verbatim. Optional at the API layer so other
    # clients keep working; the public web form requires it.
    contact_person_profile: Optional[str] = Field(default=None, max_length=60)

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

    @field_validator("business_type")
    @classmethod
    def _clean_business_type(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Tell us your business category")
        return v

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, v: str) -> str:
        # Indian mobile: keep digits only, strip a leading +91 / 91 / 0
        # trunk prefix, then require exactly 10 digits starting 6-9.
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) != 10 or digits[0] not in "6789":
            raise ValueError("Enter a valid 10-digit mobile number")
        return digits

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

    @field_validator("address_line2", "address_line3", "contact_person_profile", mode="before")
    @classmethod
    def _empty_string_to_none(cls, v):
        # Treat blank strings as None for optional fields.
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
    contact_person_profile: Optional[str] = None
    email: Optional[EmailStr] = None
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
    description: Optional[str] = None
    severity: str
    status: str
    warranty_status: str
    service_type: str

    # Payment tracking. NULL payment_status = legacy ticket (never gated).
    # PENDING / COLLECTED for new-flow tickets. `payment_required` is the
    # computed gate (out-of-warranty, or covered + charges) the UIs key off so
    # they don't re-derive the rule.
    payment_status: Optional[str] = None
    payment_required: bool = False
    payment_amount_inr: Optional[int] = None
    payment_collected_at: Optional[datetime] = None
    payment_collected_by: Optional[UserOut] = None
    # Money breakdown for partial-payment tracking: total billable, collected so
    # far, and what's still outstanding. The ticket can only CLOSE when pending
    # reaches ₹0 (i.e. paid in full).
    amount_due_inr: int = 0
    amount_collected_inr: int = 0
    amount_pending_inr: int = 0

    # Who raised the ticket. NULL = customer self-submitted via the public web
    # form; set = staff member (typically an Engineer) who submitted on the
    # customer's behalf from the app.
    raised_by: Optional[UserOut] = None

    # Assignment context (Phase 2.2+) - nested user objects via the ORM relationship.
    acknowledged_by: Optional[UserOut] = None
    acknowledged_at: Optional[datetime] = None
    assigned_by: Optional[UserOut] = None
    assigned_engineer: Optional[UserOut] = None
    assigned_at: Optional[datetime] = None

    # Resolution context (Phase 2.3+)
    accepted_at: Optional[datetime] = None
    resolving_started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution_summary: Optional[str] = None

    # Signing + PDF (Phase 2.4+) — only ever populated once resolve() runs.
    resolution: Optional["ResolutionOut"] = None

    # Optional list of temporary field contractors brought in to help.
    sub_engineers: List[SubEngineerOut] = []

    # Extra system users (engineers) attending the same visit. View + notified
    # only; the primary `assigned_engineer` still drives the workflow.
    additional_engineers: List[AdditionalEngineerOut] = []

    # Work attempts (visits across multiple days), each with its nested notes.
    attempts: List[TicketAttemptOut] = []

    created_at: datetime
    attachments: List[AttachmentOut] = []


class ClosePreviewOut(BaseModel):
    """Summary + 'what's still pending' for the force-close confirmation modal."""
    model_config = ConfigDict(from_attributes=True)

    reference: str
    business_name: str
    status: str
    warranty_status: str
    service_type: str
    assigned_engineer: Optional[UserOut] = None
    additional_engineers: List[AdditionalEngineerOut] = []
    acknowledged_at: Optional[datetime] = None
    assigned_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    resolving_started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    # Computed (set by the endpoint) — everything still incomplete.
    pending: List[str] = []


class TicketListPage(BaseModel):
    """Paginated envelope around TicketListItem rows."""
    items: List["TicketListItem"]
    total: int
    limit: int
    offset: int


class TicketListItem(BaseModel):
    """Lightweight projection for the admin inbox table.

    Excludes lazy-loaded relationships (attachments, resolution, sub_engineers)
    so listing 100 tickets stays a single SQL round-trip instead of 300+.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    business_name: str
    # Customer contact + their role — used to tag customer-raised tickets in the
    # admin inbox ("Raised by customer — <name> — <profile>").
    contact_name: str
    contact_person_profile: Optional[str] = None
    city: str
    state: str
    product_category: str
    serial_number: str
    issue_category: str
    severity: str
    status: str
    warranty_status: str
    service_type: str
    raised_by: Optional[UserOut] = None
    assigned_engineer: Optional[UserOut] = None
    created_at: datetime


class ResolutionMediaOut(BaseModel):
    """A photo/video attached at field sign-off, with a viewable URL."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str  # "photo" | "video"
    filename: str
    content_type: str
    size_bytes: int
    storage_url: str
    uploaded_at: datetime

    @field_validator("storage_url", mode="after")
    @classmethod
    def _resolve_media_url(cls, v: str) -> str:
        from ..services.storage import get_storage  # noqa: WPS433

        try:
            return get_storage().public_url(v)
        except Exception:
            return v


class ResolutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_signer_name: Optional[str] = None
    customer_signed_at: Optional[datetime] = None
    # Set when an optional customer photo was captured at sign-off.
    customer_photo_captured_at: Optional[datetime] = None
    # Viewable URL for the customer photo, resolved from the stored key (the raw
    # key itself stays unexposed, like the signature keys). Mirrors AttachmentOut.
    customer_photo_url: Optional[str] = Field(
        default=None, validation_alias="customer_photo_storage_key"
    )
    engineer_signed_at: Optional[datetime] = None
    # Sub-engineer's name when the resolution was signed via the remote link.
    engineer_signer_name: Optional[str] = None
    pdf_generated_at: Optional[datetime] = None
    # Set once the remote field-signing link is generated — locks on-site signing.
    field_sign_link_generated_at: Optional[datetime] = None
    # Signing token. The frontend builds the field-sign URL from this.
    customer_sign_token: str
    # Optional photos/videos the sub-engineer attached at field sign-off.
    media: List[ResolutionMediaOut] = []

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

# Update forward refs after the referenced classes are defined.
TicketResponse.model_rebuild()
TicketListPage.model_rebuild()


class TicketDuplicateResponse(BaseModel):
    """Returned when a duplicate ticket (same serial, within window) is detected."""

    duplicate: bool = True
    existing_reference: str
    existing_status: str
    created_at: datetime
    hours_until_new_allowed: float
    message: str


# ----------------------------- Shipments --------------------------------- #

class ShipmentItemIn(BaseModel):
    """One line in a "Ship parts" request."""
    catalog_id: Optional[int] = None
    name: Optional[str] = Field(default=None, max_length=160)
    quantity: int = Field(default=1, ge=1, le=999)


class ShipmentCreate(BaseModel):
    courier_name: str = Field(min_length=2, max_length=120)
    tracking_id: Optional[str] = Field(default=None, max_length=120)
    # If omitted the server stamps datetime.now(UTC).
    departed_at: Optional[datetime] = None
    items: List[ShipmentItemIn] = Field(default_factory=list)

    @field_validator("tracking_id", mode="before")
    @classmethod
    def _blank_tracking_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class ShipmentItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    catalog_id: Optional[int] = None
    name: str
    quantity: int


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    courier_name: str
    tracking_id: Optional[str] = None
    departed_at: datetime
    delivered_at: Optional[datetime] = None
    created_at: datetime
    created_by: Optional[UserOut] = None
    items: List[ShipmentItemOut] = []
