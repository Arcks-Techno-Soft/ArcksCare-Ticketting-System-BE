"""Installation endpoints — under /api/v1/admin/installations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, or_
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
)
from ..services.auth import get_current_user, require_role
from ..services.installation_signing import (
    record_customer_signature_via_engineer,
    record_engineer_signature,
)
from ..services.installation_workflow import (
    add_note,
    assign,
    close_installation,
    update_address,
    update_invoice,
)
from ..services.storage import get_storage

logger = logging.getLogger("skposcare.installations")

router = APIRouter(prefix="/api/v1/admin/installations", tags=["installations"])


def _require_owner_or_manager(user: User) -> None:
    if user.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Only Admin or Manager can do this")


def _make_reference(installation_id: int, year: Optional[int] = None) -> str:
    year = year or datetime.utcnow().year
    return f"AI-{year}-{installation_id:05d}"


def _load(db: Session, reference: str) -> Installation:
    inst = (
        db.query(Installation)
        .filter(Installation.reference == reference.strip().upper())
        .one_or_none()
    )
    if inst is None:
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
    rows = (
        q.order_by(Installation.created_at.desc())
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
    """Any staff member (Admin / Manager / Engineer) creates a new installation.
    Engineer-created installations land in the admin dashboard tagged with
    `created_by`, and stay unassigned for an Admin/Manager to route. Admins and
    Managers may optionally pre-assign (engineer, or self-assign by own id)."""
    # Engineers cannot pre-assign — their installations go to the admin queue.
    if user.role == UserRole.ENGINEER.value:
        body.assigned_engineer_id = None

    inst = Installation(
        reference="PENDING",
        business_name=body.business_name.strip(),
        business_category=body.business_category.strip(),
        contact_name=body.contact_name.strip(),
        phone=body.phone,
        email=(body.email or "").strip().lower() or None,
        invoice_number=body.invoice_number.strip(),
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
    return _load(db, reference)


@router.get("/{reference}/events", response_model=List[InstallationEventOut])
def list_events(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference)
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
    inst = _load(db, reference)
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
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inst = _load(db, reference)
    image_bytes = await signature.read()
    record_engineer_signature(
        db, inst, user, image_bytes,
        content_type=signature.content_type or "image/png",
    )
    db.refresh(inst)
    return inst


@router.get("/{reference}/pdf")
def installation_pdf(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Short-lived link to the generated installation PDF (Admin/Manager)."""
    if user.role not in (UserRole.ADMIN.value, UserRole.OWNER.value, UserRole.MANAGER.value):
        raise HTTPException(status_code=403, detail="Manager or Admin only")
    inst = _load(db, reference)
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
