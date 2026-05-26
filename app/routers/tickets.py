"""HTTP routes for ticket submission and lookup."""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.ticket import Ticket, TicketAttachment
from ..schemas.ticket import (
    TicketCreate,
    TicketDuplicateResponse,
    TicketResponse,
)
from ..services.email import send_ticket_notification
from ..services.storage import cleanup_ticket_files, save_uploads
from ..services.whatsapp import send_new_ticket_alert
from ..services.ticket_service import (
    create_ticket,
    find_recent_open_ticket,
    hours_remaining_in_window,
)

logger = logging.getLogger("skposcare.tickets")

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


@router.post(
    "",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {
            "model": TicketDuplicateResponse,
            "description": "An open ticket already exists for this serial number within the dedup window.",
        }
    },
)
async def submit_ticket(
    background: BackgroundTasks,
    payload: str = Form(..., description="JSON-encoded TicketCreate"),
    files: Optional[List[UploadFile]] = File(default=None, description="Optional attachments (JPG/PNG/GIF/MP4/MOV, <=50MB each)"),
    db: Session = Depends(get_db),
):
    """Create a new support ticket with optional file attachments.

    Accepts multipart/form-data:
      - `payload`: a JSON string matching the TicketCreate schema
      - `files`:   zero or more uploaded files

    Returns 409 Conflict with the existing ticket's reference if the same
    serial number already has an open ticket created within the dedup window
    (default: 48 hours).
    """
    # 1) Parse + validate the JSON payload
    try:
        data = TicketCreate.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.errors(),
        )

    # 2) Dedup check before doing any disk work
    duplicate = find_recent_open_ticket(db, data.serial_number)
    if duplicate is not None:
        remaining = hours_remaining_in_window(duplicate)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=TicketDuplicateResponse(
                existing_reference=duplicate.reference,
                existing_status=duplicate.status,
                created_at=duplicate.created_at,
                hours_until_new_allowed=round(remaining, 1),
                message=(
                    f"We already have ticket {duplicate.reference} open for this device. "
                    f"Our team will reach out shortly. You can raise a new ticket "
                    f"in about {remaining:.1f} hour(s) if the issue isn't resolved."
                ),
            ).model_dump(mode="json"),
        )

    # 3) Create the ticket row
    ticket = create_ticket(db, data)
    logger.info("Created ticket %s for serial %s", ticket.reference, ticket.serial_number)

    # 4) Save uploads (if any) and link to the ticket
    real_files = [f for f in (files or []) if f and (f.filename or "").strip()]
    if real_files:
        try:
            saved = save_uploads(real_files, ticket.reference)
        except HTTPException:
            # Upload failed - keep the ticket but clean any partials and rethrow
            cleanup_ticket_files(ticket.reference)
            raise
        for meta in saved:
            db.add(TicketAttachment(ticket_id=ticket.id, **meta))
        db.commit()
        db.refresh(ticket)
        logger.info("Attached %d file(s) to ticket %s", len(saved), ticket.reference)

    # 5) Fire-and-forget email
    background.add_task(send_ticket_notification, ticket)
    background.add_task(send_new_ticket_alert, ticket.id)

    return ticket


@router.get("/{reference}", response_model=TicketResponse)
def get_ticket_by_reference(reference: str, db: Session = Depends(get_db)):
    """Look up a ticket by its human reference (e.g. AC-2026-00042).

    Phase 1 has no auth - this is a read-only lookup intended for the
    customer-facing "track my ticket" page (Phase 2). Knowledge of the
    reference is the auth factor for now.
    """
    ticket = (
        db.query(Ticket).filter(Ticket.reference == reference.strip().upper()).one_or_none()
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.get("", response_model=list[TicketResponse])
def list_recent_tickets(limit: int = 20, db: Session = Depends(get_db)):
    """Internal helper for development - lists most recent tickets.

    NOTE: This endpoint is not safe to expose publicly in production. Lock
    it behind admin auth before deploying.
    """
    tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).limit(min(limit, 100)).all()
    return tickets
