"""Installation endpoints — under /api/v1/admin/installations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.installation import (
    Installation,
    InstallationEvent,
    InstallationNote,
    InstallationStatus,
)
from ..models.user import User, UserRole
from ..schemas.installation import (
    InstallationAddressUpdate,
    InstallationAssignRequest,
    InstallationCreate,
    InstallationEventOut,
    InstallationInvoiceUpdate,
    InstallationListItem,
    InstallationListPage,
    InstallationNoteIn,
    InstallationNoteOut,
    InstallationOut,
    InstallationSalesRepUpdate,
)
from ..services.auth import get_current_user, require_role
from ..services.installation_notify import notify_sales_rep_assigned
from ..services.installation_signing import (
    record_customer_signature_via_engineer,
    record_engineer_signature,
)
from ..services.installation_workflow import (
    add_note,
    assign,
    close_installation,
    end_attempt,
    remove_invoice_document,
    set_invoice_document,
    start_attempt,
    update_address,
    update_invoice,
)
from ..services.storage import get_storage, save_document

logger = logging.getLogger("skposcare.installations")

router = APIRouter(prefix="/api/v1/admin/installations", tags=["installations"])


def _is_sales_eligible(user: User) -> bool:
    """A user can be credited as a sales rep if their role is SALES or they've
    been flagged `is_sales_rep` (e.g. a Manager who also does sales)."""
    return user.role == UserRole.SALES.value or bool(getattr(user, "is_sales_rep", False))


def _require_owner_or_manager(user: User) -> None:
    if user.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Admin or Manager can do this")


def _make_reference(installation_id: int, year: Optional[int] = None) -> str:
    year = year or datetime.utcnow().year
    return f"AI-{year}-{installation_id:05d}"


def _load(db: Session, reference: str, user: Optional[User] = None) -> Installation:
    """Load an installation by reference, applying role-based visibility.

    A SALES user may only open installations they sourced (they're the
    `sales_rep`) or opened themselves — this lets them follow the engineer's
    progress (status, work attempts, notes/photos, timeline) on their own deals
    while staying blind to everyone else's. We return 404 rather than 403 so a
    sales rep can't enumerate the reference space. Admin/Manager/Engineer keep
    their existing access; write endpoints independently enforce role/assignee
    in the workflow service, so visibility never grants the ability to act.

    `user` is optional for legacy callers; read endpoints should pass the
    authenticated user so the scope check fires.
    """
    inst = (
        db.query(Installation)
        .filter(Installation.reference == reference.strip().upper())
        .one_or_none()
    )
    if inst is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    if (
        user is not None
        and user.role == UserRole.SALES.value
        and inst.sales_rep_id != user.id
        and inst.created_by_id != user.id
        and inst.assigned_engineer_id != user.id  # …or it's assigned to them
    ):
        raise HTTPException(status_code=404, detail="Installation not found")
    return inst


# --------------------------- list + create ------------------------------ #

@router.get("", response_model=InstallationListPage)
def list_installations(
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    created_within_days: Optional[int] = Query(default=None, ge=1, le=3650),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Installation)
    if user.role == UserRole.ENGINEER.value:
        q = q.filter(Installation.assigned_engineer_id == user.id)
    elif user.role == UserRole.SALES.value:
        # Sales reps see installations they sourced, opened themselves, or that
        # are assigned to them (an installation can now be assigned to a sales rep).
        q = q.filter(
            or_(
                Installation.sales_rep_id == user.id,
                Installation.created_by_id == user.id,
                Installation.assigned_engineer_id == user.id,
            )
        )
    if status:
        q = q.filter(Installation.status == status.upper())
    if created_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=created_within_days)
        q = q.filter(Installation.created_at >= cutoff)
    if search:
        like = f"%{search.strip()}%"
        like_upper = f"%{search.strip().upper()}%"
        q = q.filter(
            or_(
                Installation.reference.ilike(like_upper),
                Installation.business_name.ilike(like),
                Installation.invoice_number.ilike(like),
                Installation.contact_name.ilike(like),
                Installation.phone.ilike(like),
            )
        )
    total = q.with_entities(func.count(Installation.id)).scalar() or 0
    # Default order mirrors the ticket inbox: group by workflow status so the
    # installations needing attention float to the top and finished ones sink to
    # the bottom, then newest-first within each status group. Installations have
    # no severity, so status is the only grouping tier. The CASE follows the
    # InstallationStatus lifecycle (NEW → … → CLOSED); unknown values sort last.
    status_rank = case(
        {
            InstallationStatus.NEW.value: 0,
            InstallationStatus.ASSIGNED.value: 1,
            InstallationStatus.COMPLETED.value: 2,
            InstallationStatus.CLOSED.value: 3,
        },
        value=Installation.status,
        else_=99,
    )
    rows = (
        q.order_by(status_rank.asc(), Installation.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return InstallationListPage(
        items=[InstallationListItem.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=InstallationOut, status_code=201)
def create_installation(
    body: InstallationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Any staff member (Admin / Manager / Engineer / Sales) creates a new
    installation. Engineer- and Sales-created installations land in the admin
    dashboard tagged with `created_by`, and stay unassigned for an Admin/Manager
    to route. Admins and Managers may optionally pre-assign (engineer, or
    self-assign by own id) and credit a sales rep."""
    # Only Admin/Manager can pre-assign an engineer or credit a sales rep on
    # creation — engineers' and sales' installations go to the admin queue.
    if user.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        body.assigned_engineer_id = None
        # A SALES user opening their own installation is credited as the rep.
        body.sales_rep_id = user.id if user.role == UserRole.SALES.value else None

    # Validate the chosen sales rep is an active SALES user before we attach it.
    sales_rep_id: Optional[int] = None
    if body.sales_rep_id is not None:
        rep = db.query(User).filter(User.id == body.sales_rep_id).one_or_none()
        if rep is None or not rep.active or not _is_sales_eligible(rep):
            raise HTTPException(status_code=400, detail="Invalid sales representative")
        sales_rep_id = rep.id

    inst = Installation(
        reference="PENDING",
        business_name=body.business_name.strip(),
        business_category=body.business_category.strip(),
        contact_name=body.contact_name.strip(),
        phone=body.phone,
        email=(body.email or "").strip().lower() or None,
        invoice_number=body.invoice_number.strip(),
        products_for_installation=body.products_for_installation.strip(),
        address_line1=body.address_line1.strip(),
        address_line2=body.address_line2,
        address_line3=body.address_line3,
        city=body.city.strip(),
        state=body.state.strip(),
        pincode=body.pincode,
        latitude=body.latitude,
        longitude=body.longitude,
        status=InstallationStatus.NEW.value,
        created_by_id=user.id,
        sales_rep_id=sales_rep_id,
    )
    db.add(inst)
    db.flush()
    inst.reference = _make_reference(inst.id, year=datetime.utcnow().year)

    db.add(
        InstallationEvent(
            installation_id=inst.id,
            actor_user_id=user.id,
            event_type="CREATED",
            to_status=inst.status,
            payload={
                "business_name": inst.business_name,
                "invoice_number": inst.invoice_number,
            },
        )
    )
    db.commit()
    db.refresh(inst)

    if body.assigned_engineer_id is not None:
        inst, _ = assign(db, inst, user, body.assigned_engineer_id)

    # Notify the credited sales rep on WhatsApp — unless they credited
    # themselves (a SALES user opening their own installation).
    if inst.sales_rep_id is not None and inst.sales_rep_id != user.id:
        notify_sales_rep_assigned(inst.id)

    logger.info(
        "Installation %s created by %s for %s",
        inst.reference, user.username, inst.business_name,
    )
    return inst


# --------------------------- detail ------------------------------------- #

@router.get("/{reference}", response_model=InstallationOut)
def get_installation(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _load(db, reference, user)


@router.get("/{reference}/events", response_model=List[InstallationEventOut])
def list_events(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference, user)
    return (
        db.query(InstallationEvent)
        .filter(InstallationEvent.installation_id == inst.id)
        .order_by(InstallationEvent.created_at)
        .all()
    )


# --------------------------- assignment --------------------------------- #

@router.post("/{reference}/assign", response_model=InstallationOut)
def assign_endpoint(
    reference: str,
    body: InstallationAssignRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference)
    inst, _ = assign(db, inst, user, body.engineer_id)
    return inst


@router.post("/{reference}/self-assign", response_model=InstallationOut)
def self_assign_endpoint(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_owner_or_manager(user)
    inst = _load(db, reference)
    inst, _ = assign(db, inst, user, user.id)
    return inst


# --------------------------- invoice ------------------------------------ #

@router.patch("/{reference}/invoice", response_model=InstallationOut)
def update_invoice_endpoint(
    reference: str,
    body: InstallationInvoiceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edit the invoice number. Assignee / Admin / Manager, before CLOSED."""
    inst = _load(db, reference)
    return update_invoice(db, inst, user, body.invoice_number)


@router.patch("/{reference}/sales-rep", response_model=InstallationOut)
def update_sales_rep_endpoint(
    reference: str,
    body: InstallationSalesRepUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Set, change, or clear the credited sales rep. Admin / Manager only."""
    _require_owner_or_manager(user)
    inst = _load(db, reference)
    prev_rep_id = inst.sales_rep_id
    if body.sales_rep_id is None:
        inst.sales_rep_id = None
    else:
        rep = db.query(User).filter(User.id == body.sales_rep_id).one_or_none()
        if rep is None or not rep.active or not _is_sales_eligible(rep):
            raise HTTPException(status_code=400, detail="Invalid sales representative")
        inst.sales_rep_id = rep.id
    db.commit()
    db.refresh(inst)
    # Notify only when a (new, different) rep was actually credited, and not
    # when a manager credits themselves.
    if (
        inst.sales_rep_id is not None
        and inst.sales_rep_id != prev_rep_id
        and inst.sales_rep_id != user.id
    ):
        notify_sales_rep_assigned(inst.id)
    return inst


# --------------------------- invoice document --------------------------- #

@router.post("/{reference}/invoice-document", response_model=InstallationOut)
async def upload_invoice_document(
    reference: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Attach (or replace) the invoice document — a PDF or image.

    Allowed for the assignee / Admin / Manager at any time before the
    installation is CLOSED. Uploading again replaces the existing document.
    """
    inst = _load(db, reference)
    meta = save_document(file, inst.reference)
    return set_invoice_document(db, inst, user, meta)


@router.delete("/{reference}/invoice-document", response_model=InstallationOut)
def delete_invoice_document(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Remove the uploaded invoice document. Assignee / Admin / Manager, before CLOSED."""
    inst = _load(db, reference)
    return remove_invoice_document(db, inst, user)


# --------------------------- address ------------------------------------ #

@router.patch("/{reference}/address", response_model=InstallationOut)
def update_address_endpoint(
    reference: str,
    body: InstallationAddressUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edit the site address / location. Assignee / Admin / Manager, before CLOSED."""
    inst = _load(db, reference)
    return update_address(db, inst, user, body.model_dump())


# --------------------------- notes -------------------------------------- #

@router.get("/{reference}/notes", response_model=List[InstallationNoteOut])
def list_notes(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference, user)
    return (
        db.query(InstallationNote)
        .filter(InstallationNote.installation_id == inst.id)
        .order_by(InstallationNote.created_at)
        .all()
    )


@router.post("/{reference}/notes", response_model=InstallationNoteOut, status_code=201)
async def add_note_endpoint(
    reference: str,
    body: str = Form(..., min_length=2, max_length=4000),
    images: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add an installation work note with optional worksite photos."""
    inst = _load(db, reference)
    saved: list[dict] = []
    if images:
        from ..services.storage import save_uploads  # noqa: WPS433
        for f in images:
            ct = (f.content_type or "").lower()
            if ct and not ct.startswith("image/"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Only image files allowed (got {ct} for {f.filename})",
                )
        saved = save_uploads(images, inst.reference)
    return add_note(db, inst, user, body, attachments=saved)


# --------------------------- attempts ----------------------------------- #

@router.post("/{reference}/attempts", response_model=InstallationOut, status_code=201)
def start_attempt_endpoint(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Begin a new on-site work attempt. Returns the full installation."""
    inst = _load(db, reference)
    start_attempt(db, inst, user)
    db.refresh(inst)
    return inst


@router.post("/{reference}/attempts/{attempt_id}/end", response_model=InstallationOut)
def end_attempt_endpoint(
    reference: str,
    attempt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """End the given open attempt. Returns the full installation."""
    inst = _load(db, reference)
    end_attempt(db, inst, user, attempt_id)
    db.refresh(inst)
    return inst


# --------------------------- close -------------------------------------- #

@router.post("/{reference}/close", response_model=InstallationOut)
def close_endpoint(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference)
    inst, _ = close_installation(db, inst, user)
    return inst


# --------------------------- signing ------------------------------------ #

@router.post("/{reference}/sign-customer", response_model=InstallationOut)
async def sign_customer(
    reference: str,
    signer_name: str = Form(..., min_length=2, max_length=120),
    signature: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference)
    image_bytes = await signature.read()
    record_customer_signature_via_engineer(
        db, inst, user,
        signer_name=signer_name,
        image_bytes=image_bytes,
        content_type=signature.content_type or "image/png",
    )
    db.refresh(inst)
    return inst


@router.post("/{reference}/sign-engineer", response_model=InstallationOut)
async def sign_engineer(
    reference: str,
    signature: UploadFile = File(...),
    photo: UploadFile | None = File(None, description="Optional photo of the customer"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer signs and closes the installation. An optional customer photo,
    captured at this final sign-off step, is saved and embedded in the PDF."""
    inst = _load(db, reference)
    image_bytes = await signature.read()
    photo_bytes = await photo.read() if photo is not None else None
    record_engineer_signature(
        db, inst, user, image_bytes,
        content_type=signature.content_type or "image/png",
        photo_bytes=photo_bytes,
        photo_content_type=photo.content_type if photo is not None else None,
    )
    db.refresh(inst)
    return inst


@router.get("/{reference}/pdf")
def installation_pdf(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Short-lived link to the generated installation PDF.

    Accessible to the Admin, a Manager, or the engineer the installation is
    assigned to (mirrors the ticket resolution PDF)."""
    inst = _load(db, reference)
    if (
        user.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value)
        and inst.assigned_engineer_id != user.id
    ):
        raise HTTPException(
            status_code=403, detail="Manager, Admin, or the assigned engineer only"
        )
    res = inst.resolution
    if res is None or not res.pdf_storage_key:
        raise HTTPException(
            status_code=404,
            detail="PDF not available yet — both signatures must be in place.",
        )
    url = get_storage().public_url(res.pdf_storage_key)
    if not url.startswith("http"):
        url = f"http://localhost:8000{url}"
    return {
        "url": url,
        "filename": f"installation-{reference}.pdf",
        "generated_at": res.pdf_generated_at,
    }
