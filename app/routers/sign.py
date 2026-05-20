"""Public customer-signing endpoints.

No JWT auth — knowledge of the unique sign token is the auth factor. Tokens
are 32-byte random URL-safe strings and expire after a configurable TTL
(default 30 days).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.signing import (
    get_resolution_by_token,
    record_customer_signature,
    record_field_signatures,
)

logger = logging.getLogger("skposcare.sign")

router = APIRouter(prefix="/api/v1/sign", tags=["public-sign"])


class PublicResolutionDoc(BaseModel):
    """Minimal payload returned to the customer on the signing page.

    We deliberately do NOT expose internal IDs or staff names beyond the
    engineer who handled their ticket.
    """
    model_config = ConfigDict(from_attributes=True)

    reference: str
    business_name: str
    contact_name: str
    address_line1: str
    address_line2: Optional[str] = None
    address_line3: Optional[str] = None
    city: str
    state: str
    pincode: str
    product_category: str
    serial_number: str
    issue_category: str
    description: Optional[str] = None
    resolution_summary: Optional[str] = None
    engineer_name: Optional[str] = None
    resolved_at: Optional[datetime] = None
    customer_signed_at: Optional[datetime] = None  # so the page can show "already signed"


class PublicSubEngineer(BaseModel):
    """One sub-engineer the field-signer can identify themselves as."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    location: str


class FieldSignDoc(PublicResolutionDoc):
    """Payload for the remote field-signing page (sub-engineer collects both
    the customer's signature and their own)."""

    sub_engineers: List[PublicSubEngineer] = []
    engineer_signed_at: Optional[datetime] = None
    # True once both signatures are recorded — the page shows a completed state.
    completed: bool = False


def _resolution_doc_fields(ticket, resolution) -> dict:
    """Shared field mapping for the public resolution payloads."""
    return dict(
        reference=ticket.reference,
        business_name=ticket.business_name,
        contact_name=ticket.contact_name,
        address_line1=ticket.address_line1,
        address_line2=ticket.address_line2,
        address_line3=ticket.address_line3,
        city=ticket.city,
        state=ticket.state,
        pincode=ticket.pincode,
        product_category=ticket.product_category,
        serial_number=ticket.serial_number,
        issue_category=ticket.issue_category,
        description=ticket.description,
        resolution_summary=ticket.resolution_summary,
        engineer_name=ticket.assigned_engineer.name if ticket.assigned_engineer else None,
        resolved_at=ticket.resolved_at,
        customer_signed_at=resolution.customer_signed_at,
    )


def _field_doc(ticket, resolution) -> "FieldSignDoc":
    return FieldSignDoc(
        **_resolution_doc_fields(ticket, resolution),
        sub_engineers=[PublicSubEngineer.model_validate(s) for s in ticket.sub_engineers],
        engineer_signed_at=resolution.engineer_signed_at,
        completed=resolution.engineer_signed_at is not None,
    )


@router.get("/{token}", response_model=PublicResolutionDoc)
def fetch_resolution(token: str, db: Session = Depends(get_db)):
    ticket, resolution = get_resolution_by_token(db, token)
    return PublicResolutionDoc(
        reference=ticket.reference,
        business_name=ticket.business_name,
        contact_name=ticket.contact_name,
        address_line1=ticket.address_line1,
        address_line2=ticket.address_line2,
        address_line3=ticket.address_line3,
        city=ticket.city,
        state=ticket.state,
        pincode=ticket.pincode,
        product_category=ticket.product_category,
        serial_number=ticket.serial_number,
        issue_category=ticket.issue_category,
        description=ticket.description,
        resolution_summary=ticket.resolution_summary,
        engineer_name=ticket.assigned_engineer.name if ticket.assigned_engineer else None,
        resolved_at=ticket.resolved_at,
        customer_signed_at=resolution.customer_signed_at,
    )


@router.post("/{token}/customer", response_model=PublicResolutionDoc, status_code=201)
async def submit_customer_signature(
    token: str,
    signer_name: str = Form(..., min_length=2, max_length=120),
    signature: UploadFile = File(..., description="PNG of the customer's signature"),
    db: Session = Depends(get_db),
):
    ticket, resolution = get_resolution_by_token(db, token)
    image_bytes = await signature.read()
    record_customer_signature(
        db, ticket, resolution,
        signer_name=signer_name,
        image_bytes=image_bytes,
        content_type=signature.content_type or "image/png",
    )
    db.refresh(resolution)
    return PublicResolutionDoc(**_resolution_doc_fields(ticket, resolution))


# ----------------------- remote field signing ---------------------------- #

@router.get("/{token}/field", response_model=FieldSignDoc)
def fetch_field_resolution(token: str, db: Session = Depends(get_db)):
    """Public payload for the sub-engineer's no-login signing page."""
    ticket, resolution = get_resolution_by_token(db, token)
    if resolution.field_sign_link_generated_at is None:
        raise HTTPException(status_code=404, detail="This is not a field-signing link")
    return _field_doc(ticket, resolution)


@router.post("/{token}/field", response_model=FieldSignDoc, status_code=201)
async def submit_field_signatures(
    token: str,
    sub_engineer_id: int = Form(..., description="ID of the signing sub-engineer"),
    customer_signer_name: str = Form(..., min_length=2, max_length=120),
    customer_signature: UploadFile = File(..., description="PNG of the customer's signature"),
    engineer_signature: UploadFile = File(..., description="PNG of the sub-engineer's signature"),
    db: Session = Depends(get_db),
):
    """Record both signatures collected by the sub-engineer off-site. On
    success the resolution PDF is generated and the ticket transitions
    RESOLVED → CLOSED. The link is single-use — re-submitting returns 409."""
    ticket, resolution = get_resolution_by_token(db, token)
    customer_bytes = await customer_signature.read()
    engineer_bytes = await engineer_signature.read()
    record_field_signatures(
        db, ticket, resolution,
        sub_engineer_id=sub_engineer_id,
        customer_signer_name=customer_signer_name,
        customer_image_bytes=customer_bytes,
        engineer_image_bytes=engineer_bytes,
        customer_content_type=customer_signature.content_type or "image/png",
        engineer_content_type=engineer_signature.content_type or "image/png",
    )
    db.refresh(resolution)
    return _field_doc(ticket, resolution)
