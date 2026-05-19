"""Spare parts catalog + per-ticket spare line items.

SpareCatalog is a small master list (4-5 typical parts per product category)
seeded at startup. TicketSpare is what the engineer actually used on a given
ticket — a snapshot of name + unit_price + quantity, because catalog prices
can change later and we want the historical record to be stable.

Pricing rules live in the service layer / API: warranty tickets bill spares
at zero regardless of the stored unit price.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class SpareCatalog(Base):
    __tablename__ = "spare_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_category: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160))
    default_price_inr: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TicketSpare(Base):
    __tablename__ = "ticket_spares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    # Nullable — engineers can add an ad-hoc part not in the catalog.
    catalog_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("spare_catalog.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160))
    # INR, whole rupees. Engineer can override the catalog default.
    unit_price_inr: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="spares")
    catalog_item: Mapped[Optional["SpareCatalog"]] = relationship(lazy="joined")
    created_by: Mapped[Optional["User"]] = relationship(lazy="joined")


from .ticket import Ticket  # noqa: E402,F401
from .user import User  # noqa: E402,F401
