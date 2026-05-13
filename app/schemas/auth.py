"""Pydantic schemas for auth and admin endpoints."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    role: str
    active: bool
    email: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut


# ----------------------------- Admin action shapes ---------------------- #

class AssignEngineerRequest(BaseModel):
    engineer_id: int


class UpdateWarrantyRequest(BaseModel):
    warranty_status: str  # UNDER_WARRANTY / OUT_OF_WARRANTY / UNKNOWN


class UpdateSeverityRequest(BaseModel):
    severity: str  # LOW / MEDIUM / HIGH / CRITICAL


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


class WorkNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    body: str
    created_at: datetime
    author: UserOut


class AddWorkNoteRequest(BaseModel):
    body: str = Field(min_length=2, max_length=4000)


class ResolveRequest(BaseModel):
    summary: str = Field(min_length=10, max_length=4000, description="Engineer's resolution write-up.")
