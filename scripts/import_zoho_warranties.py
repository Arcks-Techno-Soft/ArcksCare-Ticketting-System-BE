"""Load the historical Zoho sales exports into the warranty registry.

Zoho's invoice export carries one row per invoice LINE; the serialized lines
list every unit's serial number in a comma-separated "Serial Numbers" cell.
This script expands those into one `warranties` row per unit so staff can type
any old serial into Warranty Management and get the correct under-warranty /
expired answer.

USAGE (from backend/ directory):
    python -m scripts.import_zoho_warranties --files "<dir with .xls>"            # dry-run
    python -m scripts.import_zoho_warranties --files "<dir with .xls>" --commit
    python -m scripts.import_zoho_warranties --rollback --yes

Dry-run is the default: nothing is written, but the full CSV report and the
summary are produced, so the numbers can be reviewed before committing.

Safety properties:
  * Serials already in `warranties` are SKIPPED, never overwritten — a manual
    registration always wins, and re-running the import is idempotent.
  * Every inserted row is stamped `source='ZOHO_IMPORT'`, so `--rollback`
    (DELETE WHERE source='ZOHO_IMPORT') can never remove a manual entry.
  * `--commit` writes in a single transaction: all 2,299 rows or none.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

# Allow `python -m scripts.import_zoho_warranties` from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import xlrd  # legacy .xls reader — openpyxl only handles .xlsx
except ImportError:  # pragma: no cover - guidance for a fresh checkout
    print("xlrd is required to read .xls files. Run: pip install xlrd", file=sys.stderr)
    raise

from app.database import SessionLocal  # noqa: E402

# Importing the model package (plus the two modules it doesn't re-export)
# registers the full mapper graph. Without it, the first Warranty query fails
# while configuring Ticket's relationships to classes that were never imported.
from app import models as _models  # noqa: E402,F401
from app.models import ticket_engineer as _te  # noqa: E402,F401
from app.models import ticket_reminder as _tr  # noqa: E402,F401
from app.models.warranty import Warranty  # noqa: E402

# Reuse the API's month-clamped date maths so an imported expiry can never
# disagree with one calculated at manual registration time.
from app.routers.warranties import _add_months  # noqa: E402

SOURCE_TAG = "ZOHO_IMPORT"
IMPORT_NOTES = "Imported from Zoho"

# Zoho column headers we need. Resolved by NAME, never by position — the export
# has ~184 columns and their order is not stable between report runs.
COL_DATE = "Invoice Date"
COL_NUMBER = "Invoice Number"
COL_STATUS = "Invoice Status"
COL_CUSTOMER = "Customer Name"
COL_ITEM = "Item Name"
COL_SERIALS = "Serial Numbers"
REQUIRED_COLUMNS = (COL_DATE, COL_NUMBER, COL_STATUS, COL_CUSTOMER, COL_ITEM, COL_SERIALS)

# Invoices in these states never became a sale, so they carry no warranty.
EXCLUDED_STATUSES = {"void", "draft"}

# Product families that ship with a 12-month warranty even though the name also
# matches a 36-month pattern (an Android kiosk is still a 12-month kiosk).
ANDROID_KEYWORDS = ("ANDROID", "RK3568", "RK-3566", "RK3566")


def classify_months(item_name: str) -> int:
    """Warranty duration in months implied by the product name.

    Zoho does not export the warranty term, so it is derived from the item
    name. Rules are checked IN ORDER — the first match wins, which is what
    makes "Android kiosk" resolve to 12 rather than falling through to the
    36-month touch-series rule.
    """
    name = " ".join(item_name.split()).upper()
    if "KIOSK" in name:
        return 12
    if "COMPACT SERIES" in name:
        return 12
    if any(k in name for k in ANDROID_KEYWORDS):
        return 12
    if "SK-POS PREMIUM" in name:
        return 36
    if (
        "EXTREME SERIES" in name
        or "MIGHTY SERIES" in name
        or ("TOUCH" in name and "SERIES" in name)
    ):
        return 36
    return 12


def is_android(item_name: str) -> bool:
    """Whether the unit is an Android device — used only to split the summary
    into its three reporting buckets (36mo / 12mo Android / 12mo other)."""
    name = " ".join(item_name.split()).upper()
    return any(k in name for k in ANDROID_KEYWORDS)


def _parse_invoice_date(value, datemode: int) -> Optional[date]:
    """Zoho writes the date as an ISO text cell, but the same export can come
    back as a real Excel date cell — handle both rather than trusting one."""
    if isinstance(value, float):  # native Excel serial date
        y, m, d = xlrd.xldate_as_tuple(value, datemode)[:3]
        return date(y, m, d)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class Unit:
    """One serialized unit — the thing that becomes one `warranties` row."""

    __slots__ = (
        "serial", "serial_norm", "product_name", "invoice_number",
        "invoice_date", "customer_name", "source_file", "months", "expiry_date",
    )

    def __init__(self, serial, serial_norm, product_name, invoice_number,
                 invoice_date, customer_name, source_file):
        self.serial = serial
        self.serial_norm = serial_norm
        self.product_name = product_name
        self.invoice_number = invoice_number
        self.invoice_date = invoice_date
        self.customer_name = customer_name
        self.source_file = source_file
        self.months = classify_months(product_name)
        self.expiry_date = _add_months(invoice_date, self.months)

    @property
    def is_active(self) -> bool:
        return date.today() <= self.expiry_date


def parse_files(directory: str) -> Tuple[List[Unit], List[str]]:
    """Expand every .xls in `directory` into per-unit rows.

    Returns (units, warnings). Rows without a serial number (roughly two thirds
    of all hardware lines) and Void/Draft invoices are dropped here.
    """
    warnings: List[str] = []
    paths = sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.lower().endswith(".xls") and not f.startswith("~$")
    )
    if not paths:
        raise SystemExit(f"No .xls files found in {directory}")

    units: List[Unit] = []
    for path in paths:
        name = os.path.basename(path)
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_index(0)
        header = [str(h).strip() for h in sheet.row_values(0)]
        index = {h: i for i, h in enumerate(header)}
        missing = [c for c in REQUIRED_COLUMNS if c not in index]
        if missing:
            # The Apr-2024→Sep-2024 export predates the Serial Numbers column;
            # it simply contributes no units instead of failing the run.
            warnings.append(f"{name}: skipped — missing column(s) {', '.join(missing)}")
            continue

        rows_used = 0
        for r in range(1, sheet.nrows):
            row = sheet.row_values(r)
            raw_serials = str(row[index[COL_SERIALS]]).strip()
            if not raw_serials:
                continue  # non-serialized line (consumables, services, ...)
            status = str(row[index[COL_STATUS]]).strip()
            if status.lower() in EXCLUDED_STATUSES:
                continue
            invoice_date = _parse_invoice_date(
                row[index[COL_DATE]], book.datemode
            )
            if invoice_date is None:
                warnings.append(f"{name}: row {r + 1} has an unreadable invoice date — skipped")
                continue
            product = " ".join(str(row[index[COL_ITEM]]).split())
            invoice_number = str(row[index[COL_NUMBER]]).strip()
            customer = " ".join(str(row[index[COL_CUSTOMER]]).split())

            for raw in raw_serials.split(","):
                serial = raw.strip()
                if not serial:
                    continue
                units.append(Unit(
                    serial=serial,
                    serial_norm=Warranty.normalise_serial(serial),
                    product_name=product,
                    invoice_number=invoice_number,
                    invoice_date=invoice_date,
                    customer_name=customer,
                    source_file=name,
                ))
                rows_used += 1
        print(f"  parsed {name}: {rows_used} serialized units")
    return units, warnings


def dedupe(units: List[Unit]) -> Tuple[Dict[str, Unit], List[Tuple[Unit, Unit]]]:
    """Keep one unit per normalised serial — the one on the LATEST invoice.

    A handful of serials appear twice because the unit was resold or swapped
    out; the warranty restarts on the later sale. Returns the winners plus
    (loser, winner) pairs so the report can show what was discarded.
    """
    winners: Dict[str, Unit] = {}
    discarded: List[Tuple[Unit, Unit]] = []
    for unit in units:
        current = winners.get(unit.serial_norm)
        if current is None:
            winners[unit.serial_norm] = unit
            continue
        if unit.invoice_date > current.invoice_date:
            winners[unit.serial_norm] = unit
            discarded.append((current, unit))
        else:
            discarded.append((unit, current))
    return winners, discarded


def write_report(path: str, rows: List[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "serial", "product", "invoice_number", "invoice_date",
            "warranty_months", "expiry_date", "active", "status", "detail",
        ])
        writer.writeheader()
        writer.writerows(rows)


def build_report_rows(
    winners: Dict[str, Unit],
    discarded: List[Tuple[Unit, Unit]],
    existing: Dict[str, Warranty],
) -> Tuple[List[dict], List[Unit]]:
    """One report row per unit seen (including the dedupe losers), plus the
    list of units that will actually be inserted."""
    rows: List[dict] = []
    to_insert: List[Unit] = []

    def row(unit: Unit, status: str, detail: str = "") -> dict:
        return {
            "serial": unit.serial,
            "product": unit.product_name,
            "invoice_number": unit.invoice_number,
            "invoice_date": unit.invoice_date.isoformat(),
            "warranty_months": unit.months,
            "expiry_date": unit.expiry_date.isoformat(),
            "active": "y" if unit.is_active else "n",
            "status": status,
            "detail": detail,
        }

    for unit in sorted(winners.values(), key=lambda u: (u.invoice_date, u.serial_norm)):
        already = existing.get(unit.serial_norm)
        if already is not None:
            rows.append(row(
                unit, "skip-existing",
                f"already registered as '{already.serial_number}' ({already.product_name})",
            ))
            continue
        rows.append(row(unit, "import"))
        to_insert.append(unit)

    for loser, winner in discarded:
        rows.append(row(
            loser, "skip-duplicate",
            f"superseded by invoice {winner.invoice_number} of {winner.invoice_date.isoformat()}",
        ))
    return rows, to_insert


def print_summary(
    winners: Dict[str, Unit],
    discarded: List[Tuple[Unit, Unit]],
    rows: List[dict],
    to_insert: List[Unit],
    warnings: List[str],
) -> None:
    buckets = Counter()
    active = 0
    for unit in winners.values():
        if unit.months == 36:
            buckets["36mo"] += 1
        elif is_android(unit.product_name):
            buckets["12mo-android"] += 1
        else:
            buckets["12mo-other"] += 1
        if unit.is_active:
            active += 1
    statuses = Counter(r["status"] for r in rows)

    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    print(f"  unique units (after dedupe) : {len(winners)}")
    print(f"  duplicate serials discarded : {len(discarded)}")
    print("  buckets:")
    print(f"    36 months                 : {buckets['36mo']}")
    print(f"    12 months (Android)       : {buckets['12mo-android']}")
    print(f"    12 months (other)         : {buckets['12mo-other']}")
    print(f"  as of {date.today().isoformat()}:")
    print(f"    active                    : {active}")
    print(f"    expired                   : {len(winners) - active}")
    print("  report rows:")
    for status in ("import", "skip-existing", "skip-duplicate"):
        print(f"    {status:<26}: {statuses.get(status, 0)}")
    print(f"  rows to insert              : {len(to_insert)}")
    if warnings:
        print("  warnings:")
        for w in warnings:
            print(f"    - {w}")
    print("=" * 62)


def _target_db() -> str:
    """Human-readable target for the confirmation line — credentials stripped."""
    from app.config import get_settings
    url = get_settings().database_url
    return re.sub(r"//[^@/]*@", "//***@", url)


def do_commit(to_insert: List[Unit]) -> int:
    """Insert every unit in ONE transaction — all or nothing."""
    if not to_insert:
        print("Nothing to insert.")
        return 0
    with SessionLocal() as db:
        db.add_all([
            Warranty(
                product_name=u.product_name,
                serial_number=u.serial,
                serial_number_norm=u.serial_norm,
                invoice_number=u.invoice_number,
                sale_date=u.invoice_date,
                warranty_months=u.months,
                expiry_date=u.expiry_date,
                notes=IMPORT_NOTES,
                source=SOURCE_TAG,
                customer_name=u.customer_name,
                created_by_id=None,  # not registered by a staff member
            )
            for u in to_insert
        ])
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    print(f"Committed {len(to_insert)} warranty rows to {_target_db()}")
    return len(to_insert)


def do_rollback(confirmed: bool) -> int:
    """Delete ONLY the rows this import created. Manual rows have source NULL
    and are untouchable by this query."""
    with SessionLocal() as db:
        count = db.query(Warranty).filter(Warranty.source == SOURCE_TAG).count()
        print(f"{count} imported rows (source='{SOURCE_TAG}') in {_target_db()}")
        if not confirmed:
            print("Refusing to delete. Re-run with --yes to confirm.", file=sys.stderr)
            return count
        if count == 0:
            return 0
        db.query(Warranty).filter(Warranty.source == SOURCE_TAG).delete(
            synchronize_session=False
        )
        db.commit()
    print(f"Deleted {count} imported warranty rows.")
    return count


def main() -> int:
    default_report = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "warranty_import_report.csv"
    )
    parser = argparse.ArgumentParser(
        description="Import historical Zoho invoice serials into the warranty registry."
    )
    parser.add_argument("--files", help="Directory containing the Zoho .xls exports")
    parser.add_argument("--report", default=default_report,
                        help=f"CSV report path (default: {default_report})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Parse + report only, write nothing (default)")
    mode.add_argument("--commit", action="store_true",
                      help="Insert the rows in a single transaction")
    mode.add_argument("--rollback", action="store_true",
                      help=f"Delete rows with source='{SOURCE_TAG}' (requires --yes)")
    parser.add_argument("--yes", action="store_true", help="Confirm a --rollback")
    args = parser.parse_args()

    if args.rollback:
        do_rollback(args.yes)
        return 0

    if not args.files:
        parser.error("--files is required for --dry-run / --commit")
    if not os.path.isdir(args.files):
        parser.error(f"--files must be a directory: {args.files}")

    committing = args.commit
    print(f"Mode: {'COMMIT' if committing else 'DRY RUN'}   DB: {_target_db()}")
    print(f"Reading .xls exports from {args.files}")
    units, warnings = parse_files(args.files)
    print(f"  total serialized units parsed: {len(units)}")

    winners, discarded = dedupe(units)
    for loser, winner in discarded:
        print(
            f"  dedupe: serial {loser.serial} on invoice {loser.invoice_number} "
            f"({loser.invoice_date}) superseded by {winner.invoice_number} "
            f"({winner.invoice_date})"
        )

    # Manual registrations always win — look up everything we're about to write.
    with SessionLocal() as db:
        existing: Dict[str, Warranty] = {}
        norms = list(winners.keys())
        for i in range(0, len(norms), 500):  # chunked: some drivers cap IN() size
            for row in db.query(Warranty).filter(
                Warranty.serial_number_norm.in_(norms[i:i + 500])
            ).all():
                existing[row.serial_number_norm] = row

    rows, to_insert = build_report_rows(winners, discarded, existing)
    write_report(args.report, rows)
    print(f"\nReport written: {args.report} ({len(rows)} rows)")
    print_summary(winners, discarded, rows, to_insert, warnings)

    if committing:
        do_commit(to_insert)
    else:
        print("\nDry run — nothing written. Re-run with --commit to insert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
