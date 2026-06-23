"""Pydantic schemas for auth and admin endpoints."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    role: str
    active: bool
    email: Optional[str] = None
    # District an Engineer covers (NULL for Admin/Manager).
    district: Optional[str] = None
    # When True, this user can be credited as a sales rep on installations,
    # regardless of role. SALES-role users are eligible without this flag.
    is_sales_rep: bool = False


class EngineerOption(UserOut):
    """An engineer for the 'assign to' picker, annotated with their current
    workload so the UI can surface the least-busy (and recommend free) ones.

    `open_ticket_count` = open workload assigned to this engineer: active
    tickets (not CLOSED, not soft-deleted) plus pending installations (not
    CLOSED) — i.e. what's currently on their plate.
    """

    open_ticket_count: int = 0


class CreateUserRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    phone: str = Field(min_length=7, max_length=20)
    email: str = Field(min_length=3, max_length=200)
    # Admin picks both username + password — no auto-generation. Username must
    # be 3-50 chars, lowercased a-z/0-9/dot/underscore/hyphen so it can sit in
    # URLs and logs without escaping.
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=200)
    # Accepted: "MANAGER" (admin-tier), "ENGINEER", or "SALES".
    role: str = Field(pattern=r"^(MANAGER|ENGINEER|SALES)$")
    # District an Engineer covers — used to match incoming tickets by city.
    # Optional; only meaningful for ENGINEER accounts.
    district: Optional[str] = Field(default=None, max_length=80)
    # Optionally flag this user as also a sales rep (creditable on installations),
    # independent of role. Defaults off.
    is_sales_rep: bool = False

    @field_validator("district", mode="before")
    @classmethod
    def _blank_district_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class CreateUserResponse(BaseModel):
    user: UserOut


class UpdateUserActiveRequest(BaseModel):
    active: bool


class UpdateUserSalesRepRequest(BaseModel):
    is_sales_rep: bool


class RegisterPushTokenRequest(BaseModel):
    # Expo push token, e.g. "ExponentPushToken[...]".
    token: str = Field(min_length=10, max_length=255)
    platform: Optional[str] = Field(default=None, max_length=20)  # "ios" | "android"


class UnregisterPushTokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=255)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut


# ----------------------------- Admin action shapes ---------------------- #

class AssignEngineerRequest(BaseModel):
    engineer_id: int


class ForceCloseRequest(BaseModel):
    # Admin/Owner override-close. Reason is mandatory for the audit trail.
    reason: str = Field(min_length=3, max_length=500)


class DeleteTicketRequest(BaseModel):
    # Soft-delete reason is optional but recorded when provided.
    reason: Optional[str] = Field(default=None, max_length=500)


class UpdateWarrantyRequest(BaseModel):
    warranty_status: str  # UNKNOWN / UNDER_WARRANTY / OUT_OF_WARRANTY / AMC


class UpdateSeverityRequest(BaseModel):
    severity: str  # LOW / MEDIUM / HIGH / CRITICAL


class UpdateServiceTypeRequest(BaseModel):
    service_type: str  # SITE_VISIT / REMOTE_SUPPORT


class TicketEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    payload: Optional[dict] = None
    note: Optional[str] = None
    created_at: datetime
    actor: Optional[UserOut] = None


class WorkNoteAttachmentOut(BaseModel):
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


class WorkNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    body: str
    created_at: datetime
    author: UserOut
    attachments: list[WorkNoteAttachmentOut] = []


class TicketAttemptOut(BaseModel):
    """A work attempt with its notes (and their photos) nested inside."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    attempt_number: int
    started_at: datetime
    ended_at: Optional[datetime] = None
    started_by: Optional[UserOut] = None
    notes: list[WorkNoteOut] = []


class SubEngineerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    location: str
    # Fee paid to this outsourced contractor for this ticket (INR). NULL until set.
    fee_inr: Optional[int] = None
    created_at: datetime
    created_by: Optional[UserOut] = None


class AdditionalEngineerOut(BaseModel):
    """An extra system user attending the same visit (view + notified only)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    engineer: UserOut
    added_by: Optional[UserOut] = None
    added_at: datetime


class UpdateSubEngineerFeeRequest(BaseModel):
    fee_inr: int = Field(ge=0, le=10_000_000)


class AddSubEngineerRequest(BaseModel):
    """Add a sub-engineer to a ticket.

    Either pass `roster_id` to pick an existing roster contact, or pass
    `name` + `phone` + `location` for a brand-new contact (which is also
    added to the district roster).
    """
    roster_id: Optional[int] = None
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    phone: Optional[str] = Field(default=None, min_length=7, max_length=20)
    location: Optional[str] = Field(default=None, min_length=2, max_length=120)


class SubEngineerRosterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    district: str
    active: bool
    created_at: datetime
    created_by: Optional[UserOut] = None


class CreateRosterSubEngineerRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    district: str = Field(min_length=2, max_length=80)


class UpdateRosterSubEngineerRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    phone: Optional[str] = Field(default=None, min_length=7, max_length=20)
    district: Optional[str] = Field(default=None, min_length=2, max_length=80)
    active: Optional[bool] = None


class SpareCatalogItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_category: str
    name: str
    default_price_inr: int


class TicketSpareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    catalog_id: Optional[int] = None
    name: str
    unit_price_inr: int
    quantity: int
    created_at: datetime
    created_by: Optional[UserOut] = None


class AddTicketSpareRequest(BaseModel):
    # Either pick from the catalog (preferred)…
    catalog_id: Optional[int] = None
    # …or supply a free-form name + price (ad-hoc parts).
    name: Optional[str] = Field(default=None, max_length=160)
    unit_price_inr: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    quantity: int = Field(default=1, ge=1, le=999)


class UpdateTicketSpareRequest(BaseModel):
    unit_price_inr: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    quantity: Optional[int] = Field(default=None, ge=1, le=999)


class UpdateServiceFeeRequest(BaseModel):
    service_fee_inr: int = Field(ge=0, le=10_000_000)


class CollectPaymentRequest(BaseModel):
    """Record the amount actually collected for an out-of-warranty ticket."""
    amount_collected_inr: int = Field(ge=0, le=10_000_000)


class ChargeLineItem(BaseModel):
    id: int
    catalog_id: Optional[int] = None
    name: str
    unit_price_inr: int
    quantity: int
    line_total_inr: int
    billable: bool


class ChargesSummary(BaseModel):
    warranty_status: str
    is_warranty: bool
    service_fee_inr: int
    service_fee_billable_inr: int
    spares_list_price_total_inr: int
    spares_billable_total_inr: int
    grand_total_inr: int
    items: list[ChargeLineItem]


class AddWorkNoteRequest(BaseModel):
    body: str = Field(min_length=2, max_length=4000)


class ResolveRequest(BaseModel):
    summary: str = Field(min_length=10, max_length=4000, description="Engineer's resolution write-up.")
