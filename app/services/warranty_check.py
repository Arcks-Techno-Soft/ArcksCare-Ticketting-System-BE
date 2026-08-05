"""Check a ticket's serial number against the warranty registry.

Backs the "Check warranty status" button. Three things here are worth knowing
because they come from how the registry data actually looks in production:

* **Serials are matched ignoring whitespace.** A serial read off a device may
  be typed with spaces the registry doesn't have (or vice versa).

* **A registry row can hold several serials.** 18 rows carry comma- or
  space-separated lists ("UL2512231275, UL2512231297, ..."), covering 34 units
  that an exact-match lookup would wrongly report as unregistered. Those lists
  are split and each part matched. This is why whitespace can't simply be
  stripped from the stored value — "A B" may be two serials, not one.

* **"Today" is an IST calendar day.** The server runs UTC; India is UTC+5:30.
  Using the server's date would disagree with the engineer's calendar for the
  5.5 hours after UTC midnight, which matters on the day a warranty expires.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.ticket import Ticket, WarrantyStatus
from ..models.warranty import Warranty
from .reports import IST

logger = logging.getLogger("skposcare.warranty_check")

#: Serial lists inside one registry field are separated by commas, semicolons,
#: slashes or plain whitespace.
_SEPARATORS = re.compile(r"[,;/\s]+")


def squash(raw: Optional[str]) -> str:
    """Drop every space and upper-case, so 'sk 12 ' and 'SK12' compare equal.

    Note this is deliberately NOT Warranty.normalise_serial, which collapses
    runs of whitespace to a single space rather than removing them (its
    docstring claims otherwise, but 'sk 12' != 'SK12' under it). Matching here
    has to ignore spacing entirely.
    """
    return "".join((raw or "").split()).upper()


def _candidate_serials(row: Warranty) -> list[str]:
    """Every individual serial a registry row covers, whitespace-squashed."""
    parts = [p for p in _SEPARATORS.split(row.serial_number_norm or "") if p]
    # A single-serial row splits to one part; a list row splits to many.
    return [squash(p) for p in parts]


def find_warranty(db: Session, serial: str) -> Optional[Warranty]:
    """Locate the registry row covering `serial`, or None.

    Tries the cheap indexed paths first and only falls back to scanning the
    handful of multi-serial rows, so the common case stays a single index hit.
    """
    wanted = squash(serial)
    if not wanted:
        return None

    # 1. Exact match on the stored normalised form (indexed).
    row = (
        db.query(Warranty)
        .filter(Warranty.serial_number_norm == Warranty.normalise_serial(serial))
        .one_or_none()
    )
    if row is not None:
        return row

    # 2. The serial was typed with spacing the registry doesn't have (or vice
    #    versa). A stored value with no whitespace already equals its squashed
    #    form, so comparing against `wanted` stays an indexed equality lookup.
    row = (
        db.query(Warranty)
        .filter(Warranty.serial_number_norm == wanted)
        .one_or_none()
    )
    if row is not None:
        return row

    # 3. Multi-serial rows: match the individual parts. Only rows containing a
    #    separator can hold a list, so this scan covers ~18 rows, not the table.
    #    Deliberately compares parts and never the whole squashed value — the
    #    row "242508520421 252508520419" is two units, and the 24-digit
    #    concatenation is not a serial anyone owns.
    listish = (
        db.query(Warranty)
        .filter(
            or_(
                Warranty.serial_number_norm.contains(" "),
                Warranty.serial_number_norm.contains(","),
                Warranty.serial_number_norm.contains(";"),
                Warranty.serial_number_norm.contains("/"),
            )
        )
        .all()
    )
    for candidate in listish:
        if wanted in _candidate_serials(candidate):
            return candidate
    return None


def today_ist():
    """The current calendar date in IST — see the module docstring."""
    return datetime.now(IST).date()


def verdict_for(row: Warranty) -> str:
    """UNDER_WARRANTY while today (IST) is on or before the expiry date."""
    return (
        WarrantyStatus.UNDER_WARRANTY.value
        if today_ist() <= row.expiry_date
        else WarrantyStatus.OUT_OF_WARRANTY.value
    )


def check_ticket_warranty(db: Session, ticket: Ticket) -> dict:
    """Resolve the ticket's serial against the registry and describe the result.

    Read-only — deciding whether to *apply* the verdict is the caller's job,
    because applying it can change what the customer is billed.
    """
    row = find_warranty(db, ticket.serial_number)
    current = ticket.warranty_status

    if row is None:
        return {
            "found": False,
            "serial_number": (ticket.serial_number or "").strip(),
            "verdict": None,
            "current_status": current,
            "requires_confirmation": False,
            "conflict_reason": None,
            "warranty": None,
            "message": "Invalid serial no.",
        }

    verdict = verdict_for(row)

    # Applying is safe only when it isn't quietly overturning an existing
    # answer. Both of these can move money: warranty status drives whether the
    # service fee and spares are billable.
    conflict_reason = None
    if current == WarrantyStatus.AMC.value:
        conflict_reason = "AMC"
    elif current in (
        WarrantyStatus.UNDER_WARRANTY.value,
        WarrantyStatus.OUT_OF_WARRANTY.value,
    ) and current != verdict:
        conflict_reason = "DIFFERS"

    label = "Under warranty" if verdict == WarrantyStatus.UNDER_WARRANTY.value else "Out of warranty"
    if conflict_reason == "AMC":
        message = (
            f"{label} per the warranty registry (expires "
            f"{row.expiry_date:%d %b %Y}).\n\nThis ticket is marked AMC, which the "
            "registry doesn't track. Replacing it will drop the AMC coverage."
        )
    elif conflict_reason == "DIFFERS":
        was = "Under warranty" if current == WarrantyStatus.UNDER_WARRANTY.value else "Out of warranty"
        message = (
            f"{label} per the warranty registry (expires "
            f"{row.expiry_date:%d %b %Y}).\n\nThis ticket is currently set to "
            f"{was}. Applying this will change what the customer is billed."
        )
    else:
        message = label

    return {
        "found": True,
        "serial_number": (ticket.serial_number or "").strip(),
        "verdict": verdict,
        "current_status": current,
        "requires_confirmation": conflict_reason is not None,
        "conflict_reason": conflict_reason,
        "warranty": {
            "product_name": row.product_name,
            "serial_number": row.serial_number,
            "invoice_number": row.invoice_number,
            "customer_name": row.customer_name,
            "sale_date": row.sale_date,
            "warranty_months": row.warranty_months,
            "expiry_date": row.expiry_date,
        },
        "message": message,
    }
