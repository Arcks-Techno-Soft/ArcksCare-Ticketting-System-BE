"""Pydantic schemas for the installation endpoints."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import UserOut


class InstallationCreate(BaseModel):
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
    assigned_engineer: Optional[UserOut] = None
    created_at: datetime


class InstallationListPage(BaseModel):
    items: List[InstallationListItem]
    total: int
    limit: int
    offset: int
