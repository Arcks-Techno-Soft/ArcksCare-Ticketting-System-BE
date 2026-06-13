"""Business logic for ticket creation, deduplication, and lookup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import MIGRATION_SCHEMA, qualify
from ..models.ticket import Ticket, TicketStatus
from ..models.user import User
from ..schemas.ticket import TicketCreate
from ..utils.ticket_id import make_reference


def ensure_raised_by_column(engine: Engine) -> None:
    """Add tickets.raised_by_id if it's missing.

    `create_all` adds new tables but never new columns on existing tables, so
    this small idempotent ALTER backfills the column for databases that
    pre-date the "who raised this ticket" feature. Works on SQLite + Postgres.
    Existing rows get NULL (i.e. customer self-submitted).

    Scoped to DB_SCHEMA so a test backend can NEVER alter the public/production
    table.
    """
    insp = inspect(engine)
    if "tickets" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    existing = {c["name"] for c in insp.get_columns("tickets", schema=MIGRATION_SCHEMA)}
    if "raised_by_id" in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {qualify('tickets')} ADD COLUMN raised_by_id INTEGER"))

# Any not-yet-resolved status. New submissions for the same serial are blocked
# while one of these exists within the dedup window.
OPEN_STATUSES = (
    TicketStatus.OPEN.value,
    TicketStatus.ACKNOWLEDGED.value,
    TicketStatus.ASSIGNED.value,
    TicketStatus.ACCEPTED.value,
    TicketStatus.RESOLVING.value,
)


def find_recent_open_ticket(db: Session, serial_number: str) -> Optional[Ticket]:
    """Return an open ticket for this serial number created within the dedupe window.

    Dedup rule: same `serial_number` cannot raise a 2nd ticket if there's any
    OPEN ticket on it created within the last DUPLICATE_WINDOW_HOURS hours.
    Resolved/closed tickets do NOT block new submissions - if a fix didn't
    stick, the customer should be able to reopen.
    """
    settings = get_settings()
    window_start = datetime.now(timezone.utc) - timedelta(hours=settings.duplicate_window_hours)

    stmt = (
        select(Ticket)
        .where(Ticket.serial_number == serial_number.strip().upper())
        .where(Ticket.status.in_(OPEN_STATUSES))
        .where(Ticket.created_at >= window_start)
        .order_by(Ticket.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def create_ticket(
    db: Session, payload: TicketCreate, raised_by: Optional[User] = None
) -> Ticket:
    """Persist a new ticket and return it (with the generated reference).

    `raised_by` is the staff user who submitted on a customer's behalf (from
    the mobile/admin app). Leave it None for public customer self-submissions.
    """
    ticket = Ticket(
        raised_by_id=raised_by.id if raised_by is not None else None,
        business_name=payload.business_name.strip(),
        contact_name=payload.contact_name.strip(),
        phone=payload.phone,
        email=(payload.email or "").strip().lower() or None,
        business_type=payload.business_type.value,
        address_line1=payload.address_line1.strip(),
        address_line2=(payload.address_line2 or "").strip() or None,
        address_line3=(payload.address_line3 or "").strip() or None,
        city=payload.city.strip(),
        state=payload.state.strip(),
        pincode=payload.pincode,
        latitude=payload.latitude,
        longitude=payload.longitude,
        product_category=payload.product_category.value,
        serial_number=payload.serial_number,
        issue_category=payload.issue_category.value,
        severity=payload.severity.value,
        description=payload.description.strip(),
        preferred_contact_time=(payload.preferred_contact_time or "").strip() or None,
        status=TicketStatus.OPEN.value,
        reference="PENDING",  # placeholder, replaced below
    )
    db.add(ticket)
    db.flush()  # assigns ticket.id
    # Prefix the reference with the RTO-style state code (e.g. UP-2026-00042).
    ticket.reference = make_reference(
        ticket.id, state=ticket.state, year=datetime.utcnow().year,
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def hours_remaining_in_window(ticket: Ticket) -> float:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    created = ticket.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    expiry = created + timedelta(hours=settings.duplicate_window_hours)
    return max(0.0, (expiry - now).total_seconds() / 3600.0)
