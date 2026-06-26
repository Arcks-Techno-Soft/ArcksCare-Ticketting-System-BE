"""Warranty-registry endpoints.

Staff register the warranty for a sold unit by its serial number; the ticket
flow (and the form's own live check) can then look a serial up to decide
whether service + spares are waived.

All routes require a valid JWT. Creating / deleting is limited to Admin and
Manager; reading is open to any authenticated staff member.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.user import User, UserRole
from ..models.warranty import Warranty
from ..schemas.warranty import WarrantyCreate, WarrantyLookupOut, WarrantyOut
from ..services.auth import get_current_user, require_role

logger = logging.getLogger("skposcare")

router = APIRouter(prefix="/api/v1/admin/warranties", tags=["warranties"])


def _add_months(start: date, months: int) -> date:
    """Return `start` advanced by `months` calendar months, clamping the day to
    the last valid day of the target month (e.g. 31 Jan + 1 month → 28/29 Feb).
    No external dependency needed."""
    zero_based = start.month - 1 + months
    year = start.year + zero_based // 12
    month = zero_based % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(start.day, last_day))


@router.get("", response_model=List[WarrantyOut])
def list_warranties(
    q: Optional[str] = Query(default=None, description="Filter by serial or product"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Most-recently registered warranties first. Optional `q` matches on serial
    number or product name (case-insensitive)."""
    query = db.query(Warranty)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Warranty.serial_number.ilike(like),
                Warranty.product_name.ilike(like),
            )
        )
    return query.order_by(Warranty.created_at.desc()).limit(limit).all()


@router.get("/lookup", response_model=WarrantyLookupOut)
def lookup_by_serial(
    serial: str = Query(min_length=1),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Look a serial number up. Used by the registration form to warn before
    submit, and by the ticket flow to decide warranty coverage."""
    norm = Warranty.normalise_serial(serial)
    row = (
        db.query(Warranty)
        .filter(Warranty.serial_number_norm == norm)
        .one_or_none()
    )
    if row is None:
        return WarrantyLookupOut(found=False)
    return WarrantyLookupOut(found=True, warranty=row)


@router.post("", response_model=WarrantyOut, status_code=status.HTTP_201_CREATED)
def create_warranty(
    body: WarrantyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER)),
):
    """Register a warranty for one unit. The serial number is unique
    (case-insensitively): registering the same serial twice returns 409 so the
    UI can tell the user it is already added."""
    norm = Warranty.normalise_serial(body.serial_number)

    existing = (
        db.query(Warranty)
        .filter(Warranty.serial_number_norm == norm)
        .one_or_none()
    )
    if existing is not None:
        # 409 Conflict — the frontend surfaces this as
        # "already added warranty for this product".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A warranty is already registered for serial '{existing.serial_number}' "
                f"({existing.product_name})."
            ),
        )

    expiry = _add_months(body.sale_date, body.warranty_months)

    warranty = Warranty(
        product_name=body.product_name.strip(),
        serial_number=body.serial_number.strip(),
        serial_number_norm=norm,
        sale_date=body.sale_date,
        warranty_months=body.warranty_months,
        expiry_date=expiry,
        notes=body.notes,
        created_by_id=user.id,
    )
    db.add(warranty)
    try:
        db.commit()
    except Exception:
        # Guards the race where two requests pass the existence check at once —
        # the unique index on serial_number_norm still rejects the duplicate.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A warranty is already registered for this serial number.",
        )
    db.refresh(warranty)
    logger.info(
        "Warranty registered: %s (%s) by %s — %s months, expires %s",
        warranty.serial_number, warranty.product_name, user.username,
        warranty.warranty_months, warranty.expiry_date,
    )
    return warranty


@router.delete("/{warranty_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_warranty(
    warranty_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER)),
):
    """Remove a warranty record (e.g. entered against the wrong serial)."""
    row = db.query(Warranty).filter(Warranty.id == warranty_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Warranty not found")
    db.delete(row)
    db.commit()
    return None
