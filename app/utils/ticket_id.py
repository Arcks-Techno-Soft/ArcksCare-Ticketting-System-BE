"""Human-friendly ticket reference generator.

Format: AC-{YYYY}-{NNNNN}  e.g. AC-2026-00042

The numeric suffix is the row's primary key zero-padded to 5 digits. We
generate the reference after the row has been flushed (so we have an id).
"""
from datetime import datetime
from typing import Optional


def make_reference(ticket_id: int, year: Optional[int] = None) -> str:
    year = year or datetime.utcnow().year
    return f"AC-{year}-{ticket_id:05d}"
