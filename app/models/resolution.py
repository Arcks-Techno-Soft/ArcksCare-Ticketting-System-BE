"""Resolution document — created when an engineer marks a ticket RESOLVED.

Lifecycle:
  1. Engineer hits "Mark resolved" → service creates a Resolution row with a
     unique `customer_sign_token`. Customer is emailed a link to /sign/<token>.
  2. Customer opens the page, draws a signature, submits. We save the PNG to
     storage and set `customer_signed_at`.
  3. Engineer then signs from inside the admin app. Once both signatures are
     in place, the service generates the PDF, sets `pdf_storage_key`, and
     transitions the ticket to CLOSED.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class Resolution(Base):
    __tablename__ = "resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), unique=True, index=True)

    # Customer signing link
    customer_sign_token: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    customer_sign_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Customer signature
    customer_signer_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    customer_signature_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    customer_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Engineer signature
    engineer_signature_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    engineer_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Name shown for the second (engineer) signature. Populated with the
    # sub-engineer's name in the remote field-signing flow; stays NULL for the
    # on-site flow, where the PDF falls back to the assigned engineer's name.
    engineer_signer_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # Remote field-signing link. When set, a sub-engineer collects BOTH
    # signatures off-site via /field-sign/<token>, and on-site signing in the
    # admin app is locked for this ticket.
    field_sign_link_generated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Which sub-engineer signed remotely (field-signing flow only).
    signed_by_sub_engineer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sub_engineers.id", ondelete="SET NULL"), nullable=True
    )

    # PDF
    pdf_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="resolution")


# Type-only import to make forward references resolve cleanly.
from .ticket import Ticket  # noqa: E402,F401
