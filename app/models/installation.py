"""Installation workflow models.

A lightweight cousin of Ticket — used when Admin/Admin starts a new product
installation for a business. Only basic info is captured (no severity, no
spares, no attachments). The signing + PDF flow mirrors ticket Resolution.

Lifecycle: NEW → ASSIGNED → COMPLETED → CLOSED
  NEW       — created, not assigned yet
  ASSIGNED  — engineer (or owner/manager self-assign) is on it
  COMPLETED — engineer hit "Close": work done, awaiting signatures
  CLOSED    — both signatures captured, PDF generated
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class InstallationStatus(str, Enum):
    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    COMPLETED = "COMPLETED"   # engineer hit "Close" — awaiting signatures
    CLOSED = "CLOSED"         # both signed, PDF generated


class Installation(Base):
    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # Customer / business basics
    business_name: Mapped[str] = mapped_column(String(200))
    business_category: Mapped[str] = mapped_column(String(80))
    contact_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)

    invoice_number: Mapped[str] = mapped_column(String(80), index=True)

    status: Mapped[str] = mapped_column(
        String(20), default=InstallationStatus.NEW.value, index=True
    )

    # Who created it (Admin/Manager)
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Assignment (engineer, or owner/manager self-assignment)
    assigned_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_engineer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    notes: Mapped[List["InstallationNote"]] = relationship(
        back_populates="installation",
        cascade="all, delete-orphan",
        order_by="InstallationNote.created_at",
    )
    events: Mapped[List["InstallationEvent"]] = relationship(
        back_populates="installation",
        cascade="all, delete-orphan",
        order_by="InstallationEvent.created_at",
    )
    resolution: Mapped[Optional["InstallationResolution"]] = relationship(
        back_populates="installation", cascade="all, delete-orphan", uselist=False
    )

    created_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    assigned_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assigned_by_id], lazy="joined"
    )
    assigned_engineer: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assigned_engineer_id], lazy="joined"
    )


class InstallationNote(Base):
    __tablename__ = "installation_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    installation: Mapped["Installation"] = relationship(back_populates="notes")
    author: Mapped["User"] = relationship(lazy="joined")
    attachments: Mapped[List["InstallationNoteAttachment"]] = relationship(
        back_populates="note",
        cascade="all, delete-orphan",
        order_by="InstallationNoteAttachment.uploaded_at",
    )


class InstallationNoteAttachment(Base):
    """Optional images attached to an installation work note (worksite photos)."""

    __tablename__ = "installation_note_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_note_id: Mapped[int] = mapped_column(
        ForeignKey("installation_notes.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_url: Mapped[str] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    note: Mapped["InstallationNote"] = relationship(back_populates="attachments")


class InstallationEvent(Base):
    __tablename__ = "installation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    installation: Mapped["Installation"] = relationship(back_populates="events")
    actor: Mapped[Optional["User"]] = relationship(lazy="joined")


class InstallationResolution(Base):
    """Mirrors ticket Resolution — holds the two signatures + the generated PDF."""

    __tablename__ = "installation_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), unique=True, index=True
    )

    customer_sign_token: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    customer_sign_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    customer_signer_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    customer_signature_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    customer_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    engineer_signature_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    engineer_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    pdf_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installation: Mapped["Installation"] = relationship(back_populates="resolution")


from .user import User  # noqa: E402,F401
