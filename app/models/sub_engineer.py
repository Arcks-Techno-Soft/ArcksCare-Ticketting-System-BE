"""Sub-engineers — ad-hoc field contractors added to a ticket.

These are NOT users in the User table — they don't log in. Just a small list
of names + phone numbers that the assigned engineer (or Admin / Manager)
records for traceability. The main assignee remains responsible for the
actual ticket workflow and work-note updates.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class SubEngineer(Base):
    __tablename__ = "sub_engineers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20))
    # Defaults to the ticket's city at creation time, but editable in case the
    # sub-engineer covers a nearby town.
    location: Mapped[str] = mapped_column(String(120))

    # Fee paid to this outsourced contractor for their work on this ticket
    # (INR). Internal cost — NOT part of the customer invoice or resolution
    # PDF. NULL until a fee is recorded.
    fee_inr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="sub_engineers")
    created_by: Mapped[Optional["User"]] = relationship(lazy="joined")


class SubEngineerRoster(Base):
    """Master roster of field contractors, organised by district.

    Unlike `SubEngineer` (which records a contractor ON a specific ticket), a
    roster entry is a reusable contact. The ticket "Add sub-engineer" dropdown
    is populated from this roster, filtered by the ticket's city/district.
    Removing a contractor from a ticket never touches the roster, so the
    contact stays available for future tickets.
    """

    __tablename__ = "sub_engineer_roster"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20))
    district: Mapped[str] = mapped_column(String(80), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    created_by: Mapped[Optional["User"]] = relationship(lazy="joined")


# Type-only imports for forward references.
from .ticket import Ticket  # noqa: E402,F401
from .user import User  # noqa: E402,F401
