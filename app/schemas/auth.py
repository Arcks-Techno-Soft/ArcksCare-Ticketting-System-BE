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

    Workload counts only jobs still needing field work — a RESOLVED ticket or
    a COMPLETED installation is finished bar the customer signature and the
    PDF, so it no longer occupies the engineer:

    Jobs on hold are excluded from both counts as well — the engineer can't act
    on a parked job, so it must not make them look busy.

    * `open_service_call_count` — tickets in ASSIGNED / ACCEPTED / RESOLVING
      (not soft-deleted, not on hold) where the user is the primary assignee OR
      an additional (co-)engineer. Counted once per ticket either way.
    * `open_installation_count` — installations in NEW / ASSIGNED, not on hold.
    * `open_ticket_count` — the two added together. Kept under its original
      name so existing clients keep reading the total from the same field;
      the pickers show it as "N open jobs (SC-x / INS-y)".
    """

    open_service_call_count: int = 0
    open_installation_count: int = 0
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
    # Creating users is a Super-Admin-only action, so any tier may be created,
    # including ADMIN and (another) SUPER_ADMIN.
    role: str = Field(pattern=r"^(SUPER_ADMIN|ADMIN|MANAGER|ENGINEER|SALES)$")
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


class UpdateUserRoleRequest(BaseModel):
    # Change a user's role. Super-Admin-only action (see the endpoint), so any
    # tier is assignable.
    role: str = Field(pattern=r"^(SUPER_ADMIN|ADMIN|MANAGER|ENGINEER|SALES)$")


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


class TicketSalesRepUpdate(BaseModel):
    # Credit a sales rep with the service call, or clear it with null.
    sales_rep_id: Optional[int] = None


class ForceCloseRequest(BaseModel):
    # Admin/Owner override-close. Reason is mandatory for the audit trail.
    reason: str = Field(min_length=3, max_length=500)


class DeleteTicketRequest(BaseModel):
    # Soft-delete reason is optional but recorded when provided.
    reason: Optional[str] = Field(default=None, max_length=500)


class HoldRequest(BaseModel):
    # Manager/Admin parks a ticket or installation. Reason is mandatory — it's
    # what the inbox badge and the audit trail show.
    reason: str = Field(min_length=3, max_length=500)


class ResumeRequest(BaseModel):
    # Lifting a hold needs no reason; an optional note lands in the event log.
    note: Optional[str] = Field(default=None, max_length=500)


class UpdateWarrantyRequest(BaseModel):
    warranty_status: str  # UNKNOWN / UNDER_WARRANTY / OUT_OF_WARRANTY / AMC


class UpdateSeverityRequest(BaseModel):
    severity: str  # LOW / MEDIUM / HIGH / CRITICAL


class UpdateServiceTypeRequest(BaseModel):
    service_type: str  # SITE_VISIT / REMOTE_SUPPORT / THIRD_PARTY_SUPPORT


class UpdateThirdPartyInfoRequest(BaseModel):
    """Third-party support details. All optional here so the engineer can save
    progressively; device_name + issue_info are enforced at close time."""
    third_party_device_name: Optional[str] = Field(default=None, max_length=120)
    third_party_issue_info: Optional[str] = Field(default=None, max_length=5000)
    third_party_ticket_ref: Optional[str] = Field(default=None, max_length=120)


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


class BusinessNameSuggestion(BaseModel):
    """One business-name autocomplete hit. `business_type` is the category last
    recorded for that name (from a past ticket or installation) so the staff
    form can pre-fill it; it may be empty if none was ever stored."""

    business_name: str
    business_type: str = ""


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


class VerifyPaymentRequest(BaseModel):
    """Admin confirmation that collected money actually arrived. The optional
    note is recorded on the audit timeline (e.g. the bank reference)."""
    note: Optional[str] = Field(default=None, max_length=500)


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
    # Minimum service fee for this ticket (0 = no floor). Non-Admins can't set
    # below this; an Admin can.
    service_fee_min_inr: int = 0
    spares_list_price_total_inr: int
    spares_billable_total_inr: int
    grand_total_inr: int
    items: list[ChargeLineItem]


class AddWorkNoteRequest(BaseModel):
    body: str = Field(min_length=2, max_length=4000)


class ResolveRequest(BaseModel):
    summary: str = Field(min_length=10, max_length=4000, description="Engineer's resolution write-up.")
