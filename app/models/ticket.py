"""Database models for tickets and attachments.

The schema is designed for the full Phase 1-4 roadmap (status tracking,
technician assignment, analytics) but Phase 1 only populates the core ticket
fields. Adding columns later via Alembic is cheap; renaming is expensive,
so we get the shape right now.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"  # Manager/Owner has seen and triaged it
    ASSIGNED = "ASSIGNED"          # Manager/Owner assigned an engineer
    ACCEPTED = "ACCEPTED"          # Engineer accepted the assignment (separate click)
    RESOLVING = "RESOLVING"        # Engineer actively working
    RESOLVED = "RESOLVED"          # Engineer marked done; awaiting signatures + PDF
    CLOSED = "CLOSED"              # Final state - signed off and archived


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class WarrantyStatus(str, Enum):
    UNKNOWN = "UNKNOWN"              # Default at intake; Owner sets this later
    UNDER_WARRANTY = "UNDER_WARRANTY"
    OUT_OF_WARRANTY = "OUT_OF_WARRANTY"


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Human-friendly ticket reference shown to customer (e.g. AC-2026-00042).
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # Customer details
    business_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    business_type: Mapped[str] = mapped_column(String(60))

    # Address (line 1, city, state, pincode are required; line 2/3 are optional)
    address_line1: Mapped[str] = mapped_column(String(200))
    address_line2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    address_line3: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(80))
    pincode: Mapped[str] = mapped_column(String(10), index=True)

    # Optional geo - populated if customer drops a pin on the map
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Product details
    product_category: Mapped[str] = mapped_column(String(60))
    serial_number: Mapped[str] = mapped_column(String(120), index=True)

    # Issue details
    issue_category: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20), default=Severity.MEDIUM.value)
    description: Mapped[str] = mapped_column(Text)
    preferred_contact_time: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    # Workflow
    status: Mapped[str] = mapped_column(String(20), default=TicketStatus.OPEN.value, index=True)
    # Owner sets this after triage; defaults to UNKNOWN at intake.
    warranty_status: Mapped[str] = mapped_column(
        String(20), default=WarrantyStatus.UNKNOWN.value, index=True
    )

    # Assignment (Phase 2.2+)
    acknowledged_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_engineer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolving_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Engineer's written resolution summary (filled when status goes RESOLVING→RESOLVED).
    resolution_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Service charge in INR, editable by the engineer. Default 500.
    # NOTE: added post-Phase-2.4 — a startup ALTER fills this in for existing
    # SQLite/Postgres rows that pre-date the column. Always treat the value as
    # already-present here.
    service_fee_inr: Mapped[int] = mapped_column(Integer, nullable=False, server_default="500", default=500)

    attachments: Mapped[List["TicketAttachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )
    work_notes: Mapped[List["WorkNote"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="WorkNote.created_at",
    )
    sub_engineers: Mapped[List["SubEngineer"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="SubEngineer.created_at",
    )
    spares: Mapped[List["TicketSpare"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketSpare.created_at",
    )
    shipments: Mapped[List["TicketShipment"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketShipment.departed_at.desc()",
    )
    resolution: Mapped[Optional["Resolution"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", uselist=False
    )
    events: Mapped[List["TicketEvent"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketEvent.created_at",
    )
    # The engineer / manager / owner who took these actions. Lazy-loaded.
    acknowledged_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[acknowledged_by_id], lazy="joined"
    )
    assigned_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assigned_by_id], lazy="joined"
    )
    assigned_engineer: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assigned_engineer_id], lazy="joined"
    )


class TicketEvent(Base):
    """Audit log of every meaningful state change on a ticket."""

    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="events")
    actor: Mapped[Optional["User"]] = relationship(lazy="joined")


class WorkNote(Base):
    """Internal engineer notes recorded while resolving a ticket.

    Phase 2.3: notes are visible to all staff (Owner / Manager / Engineer)
    but only the assigned engineer can add new ones. They're internal-only —
    customers do NOT see these.
    """

    __tablename__ = "work_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="work_notes")
    author: Mapped["User"] = relationship(lazy="joined")
    attachments: Mapped[List["WorkNoteAttachment"]] = relationship(
        back_populates="work_note",
        cascade="all, delete-orphan",
        order_by="WorkNoteAttachment.uploaded_at",
    )


class WorkNoteAttachment(Base):
    """Optional images attached to a work note (worksite photos)."""

    __tablename__ = "work_note_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_note_id: Mapped[int] = mapped_column(
        ForeignKey("work_notes.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_url: Mapped[str] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    work_note: Mapped["WorkNote"] = relationship(back_populates="attachments")


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_url: Mapped[str] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["Ticket"] = relationship(back_populates="attachments")


# Type-only import to make forward references resolve cleanly.
from .user import User  # noqa: E402,F401
