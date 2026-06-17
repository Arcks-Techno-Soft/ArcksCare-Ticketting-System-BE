"""Additional engineers assigned to a ticket.

Unlike the single `assigned_engineer_id` on a ticket (the PRIMARY engineer who
drives the workflow), this join table records EXTRA system users (engineers /
managers / admins) who also need to attend the same site visit. They can VIEW
the ticket and are notified, but only the primary assignee can drive the
workflow (accept / work notes / resolve). Distinct from `SubEngineer`, which
records non-login field contractors.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class TicketEngineer(Base):
    __tablename__ = "ticket_engineers"
    # A user can only be an additional engineer on a given ticket once.
    __table_args__ = (
        UniqueConstraint("ticket_id", "engineer_id", name="uq_ticket_engineer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    engineer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Who added this engineer (Admin / Manager / Owner). NULL only if the actor
    # was later deleted.
    added_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="additional_engineers")
    engineer: Mapped["User"] = relationship(foreign_keys=[engineer_id], lazy="joined")
    added_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[added_by_id], lazy="joined"
    )


# Type-only imports for forward references.
from .ticket import Ticket  # noqa: E402,F401
from .user import User  # noqa: E402,F401
