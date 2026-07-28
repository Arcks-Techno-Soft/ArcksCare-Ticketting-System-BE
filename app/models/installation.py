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
from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
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

    # Date the installation is expected to happen on site. Optional — captured
    # on the new-installation form and editable afterwards. Drives the upcoming-
    # installation WhatsApp reminder to Super Admin / Admin / Managers (see
    # services/installation_reminders.py). Nullable so rows that pre-date the
    # feature stay valid.
    expected_installation_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, index=True
    )
    # Stamped once the "upcoming installation" reminder has gone out, so the
    # scheduler never double-sends. Cleared whenever the expected date changes.
    expected_date_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Free-text list of products to install (one per line, name + quantity).
    # Mandatory at the API for new installations; nullable in the DB so the
    # idempotent migration can backfill installations that pre-date the feature.
    products_for_installation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Optional uploaded invoice document (PDF or image). A single file —
    # uploading again replaces it. The storage key is resolved to a viewable
    # URL on the way out (see InstallationInvoiceDocumentOut).
    invoice_document_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    invoice_document_content_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    invoice_document_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    invoice_document_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    invoice_document_uploaded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Site address (line 1, city, state, pincode required at the API; line 2/3
    # optional). Mirrors the ticket address fields. Columns are nullable so the
    # idempotent migration can backfill installations that pre-date the feature.
    address_line1: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    address_line3: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    pincode: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)

    # Optional geo — populated when a pin is dropped on the map (web).
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), default=InstallationStatus.NEW.value, index=True
    )

    # Who created it (Admin/Manager)
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Sales rep credited with sourcing this installation. Set at create-time by
    # an Admin/Manager (or self when a SALES user opens it). NULL when none picked.
    sales_rep_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Assignment (engineer, or owner/manager self-assignment)
    assigned_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_engineer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # On hold — Manager/Admin/Owner only. An overlay on `status`, NOT a status
    # of its own: the installation keeps its stage, so resuming just clears
    # these columns. While held it is frozen (notes still allowed), drops out
    # of the engineer's open-job count, and stops the upcoming-installation
    # reminders. Hold/resume history lives in the event log (HELD / RESUMED).
    held_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    held_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    hold_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
    # Work attempts (visits across multiple days). Each groups its own notes.
    attempts: Mapped[List["InstallationAttempt"]] = relationship(
        back_populates="installation",
        cascade="all, delete-orphan",
        order_by="InstallationAttempt.attempt_number",
    )
    events: Mapped[List["InstallationEvent"]] = relationship(
        back_populates="installation",
        cascade="all, delete-orphan",
        order_by="InstallationEvent.created_at",
    )
    resolution: Mapped[Optional["InstallationResolution"]] = relationship(
        back_populates="installation", cascade="all, delete-orphan", uselist=False
    )
    # Off-field contractors attending this installation. Not User rows — they
    # don't log in; they sign via the tokenized field-sign link.
    sub_engineers: Mapped[List["InstallationSubEngineer"]] = relationship(
        back_populates="installation",
        cascade="all, delete-orphan",
        order_by="InstallationSubEngineer.created_at",
    )

    created_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )
    sales_rep: Mapped[Optional["User"]] = relationship(
        foreign_keys=[sales_rep_id], lazy="joined"
    )
    assigned_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assigned_by_id], lazy="joined"
    )
    assigned_engineer: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assigned_engineer_id], lazy="joined"
    )
    held_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[held_by_id], lazy="joined"
    )

    @property
    def on_hold(self) -> bool:
        """True while the installation is parked. Derived so the API and every
        guard read the same thing rather than each testing `held_at`."""
        return self.held_at is not None

    @property
    def invoice_document(self) -> Optional[dict]:
        """Shape the invoice-document columns into a single object (or None).

        Read by InstallationInvoiceDocumentOut, which resolves `storage_url`
        into a viewable link via the active storage backend.
        """
        if not self.invoice_document_storage_key:
            return None
        return {
            "filename": self.invoice_document_filename,
            "content_type": self.invoice_document_content_type,
            "size_bytes": self.invoice_document_size_bytes,
            "storage_url": self.invoice_document_storage_key,
            "uploaded_at": self.invoice_document_uploaded_at,
        }


class InstallationNote(Base):
    __tablename__ = "installation_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), index=True
    )
    # The work attempt this note belongs to. Nullable so notes that pre-date the
    # attempts feature stay valid; new notes are always tied to the open attempt.
    installation_attempt_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("installation_attempts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    installation: Mapped["Installation"] = relationship(back_populates="notes")
    attempt: Mapped[Optional["InstallationAttempt"]] = relationship(back_populates="notes")
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


class InstallationAttempt(Base):
    """A single on-site work attempt for an installation.

    Engineers often need several visits across multiple days. Each attempt
    groups the notes + photos captured during it. `ended_at IS NULL` means the
    attempt is still open (work in progress); only one attempt may be open at a
    time. Finishing the installation requires at least one ended attempt.
    """

    __tablename__ = "installation_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    installation: Mapped["Installation"] = relationship(back_populates="attempts")
    started_by: Mapped[Optional["User"]] = relationship(lazy="joined")
    notes: Mapped[List["InstallationNote"]] = relationship(
        back_populates="attempt",
        order_by="InstallationNote.created_at",
    )


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

    # Optional photo of the customer captured alongside their signature.
    # Optional — never blocks closing. Embedded in the PDF.
    customer_photo_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    customer_photo_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    engineer_signature_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    engineer_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who signed the engineer side. Normally the assigned engineer; when an
    # off-field sub-engineer signs via the field link, their name is recorded
    # here and `signed_by_sub_engineer_id` points at them.
    engineer_signer_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Set when an Admin/Manager/engineer generates the off-field signing link.
    # Once set, on-site signing through the app is locked — the sub-engineer
    # captures both signatures through the public tokenized page instead.
    field_sign_link_generated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signed_by_sub_engineer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("installation_sub_engineers.id", ondelete="SET NULL"), nullable=True
    )

    pdf_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installation: Mapped["Installation"] = relationship(back_populates="resolution")
    # Installation photos captured by the sub-engineer at field sign-off.
    media: Mapped[List["InstallationResolutionMedia"]] = relationship(
        back_populates="resolution",
        cascade="all, delete-orphan",
        order_by="InstallationResolutionMedia.uploaded_at",
    )


class InstallationSubEngineer(Base):
    """An off-field contractor attending an installation.

    Mirrors the ticket-side `SubEngineer`: NOT a User row (they don't log in).
    The reusable district roster (`SubEngineerRoster`) is shared with tickets —
    a contractor added here is available on tickets too, and vice versa.
    """

    __tablename__ = "installation_sub_engineers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20))
    # Defaults to the installation's city, but editable for a nearby town.
    location: Mapped[str] = mapped_column(String(120))
    # Fee paid to this contractor (INR). Internal cost — never on the customer
    # PDF. NULL until recorded; unlike tickets this does NOT block closing.
    fee_inr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    installation: Mapped["Installation"] = relationship(back_populates="sub_engineers")
    created_by: Mapped[Optional["User"]] = relationship(lazy="joined")


class InstallationResolutionMedia(Base):
    """A photo of the completed installation, captured by the sub-engineer at
    field sign-off. Optional supporting evidence of the on-site work."""

    __tablename__ = "installation_resolution_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resolution_id: Mapped[int] = mapped_column(
        ForeignKey("installation_resolutions.id", ondelete="CASCADE"), index=True
    )
    # "photo" or "video" — derived from the content type at upload time.
    kind: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_url: Mapped[str] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    resolution: Mapped["InstallationResolution"] = relationship(back_populates="media")


from .user import User  # noqa: E402,F401
