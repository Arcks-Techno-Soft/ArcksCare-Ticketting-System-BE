"""Pydantic schemas for the warranty-registry endpoints."""
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import UserOut

# Durations offered in the UI dropdown. The API accepts any 1..120 month value
# so a custom duration the user types still validates.
ALLOWED_WARRANTY_MONTHS_MIN = 1
ALLOWED_WARRANTY_MONTHS_MAX = 120


class WarrantyCreate(BaseModel):
    product_name: str = Field(min_length=1, max_length=300)
    serial_number: str = Field(min_length=1, max_length=120)
    sale_date: date
    warranty_months: int = Field(
        ge=ALLOWED_WARRANTY_MONTHS_MIN, le=ALLOWED_WARRANTY_MONTHS_MAX
    )
    notes: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("product_name", "serial_number")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field is required")
        return v

    @field_validator("notes", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("sale_date")
    @classmethod
    def _not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Sale date cannot be in the future")
        return v


class WarrantyOut(BaseModel):
    id: int
    product_name: str
    serial_number: str
    sale_date: date
    warranty_months: int
    expiry_date: date
    is_active: bool
    notes: Optional[str]
    created_by: Optional[UserOut]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WarrantyLookupOut(BaseModel):
    """Lightweight result for a serial-number lookup (used by the ticket flow
    and the live duplicate check on the form)."""

    found: bool
    warranty: Optional[WarrantyOut] = None
