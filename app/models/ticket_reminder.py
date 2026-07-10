"""Per-ticket, per-stage SLA reminder tracking.

The reminder scheduler (``services/reminders.py``) sends looping WhatsApp
reminders while a ticket is stuck at an early workflow stage:

* ``ACK``    — raised but not yet acknowledged   (every 10 min)
* ``ASSIGN`` — acknowledged but not yet assigned (every 10 min)
* ``ACCEPT`` — assigned but engineer hasn't accepted (every 30 min)

One row per ``(ticket_id, stage)`` records how many reminders have already
gone out (``sent_count``) and when the last one fired (``last_sent_at``).
This is what enforces the per-stage cap (default 5) and keeps the schedule
correct across app restarts — the counter lives in the DB, not in memory.

Intentionally decoupled from the ``Ticket`` ORM (no relationship / cascade):
the scheduler joins by ``ticket_id`` only, so this table can be added or
dropped without touching the tickets model. Rows are harmless to leave
behind once a ticket moves on.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


# Stage identifiers stored in the `stage` column.
STAGE_ACK = "ACK"        # OPEN -> not acknowledged
STAGE_ASSIGN = "ASSIGN"  # ACKNOWLEDGED -> not assigned
STAGE_ACCEPT = "ACCEPT"  # ASSIGNED -> engineer not accepted


class TicketReminder(Base):
    __tablename__ = "ticket_reminders"
    __table_args__ = (
        UniqueConstraint("ticket_id", "stage", name="uq_ticket_reminder_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    # One of STAGE_ACK / STAGE_ASSIGN / STAGE_ACCEPT.
    stage: Mapped[str] = mapped_column(String(16), index=True)
    # How many reminders have been sent for this ticket at this stage.
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # When the most recent reminder fired (NULL until the first send).
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
