"""HTTP routes for ticket submission and lookup."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.ticket import Ticket
from ..schemas.ticket import (
    TicketCreate,
    TicketDuplicateResponse,
    TicketResponse,
)
from ..services.email import send_ticket_notification
from ..services.ticket_service import (
    create_ticket,
    find_recent_open_ticket,
    hours_remaining_in_window,
)

logger = logging.getLogger("arckscare.tickets")

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
def submit_ticket(
    payload: TicketCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Create a new support ticket.

    Returns 409 Conflict with the existing ticket's reference if the same
    serial number already has an open ticket created within the dedup window
    (default: 48 hours).
    """
    duplicate = find_recent_open_ticket(db, payload.serial_number)
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

    ticket = create_ticket(db, payload)
    logger.info("Created ticket %s for serial %s", ticket.reference, ticket.serial_number)

    # Fire-and-forget email - don't block the API response.
    background.add_task(send_ticket_notification, ticket)

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
