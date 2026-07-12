"""One-off migration: rename the legacy user role OWNER -> SUPER_ADMIN.

OWNER was a legacy alias treated as top-tier admin. It has been renamed to the
distinct SUPER_ADMIN role. Backend + clients still recognise OWNER as
SUPER_ADMIN during the transition, so this migration is safe to run at any time
and is idempotent — it only rewrites rows still carrying role='OWNER'.

Usage (from the backend dir, with the app venv):
    PYTHONPATH="$PWD" .venv/bin/python -m scripts.migrate_owner_to_super_admin          # dry run
    PYTHONPATH="$PWD" .venv/bin/python -m scripts.migrate_owner_to_super_admin --apply   # write
"""
import sys

import app.models  # noqa: F401  — register mappers
from app.models import ticket_engineer, ticket_reminder  # noqa: F401
from app.database import SessionLocal, DB_SCHEMA
from app.models.user import User, UserRole

APPLY = "--apply" in sys.argv


def main() -> int:
    with SessionLocal() as db:
        print(f"DB schema in use: {DB_SCHEMA!r}")
        rows = db.query(User).filter(User.role == UserRole.OWNER.value).order_by(User.id).all()
        if not rows:
            print("No rows with role='OWNER'. Nothing to migrate.")
            return 0

        print(f"\n{len(rows)} row(s) with role='OWNER':")
        for u in rows:
            print(f"  #{u.id:<4} {u.name:<24} @{u.username:<20} active={u.active}")

        if not APPLY:
            print("\n[DRY RUN] No changes written. Re-run with --apply.")
            return 0

        for u in rows:
            u.role = UserRole.SUPER_ADMIN.value
        db.commit()
        print(f"\n[APPLIED] {len(rows)} row(s) set to role='SUPER_ADMIN'.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
