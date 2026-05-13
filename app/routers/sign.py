"""Public customer-signing endpoints.

No JWT auth — knowledge of the unique sign token is the auth factor. Tokens
are 32-byte random URL-safe strings and expire after a configurable TTL
(default 30 days).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.signing import (
    get_resolution_by_token,
    record_customer_signature,
)

logger = logging.getLogger("arckscare.sign")

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
