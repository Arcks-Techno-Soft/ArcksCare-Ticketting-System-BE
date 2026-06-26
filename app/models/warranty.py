"""Warranty registry model.

Each row registers the warranty for one sold unit, keyed by its product
**serial number** (the field engineers read off the device). A ticket raised
against a serial can then look up whether that unit is still covered: if
`today <= expiry_date`, service charge + spare parts are waived; otherwise they
are billable.

Warranty duration is captured at registration time (in months) because it is
NOT present in the Zoho sales export — it is implied by the product type and
entered by staff from a dropdown (6 / 12 / 18 / 24 / 36 / 48 months, or a
custom value). `expiry_date` is derived once at write time so lookups are a
plain date comparison with no per-request maths.

The serial number is unique (case-insensitively, via `serial_number_norm`) so
the same unit can never be registered twice — the create endpoint turns a
collision into a clear "already registered" error.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class Warranty(Base):
    __tablename__ = "warranties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # What was sold.
    product_name: Mapped[str] = mapped_column(String(300), index=True)

    # The serial number as the user typed it (preserved for display)...
    serial_number: Mapped[str] = mapped_column(String(120))
    # ...and a normalised (trimmed + upper-cased) copy used for the uniqueness
    # guarantee and for fast case-insensitive lookups from the ticket flow.
    serial_number_norm: Mapped[str] = mapped_column(
        String(120), unique=True, index=True
    )

    # Invoice number from the sale (mandatory at the API/form layer). Nullable on
    # the column so pre-existing rows registered before this field was added stay
    # valid; new registrations always carry one. Indexed for lookup/search.
    invoice_number: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True, index=True
    )

    # Warranty window. `sale_date` is the invoice date (surfaced in the UI as
    # "Invoice Date"); the column name is kept to avoid a rename migration.
    sale_date: Mapped[date] = mapped_column(Date, index=True)
    warranty_months: Mapped[int] = mapped_column(Integer)
    # Derived at write time: sale_date + warranty_months. Indexed so an
    # "expiring soon" query is cheap.
    expiry_date: Mapped[date] = mapped_column(Date, index=True)

    # Optional free-text note (invoice no., customer, remarks — "other options"
    # the user may want to jot down).
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    created_by: Mapped[Optional["User"]] = relationship(
        foreign_keys=[created_by_id], lazy="joined"
    )

    @staticmethod
    def normalise_serial(raw: str) -> str:
        """Trim + collapse whitespace + upper-case so 'sk 12 ' and 'SK12'
        compare equal. Used for both the uniqueness key and lookups."""
        return " ".join(raw.split()).upper()

    @property
    def is_active(self) -> bool:
        """True while the unit is still inside its warranty window (today
        inclusive of the expiry date)."""
        return date.today() <= self.expiry_date


from .user import User  # noqa: E402,F401
