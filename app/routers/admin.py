"""Admin endpoints for the internal staff app.

All routes here require a valid JWT via `get_current_user`. Phase 2.2 adds
acknowledge / assign / warranty actions, plus engineer + event listing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.shipment import TicketShipment, TicketShipmentItem
from ..models.spare import SpareCatalog, TicketSpare
from ..models.sub_engineer import SubEngineer
from ..models.ticket import Ticket, TicketEvent, WorkNote
from ..models.user import User, UserRole
from ..schemas.auth import (
    AddSubEngineerRequest,
    AddTicketSpareRequest,
    AddWorkNoteRequest,
    AssignEngineerRequest,
    ChargesSummary,
    CreateUserRequest,
    CreateUserResponse,
    ResolveRequest,
    SpareCatalogItem,
    SubEngineerOut,
    SubEngineerSuggestion,
    TicketEventOut,
    TicketSpareOut,
    UpdateSeverityRequest,
    UpdateServiceFeeRequest,
    UpdateTicketSpareRequest,
    UpdateUserActiveRequest,
    UpdateWarrantyRequest,
    UserOut,
    WorkNoteOut,
)
from ..schemas.ticket import (
    ShipmentCreate,
    ShipmentOut,
    TicketListItem,
    TicketListPage,
    TicketResponse,
)
from ..services.analytics import compute_analytics
from ..services.auth import get_current_user, hash_password, require_role
from ..services.email import send_engineer_assignment
from ..services.signing import (
    record_customer_signature_via_engineer,
    record_engineer_signature,
)
from ..services.spares import compute_charges
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

logger = logging.getLogger("skposcare.admin")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# --------------------------- profile + lookups -------------------------- #

@router.get("/me", response_model=UserOut)
def my_profile(user: User = Depends(get_current_user)):
    return user


# --------------------------- user management (Owner-only) --------------- #

@router.get("/users", response_model=List[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.OWNER)),
):
    """All staff accounts — Owner only."""
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.OWNER)),
):
    """Create a new Admin (Manager) or Engineer account.

    Owner explicitly chooses username + password — no auto-generation, no
    temp-password handoff. Username must be unique across all users
    (including OWNERs and inactive accounts). Email uniqueness is also
    enforced so we never end up with two profiles tied to the same inbox.
    """
    username = body.username.strip().lower()

    # Pydantic already validated the character set; this is just the DB-uniqueness
    # check (case-insensitive: usernames are stored lowercased on create).
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="That username is already taken.")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="A user with this email already exists.")

    user = User(
        username=username,
        password_hash=hash_password(body.password),
        name=f"{body.first_name} {body.last_name}".strip(),
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        email=body.email,
        role=body.role,
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("OWNER %s created user %s (%s)", _user.username, user.username, user.role)
    return CreateUserResponse(user=UserOut.model_validate(user))


@router.patch("/users/{user_id}/active", response_model=UserOut)
def set_user_active(
    user_id: int,
    body: UpdateUserActiveRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_role(UserRole.OWNER)),
):
    """Activate or deactivate a user. Owners can't deactivate themselves."""
    target = db.query(User).filter(User.id == user_id).one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == actor.id and not body.active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
    target.active = body.active
    db.commit()
    db.refresh(target)
    return target


# --------------------------- analytics (Owner-only) --------------------- #

@router.get("/analytics")
def get_analytics(
    days: int = Query(default=30, ge=7, le=365),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.OWNER)),
):
    """Aggregated metrics for the analytics dashboard."""
    return compute_analytics(db, days)


# --------------------------- lookups ------------------------------------ #

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

@router.get("/tickets", response_model=TicketListPage)
def list_tickets(
    status: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    product: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    created_within_days: Optional[int] = Query(default=None, ge=1, le=3650),
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
    if created_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=created_within_days)
        q = q.filter(Ticket.created_at >= cutoff)
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
    # COUNT runs against the same filtered query so totals stay accurate.
    total = q.with_entities(func.count(Ticket.id)).scalar() or 0
    rows = (
        q.order_by(Ticket.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TicketListPage(
        items=[TicketListItem.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


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


@router.post("/tickets/{reference}/self-assign", response_model=TicketResponse)
def self_assign_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Owner/Manager assigns the ticket to themselves. Convenience wrapper
    around /assign with engineer_id = current_user.id."""
    if user.role not in (UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Manager or Owner can self-assign")
    ticket = _load_ticket(db, reference)
    ticket, _ = assign_engineer(db, ticket, user, user.id)
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


@router.post("/tickets/{reference}/pdf/regenerate")
def regenerate_resolution_pdf(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-render the resolution PDF in place (Owner / Manager only).

    PDFs in storage are snapshots — once written at close-time they don't
    refresh when the template changes. This endpoint re-runs the generator
    against the current ticket data and overwrites the stored file, so any
    template / branding tweaks become visible on already-closed tickets
    without losing their signatures or close history.

    Preserves the original storage key (so existing references to the file
    remain valid) and bumps `pdf_generated_at` to the current time.
    """
    from datetime import datetime, timezone  # local — module-level import already covers timedelta

    from ..services.pdf_generator import generate_resolution_pdf  # avoid module-load cost when unused

    if user.role not in (UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Manager or Owner only")
    ticket = _load_ticket(db, reference)
    res = ticket.resolution
    # Need both signatures so the regenerated PDF carries the same legal
    # weight as the original; we never want a "draft-looking" PDF replacing
    # a fully-signed one.
    if res is None or res.customer_signed_at is None or res.engineer_signed_at is None:
        raise HTTPException(
            status_code=409,
            detail="Can only regenerate after both signatures are in place.",
        )

    pdf_bytes = generate_resolution_pdf(ticket, res)
    storage = get_storage()
    filename = f"resolution-{ticket.reference}.pdf"
    meta = storage.save_bytes(pdf_bytes, "application/pdf", ticket.reference, filename)
    res.pdf_storage_key = meta["storage_url"]
    res.pdf_generated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(res)
    logger.info("Regenerated PDF for %s by %s", ticket.reference, user.username)

    url = storage.public_url(res.pdf_storage_key)
    if not url.startswith("http"):
        url = f"http://localhost:8000{url}"
    return {
        "url": url,
        "filename": filename,
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
async def add_note(
    reference: str,
    body: str = Form(..., min_length=2, max_length=4000),
    images: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a work note. Supports optional worksite images (multipart form).

    `images` may be omitted entirely. Each file is validated by storage and
    persisted before the note row is committed.
    """
    ticket = _load_ticket(db, reference)
    saved: list[dict] = []
    if images:
        from ..services.storage import save_uploads  # noqa: WPS433
        # Reject obvious non-images early — keep worksite uploads to photos.
        for f in images:
            ct = (f.content_type or "").lower()
            if ct and not ct.startswith("image/"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Only image files allowed (got {ct} for {f.filename})",
                )
        saved = save_uploads(images, ticket.reference)
    return add_work_note(db, ticket, user, body, attachments=saved)


# --------------------------- sub-engineers ------------------------------ #

def _can_manage_sub_engineers(ticket: Ticket, user: User) -> bool:
    """Owner, Manager, or the current assignee can manage sub-engineers."""
    if user.role in (UserRole.OWNER.value, UserRole.MANAGER.value):
        return True
    return ticket.assigned_engineer_id == user.id


@router.get("/tickets/{reference}/sub-engineers", response_model=List[SubEngineerOut])
def list_sub_engineers(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    return (
        db.query(SubEngineer)
        .filter(SubEngineer.ticket_id == ticket.id)
        .order_by(SubEngineer.created_at)
        .all()
    )


@router.get(
    "/tickets/{reference}/sub-engineer-suggestions",
    response_model=List[SubEngineerSuggestion],
)
def list_sub_engineer_suggestions(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Previously-used sub-engineers in this ticket's city.

    Returns distinct contacts (by name + phone) drawn from prior tickets in
    the same city, with how often they've been used and when they were last
    added. Sub-engineers already on *this* ticket are filtered out so the
    dropdown doesn't suggest duplicates.
    """
    ticket = _load_ticket(db, reference)
    city_key = ticket.city.strip()
    if not city_key:
        return []

    # Aggregate by (name, phone) so the same contact appearing on N past
    # tickets shows up once with times_used = N. SQLite's lower() is fine
    # for our scale; if we ever move to Postgres we can switch to ILIKE.
    rows = (
        db.query(
            SubEngineer.name.label("name"),
            SubEngineer.phone.label("phone"),
            func.max(SubEngineer.location).label("location"),
            func.count(SubEngineer.id).label("times_used"),
            func.max(SubEngineer.created_at).label("last_used_at"),
        )
        .join(Ticket, Ticket.id == SubEngineer.ticket_id)
        .filter(func.lower(Ticket.city) == city_key.lower())
        .filter(SubEngineer.ticket_id != ticket.id)
        .group_by(SubEngineer.name, SubEngineer.phone)
        .order_by(func.max(SubEngineer.created_at).desc())
        .limit(50)
        .all()
    )

    already_on_ticket = {
        (s.name.strip().lower(), s.phone.strip())
        for s in ticket.sub_engineers
    }
    return [
        SubEngineerSuggestion(
            name=r.name,
            phone=r.phone,
            location=r.location,
            times_used=int(r.times_used),
            last_used_at=r.last_used_at,
        )
        for r in rows
        if (r.name.strip().lower(), r.phone.strip()) not in already_on_ticket
    ]


@router.post("/tickets/{reference}/sub-engineers", response_model=SubEngineerOut, status_code=201)
def add_sub_engineer(
    reference: str,
    body: AddSubEngineerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add an ad-hoc field contractor to the ticket.

    Available after the ticket has been ACKNOWLEDGED. Caller must be the
    current assignee, Owner, or Manager.
    """
    ticket = _load_ticket(db, reference)
    if ticket.status in ("OPEN",):
        raise HTTPException(
            status_code=409,
            detail="Sub-engineers can be added after the ticket is acknowledged.",
        )
    if not _can_manage_sub_engineers(ticket, user):
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Owner, or Manager can add sub-engineers",
        )
    sub = SubEngineer(
        ticket_id=ticket.id,
        name=body.name.strip(),
        phone=body.phone.strip(),
        location=body.location.strip(),
        created_by_user_id=user.id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    logger.info("Added sub-engineer %s to %s by %s", sub.name, ticket.reference, user.username)
    return sub


@router.delete("/tickets/{reference}/sub-engineers/{sub_id}", status_code=204)
def remove_sub_engineer(
    reference: str,
    sub_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    if not _can_manage_sub_engineers(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to remove sub-engineers on this ticket")
    sub = (
        db.query(SubEngineer)
        .filter(SubEngineer.id == sub_id, SubEngineer.ticket_id == ticket.id)
        .one_or_none()
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="Sub-engineer not found")
    db.delete(sub)
    db.commit()
    return None


# --------------------------- spare parts / charges ---------------------- #

def _can_manage_spares(ticket: Ticket, user: User) -> bool:
    """Spares + service fee are editable only while the ticket is RESOLVING.

    Once the engineer marks it RESOLVED the invoice freezes — the figures
    flow into the resolution PDF and the customer's signature attests to
    them, so post-hoc edits (even by Owner/Manager) would break that audit
    chain.
    """
    if ticket.status != "RESOLVING":
        return False
    if user.role in (UserRole.OWNER.value, UserRole.MANAGER.value):
        return True
    return ticket.assigned_engineer_id == user.id


@router.get("/spare-catalog", response_model=List[SpareCatalogItem])
def list_spare_catalog(
    product: Optional[str] = Query(default=None, description="Filter by product category"),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Available spare parts. Optionally filter by product category."""
    q = db.query(SpareCatalog).filter(SpareCatalog.active.is_(True))
    if product:
        q = q.filter(SpareCatalog.product_category == product)
    return q.order_by(SpareCatalog.product_category, SpareCatalog.name).all()


@router.get("/tickets/{reference}/charges", response_model=ChargesSummary)
def get_ticket_charges(
    reference: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Spares + service fee + computed totals for this ticket."""
    ticket = _load_ticket(db, reference)
    return compute_charges(ticket)


@router.post(
    "/tickets/{reference}/spares",
    response_model=TicketSpareOut,
    status_code=201,
)
def add_ticket_spare(
    reference: str,
    body: AddTicketSpareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a spare line item.

    Either pass `catalog_id` to pick from the seeded catalog, or pass
    `name` + `unit_price_inr` for an ad-hoc part. Only the assignee may add
    while the ticket is RESOLVING. Owner/Manager may add at any time.
    """
    ticket = _load_ticket(db, reference)
    if not _can_manage_spares(ticket, user):
        if ticket.status not in ("RESOLVING",):
            raise HTTPException(
                status_code=409,
                detail=f"Spares are locked once the ticket is {ticket.status.lower()}.",
            )
        raise HTTPException(
            status_code=403,
            detail="Only the assigned engineer (or Owner/Manager) can edit spares.",
        )

    name: str
    unit_price: int
    catalog_id: Optional[int] = None

    if body.catalog_id is not None:
        item = db.query(SpareCatalog).filter(SpareCatalog.id == body.catalog_id).one_or_none()
        if item is None or not item.active:
            raise HTTPException(status_code=404, detail="Spare catalog item not found")
        catalog_id = item.id
        name = item.name
        # Engineer can override the catalog price at add-time; otherwise use the default.
        unit_price = (
            body.unit_price_inr if body.unit_price_inr is not None else item.default_price_inr
        )
    else:
        if not body.name or body.unit_price_inr is None:
            raise HTTPException(
                status_code=422,
                detail="Provide catalog_id, or both name and unit_price_inr for an ad-hoc part.",
            )
        name = body.name.strip()
        unit_price = body.unit_price_inr

    spare = TicketSpare(
        ticket_id=ticket.id,
        catalog_id=catalog_id,
        name=name,
        unit_price_inr=int(unit_price),
        quantity=body.quantity,
        created_by_user_id=user.id,
    )
    db.add(spare)
    db.commit()
    db.refresh(spare)
    logger.info(
        "Added spare '%s' x%d to %s by %s",
        spare.name, spare.quantity, ticket.reference, user.username,
    )
    return spare


@router.patch("/tickets/{reference}/spares/{spare_id}", response_model=TicketSpareOut)
def update_ticket_spare(
    reference: str,
    spare_id: int,
    body: UpdateTicketSpareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edit price or quantity of an existing spare line item."""
    ticket = _load_ticket(db, reference)
    if not _can_manage_spares(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to edit spares on this ticket")
    spare = (
        db.query(TicketSpare)
        .filter(TicketSpare.id == spare_id, TicketSpare.ticket_id == ticket.id)
        .one_or_none()
    )
    if spare is None:
        raise HTTPException(status_code=404, detail="Spare not found")
    if body.unit_price_inr is not None:
        spare.unit_price_inr = int(body.unit_price_inr)
    if body.quantity is not None:
        spare.quantity = int(body.quantity)
    db.commit()
    db.refresh(spare)
    return spare


@router.delete("/tickets/{reference}/spares/{spare_id}", status_code=204)
def remove_ticket_spare(
    reference: str,
    spare_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    if not _can_manage_spares(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to edit spares on this ticket")
    spare = (
        db.query(TicketSpare)
        .filter(TicketSpare.id == spare_id, TicketSpare.ticket_id == ticket.id)
        .one_or_none()
    )
    if spare is None:
        raise HTTPException(status_code=404, detail="Spare not found")
    db.delete(spare)
    db.commit()
    return None


@router.patch("/tickets/{reference}/service-fee", response_model=ChargesSummary)
def update_service_fee(
    reference: str,
    body: UpdateServiceFeeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    if not _can_manage_spares(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to edit charges on this ticket")
    ticket.service_fee_inr = int(body.service_fee_inr)
    db.commit()
    db.refresh(ticket)
    return compute_charges(ticket)


# --------------------------- spare-parts shipments ---------------------- #

def _can_ship_parts(ticket: Ticket, user: User) -> bool:
    """Owner, Manager, or the current assignee can record shipments.

    Allowed at any status except CLOSED — once the ticket is closed the
    work is over and logging in-transit parts retroactively is noise.
    """
    if ticket.status == "CLOSED":
        return False
    if user.role in (UserRole.OWNER.value, UserRole.MANAGER.value):
        return True
    return ticket.assigned_engineer_id == user.id


@router.get("/tickets/{reference}/shipments", response_model=List[ShipmentOut])
def list_shipments(
    reference: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference)
    return (
        db.query(TicketShipment)
        .filter(TicketShipment.ticket_id == ticket.id)
        .order_by(TicketShipment.departed_at.desc())
        .all()
    )


@router.post("/tickets/{reference}/shipments", response_model=ShipmentOut, status_code=201)
def create_shipment(
    reference: str,
    body: ShipmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record a courier shipment of spare parts for this ticket.

    Items can either reference a `SpareCatalog.id` (preferred) or carry a
    free-form `name`. Tracking ID is optional — some couriers / own-vehicle
    dispatches don't generate one. Departure timestamp defaults to now.
    """
    ticket = _load_ticket(db, reference)
    if not _can_ship_parts(ticket, user):
        if ticket.status == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="Ticket is closed — shipments can no longer be added.",
            )
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Owner, or Manager can record shipments.",
        )

    departed = body.departed_at or datetime.now(timezone.utc)
    shipment = TicketShipment(
        ticket_id=ticket.id,
        courier_name=body.courier_name.strip(),
        tracking_id=(body.tracking_id or "").strip() or None,
        departed_at=departed,
        created_by_user_id=user.id,
    )
    db.add(shipment)
    db.flush()  # so shipment.id is available below

    # Resolve each item: prefer catalog_id, fall back to free-form name.
    for item in body.items:
        catalog_id: Optional[int] = None
        name: str
        if item.catalog_id is not None:
            cat = db.query(SpareCatalog).filter(SpareCatalog.id == item.catalog_id).one_or_none()
            if cat is None or not cat.active:
                raise HTTPException(
                    status_code=404,
                    detail=f"Spare catalog item {item.catalog_id} not found",
                )
            catalog_id = cat.id
            name = cat.name
        else:
            if not item.name or not item.name.strip():
                raise HTTPException(
                    status_code=422,
                    detail="Each shipment item needs either catalog_id or a name.",
                )
            name = item.name.strip()
        db.add(
            TicketShipmentItem(
                shipment_id=shipment.id,
                catalog_id=catalog_id,
                name=name,
                quantity=int(item.quantity),
            )
        )

    # Audit trail on the ticket timeline so it shows up next to acks/assigns.
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_user_id=user.id,
            event_type="PARTS_SHIPPED",
            payload={
                "courier": shipment.courier_name,
                "tracking_id": shipment.tracking_id,
                "item_count": len(body.items),
            },
        )
    )
    db.commit()
    db.refresh(shipment)
    logger.info(
        "Shipment created for %s by %s (%s, %d items)",
        ticket.reference, user.username, shipment.courier_name, len(body.items),
    )
    return shipment


@router.post(
    "/tickets/{reference}/shipments/{shipment_id}/deliver",
    response_model=ShipmentOut,
)
def mark_shipment_delivered(
    reference: str,
    shipment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Close the shipment — i.e. the parts have arrived at the worksite.

    Idempotent: hitting it twice returns the already-delivered shipment
    rather than erroring (avoids confusing double-clicks).
    """
    ticket = _load_ticket(db, reference)
    if not _can_ship_parts(ticket, user):
        if ticket.status == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="Ticket is closed — shipment status can no longer change.",
            )
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Owner, or Manager can update shipments.",
        )
    shipment = (
        db.query(TicketShipment)
        .filter(
            TicketShipment.id == shipment_id,
            TicketShipment.ticket_id == ticket.id,
        )
        .one_or_none()
    )
    if shipment is None:
        raise HTTPException(status_code=404, detail="Shipment not found")
    if shipment.delivered_at is not None:
        return shipment  # already delivered — no-op

    shipment.delivered_at = datetime.now(timezone.utc)
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_user_id=user.id,
            event_type="PARTS_DELIVERED",
            payload={
                "shipment_id": shipment.id,
                "courier": shipment.courier_name,
                "tracking_id": shipment.tracking_id,
            },
        )
    )
    db.commit()
    db.refresh(shipment)
    logger.info(
        "Shipment %d on %s marked delivered by %s",
        shipment.id, ticket.reference, user.username,
    )
    return shipment


# --------------------------- helpers ------------------------------------ #

def _load_ticket(db: Session, reference: str) -> Ticket:
    t = db.query(Ticket).filter(Ticket.reference == reference.strip().upper()).one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t
