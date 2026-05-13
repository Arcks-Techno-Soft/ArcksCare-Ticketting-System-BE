"""Drop and recreate all ArcksCare tables. Use after pulling schema changes.

USAGE (from backend/ directory):
    python -m scripts.reset_db

Safety: only runs if RESET_DB_CONFIRM=YES is set in the environment, OR you
pass --yes as an argument. This prevents accidental wipes in production.

What it does:
  1. Drops `ticket_events`, `ticket_attachments`, `tickets`, `users` (cascade)
  2. Recreates them from the current SQLAlchemy models
  3. Re-seeds Owner / Manager / Balaji / Ranjith
"""
from __future__ import annotations

import os
import sys

# Allow `python -m scripts.reset_db` from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402

# Importing models registers them on Base.metadata
from app.models import ticket as _t  # noqa: E402,F401
from app.models import user as _u  # noqa: E402,F401
from app.services.auth import seed_initial_users  # noqa: E402


def main() -> int:
    confirmed = (
        os.environ.get("RESET_DB_CONFIRM", "").upper() == "YES"
        or "--yes" in sys.argv
    )
    if not confirmed:
        print(
            "Refusing to run. Pass --yes or set RESET_DB_CONFIRM=YES.\n"
            "This will DELETE all tickets, attachments, users, and events.",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    print(f"Target DB: {settings.database_url.split('@')[-1]}")
    print("Dropping tables…")
    Base.metadata.drop_all(bind=engine)
    print("Recreating tables…")
    Base.metadata.create_all(bind=engine)
    print("Seeding users…")
    with SessionLocal() as db:
        seed_initial_users(db)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
