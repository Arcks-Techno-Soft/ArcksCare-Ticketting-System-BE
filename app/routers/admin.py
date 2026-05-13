"""Admin endpoints for the internal staff app.

All routes here require a valid JWT via `get_current_user`. Phase 2.2 adds
acknowledge / assign / warranty actions, plus engineer + event listing.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.ticket import Ticket, TicketEvent, WorkNote
from ..models.user import User, UserRole
from ..schemas.auth import (
    AddWorkNoteRequest,
    AssignEngineerRequest,
    ResolveRequest,
    TicketEventOut,
    UpdateSeverityRequest,
    UpdateWarrantyRequest,
    UserOut,
    WorkNoteOut,
)
from ..schemas.ticket import TicketResponse
from ..services.auth import get_current_user
from ..services.email import send_engineer_assignment
from ..services.signing import (
    record_customer_signature_via_engineer,
    record_engineer_signature,
)
from ..services.storage import get_storage
from ..services.ticket_workflow import (
    accept,
    acknowledge,
    add_work_note,
    assign_engineer,
    resolve,
    start_work,
    update_severity,
    update_warranty,
)

logger = logging.getLogger("arckscare.admin")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# --------------------------- profile + lookups -------------------------- #

@router.get("/me", response_model=UserOut)
def my_profile(user: User = Depends(get_current_user)):
    return user


@router.get("/engineers", response_model=List[UserOut])
def list_engineers(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Active engineers — used to populate the 'assign to' dropdown."""
    return (
        db.query(User)
        .filter(User.role == UserRole.ENGINEER.value, User.active.is_(True))
        .order_by(User.name)
        .all()
    )


# --------------------------- ticket list + detail ----------------------- #

@router.get("/tickets", response_model=List[TicketResponse])
def list_tickets(
    status: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    product: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Ticket)
    # Engineers only see tickets assigned to them.
    if user.role == UserRole.ENGINEER.value:
        q = q.filter(Ticket.assigned_engineer_id == user.id)
    if status:
        q = q.filter(Ticket.status == status.upper())
    if severity:
        q = q.filter(Ticket.severity == severity.upper())
    if product:
        q = q.filter(Ticket.product_category == product)
    if search:
        like = f"%{search.strip()}%"
        like_upper = f"%{search.strip().upper()}%"
        q = q.filter(
            or_(
                Ticket.reference.ilike(like_upper),
                Ticket.business_name.ilike(like),
                Ticket.serial_number.ilike(like_upper),
            )
        )
    q = q.order_by(Ticket.created_at.desc()).offset(offset).limit(limit)
    return q.all()


@router.get("/tickets/{reference}", response_model=TicketResponse)
def get_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    return ticket


@router.get("/tickets/{reference}/events", response_model=List[TicketEventOut])
def get_ticket_events(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    events = (
        db.query(TicketEvent)
        .filter(TicketEvent.ticket_id == ticket.id)
        .order_by(TicketEvent.created_at)
        .all()
    )
    return events


# --------------------------- actions ------------------------------------ #

@router.post("/tickets/{reference}/acknowledge", response_model=TicketResponse)
def acknowledge_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    return acknowledge(db, ticket, user)


@router.post("/tickets/{reference}/assign", response_model=TicketResponse)
def assign_ticket(
    reference: str,
    body: AssignEngineerRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    ticket, engineer = assign_engineer(db, ticket, user, body.engineer_id)
    # Notify engineer in the background so the API returns immediately.
    background.add_task(send_engineer_assignment, ticket, engineer, user)
    return ticket


@router.patch("/tickets/{reference}/warranty", response_model=TicketResponse)
def patch_warranty(
    reference: str,
    body: UpdateWarrantyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    return update_warranty(db, ticket, user, body.warranty_status.upper())


@router.patch("/tickets/{reference}/severity", response_model=TicketResponse)
def patch_severity(
    reference: str,
    body: UpdateSeverityRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Owner/Manager-only. Set or change ticket severity."""
    ticket = _load_ticket(db, reference)
    return update_severity(db, ticket, user, body.severity.upper())


# --------------------------- engineer actions --------------------------- #

@router.post("/tickets/{reference}/accept", response_model=TicketResponse)
def accept_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer claims an assigned ticket. ASSIGNED → ACCEPTED."""
    return accept(db, _load_ticket(db, reference), user)


@router.post("/tickets/{reference}/start-work", response_model=TicketResponse)
def start_work_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer begins active work. ACCEPTED → RESOLVING."""
    return start_work(db, _load_ticket(db, reference), user)


@router.post("/tickets/{reference}/resolve", response_model=TicketResponse)
def resolve_ticket(
    reference: str,
    body: ResolveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer marks the ticket fixed and writes a resolution summary.

    RESOLVING → RESOLVED. The engineer then collects both signatures (customer
    + their own) directly on this admin interface — no email link is sent.
    """
    ticket, _sign_url = resolve(db, _load_ticket(db, reference), user, body.summary)
    return ticket


@router.post("/tickets/{reference}/sign-customer", response_model=TicketResponse)
async def sign_as_customer_via_engineer(
    reference: str,
    signer_name: str = Form(..., min_length=2, max_length=120),
    signature: UploadFile = File(..., description="PNG of the customer's signature"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer captures the customer's signature on their own device.

    Only the assigned engineer can submit. After this, the engineer can
    countersign via POST /sign-engineer to close the ticket.
    """
    ticket = _load_ticket(db, reference)
    image_bytes = await signature.read()
    record_customer_signature_via_engineer(
        db, ticket, user,
        signer_name=signer_name,
        image_bytes=image_bytes,
        content_type=signature.content_type or "image/png",
    )
    db.refresh(ticket)
    return ticket


@router.post("/tickets/{reference}/sign-engineer", response_model=TicketResponse)
async def sign_as_engineer(
    reference: str,
    signature: UploadFile = File(..., description="PNG of the engineer's signature"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer signs the resolution document. Customer must have signed first.

    On success: PDF is generated, ticket transitions RESOLVED → CLOSED.
    """
    ticket = _load_ticket(db, reference)
    image_bytes = await signature.read()
    record_engineer_signature(
        db, ticket, user, image_bytes,
        content_type=signature.content_type or "image/png",
    )
    db.refresh(ticket)
    return ticket


@router.get("/tickets/{reference}/pdf")
def get_resolution_pdf_url(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a short-lived URL to the resolution PDF (Manager / Owner only).

    We return JSON rather than a redirect because the frontend calls this with
    a JWT Authorization header — browser navigation via plain <a href> would
    drop that header and fail with 401. The frontend opens the returned URL
    in a new tab to download.
    """
    if user.role not in (UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Manager or Owner only")
    ticket = _load_ticket(db, reference)
    res = ticket.resolution
    if res is None or not res.pdf_storage_key:
        raise HTTPException(
            status_code=404,
            detail="PDF not available yet — both signatures must be in place.",
        )
    url = get_storage().public_url(res.pdf_storage_key)
    if not url.startswith("http"):
        # Local mode returns "/uploads/..." — make it absolute.
        url = f"http://localhost:8000{url}"
    return {
        "url": url,
        "filename": f"resolution-{reference}.pdf",
        "generated_at": res.pdf_generated_at,
    }


# --------------------------- work notes --------------------------------- #

@router.get("/tickets/{reference}/notes", response_model=List[WorkNoteOut])
def list_notes(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    return (
        db.query(WorkNote)
        .filter(WorkNote.ticket_id == ticket.id)
        .order_by(WorkNote.created_at)
        .all()
    )


@router.post("/tickets/{reference}/notes", response_model=WorkNoteOut, status_code=201)
def add_note(
    reference: str,
    body: AddWorkNoteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return add_work_note(db, _load_ticket(db, reference), user, body.body)


# --------------------------- helpers ------------------------------------ #

def _load_ticket(db: Session, reference: str) -> Ticket:
    t = db.query(Ticket).filter(Ticket.reference == reference.strip().upper()).one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t
