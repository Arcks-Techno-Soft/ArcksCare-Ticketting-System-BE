"""Database models for tickets and attachments.

The schema is designed for the full Phase 1-4 roadmap (status tracking,
technician assignment, analytics) but Phase 1 only populates the core ticket
fields. Adding columns later via Alembic is cheap; renaming is expensive,
so we get the shape right now.
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class TicketStatus(str, Enum):
    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


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
    status: Mapped[str] = mapped_column(String(20), default=TicketStatus.NEW.value, index=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    attachments: Mapped[List["TicketAttachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )


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
