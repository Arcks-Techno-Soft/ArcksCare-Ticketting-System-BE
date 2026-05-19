"""Human-friendly ticket reference generator.

Format: {STATE_CODE}-{YYYY}-{NNNNN}  e.g. UP-2026-00042

The prefix is the RTO-style 2-letter code for the customer's state (Uttar
Pradesh → UP, Karnataka → KA, etc.). If the state is missing or unrecognised
the prefix falls back to "AC" — preserving the legacy format so the column
never ends up blank. The numeric suffix is the row's primary key zero-padded
to 5 digits, generated after the row has been flushed (so we have an id).
"""
from datetime import datetime
from typing import Optional


# RTO-style state codes used on Indian vehicle registrations. We use these as
# the ticket-reference prefix so a glance at a ticket number hints at where
# it came from.
STATE_CODES: dict[str, str] = {
    "Andhra Pradesh": "AP",
    "Arunachal Pradesh": "AR",
    "Assam": "AS",
    "Bihar": "BR",
    "Chhattisgarh": "CG",
    "Goa": "GA",
    "Gujarat": "GJ",
    "Haryana": "HR",
    "Himachal Pradesh": "HP",
    "Jharkhand": "JH",
    "Karnataka": "KA",
    "Kerala": "KL",
    "Madhya Pradesh": "MP",
    "Maharashtra": "MH",
    "Manipur": "MN",
    "Meghalaya": "ML",
    "Mizoram": "MZ",
    "Nagaland": "NL",
    "Odisha": "OD",
    "Punjab": "PB",
    "Rajasthan": "RJ",
    "Sikkim": "SK",
    "Tamil Nadu": "TN",
    "Telangana": "TS",
    "Tripura": "TR",
    "Uttar Pradesh": "UP",
    "Uttarakhand": "UK",
    "West Bengal": "WB",
    "Delhi": "DL",
    "Jammu and Kashmir": "JK",
    "Ladakh": "LA",
    "Chandigarh": "CH",
    "Puducherry": "PY",
    "Andaman and Nicobar Islands": "AN",
    "Dadra and Nagar Haveli and Daman and Diu": "DD",
    "Lakshadweep": "LD",
}


def state_code(state: Optional[str]) -> str:
    """Return the 2-letter RTO-style code for an Indian state, or "AC" if
    the state is missing/unknown. Matching is case-insensitive and tolerates
    surrounding whitespace."""
    if not state:
        return "AC"
    key = state.strip()
    if key in STATE_CODES:
        return STATE_CODES[key]
    # Case-insensitive fallback so "uttar pradesh" matches "Uttar Pradesh".
    lower = key.lower()
    for name, code in STATE_CODES.items():
        if name.lower() == lower:
            return code
    return "AC"


def make_reference(
    ticket_id: int,
    state: Optional[str] = None,
    year: Optional[int] = None,
) -> str:
    year = year or datetime.utcnow().year
    return f"{state_code(state)}-{year}-{ticket_id:05d}"
