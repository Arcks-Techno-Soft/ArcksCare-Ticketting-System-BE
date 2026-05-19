"""Spare-parts shipment records attached to a ticket.

Logistics info — separate from `TicketSpare` (which is the billable line
items on the invoice). A ticket can have multiple shipments (partial sends,
follow-up parts) and each shipment has one-or-more items referencing the
spare catalog (with an ad-hoc fallback name).
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class TicketShipment(Base):
    __tablename__ = "ticket_shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )

    courier_name: Mapped[str] = mapped_column(String(120))
    # Tracking ID is optional — small couriers and own-vehicle dispatch
    # often don't generate one. We still let staff record courier + items.
    tracking_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    # When the parts physically left the warehouse / origin.
    departed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # When the parts reached the destination. NULL until the user closes the
    # shipment by hitting "Mark delivered" — that's the only signal we have
    # for actual receipt at the worksite.
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ticket: Mapped["Ticket"] = relationship(back_populates="shipments")
    created_by: Mapped[Optional["User"]] = relationship(lazy="joined")
    items: Mapped[List["TicketShipmentItem"]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="TicketShipmentItem.created_at",
        lazy="selectin",
    )


class TicketShipmentItem(Base):
    __tablename__ = "ticket_shipment_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("ticket_shipments.id", ondelete="CASCADE"), index=True
    )
    # Nullable: ad-hoc parts not yet in the catalog are still allowed.
    catalog_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("spare_catalog.id"), nullable=True
    )
    # Snapshot of the part name at shipment time — catalog names can change
    # and we want a stable historical record on each shipment.
    name: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    shipment: Mapped["TicketShipment"] = relationship(back_populates="items")
    catalog_item: Mapped[Optional["SpareCatalog"]] = relationship(lazy="joined")


# Forward refs
from .ticket import Ticket  # noqa: E402,F401
from .user import User  # noqa: E402,F401
from .spare import SpareCatalog  # noqa: E402,F401
