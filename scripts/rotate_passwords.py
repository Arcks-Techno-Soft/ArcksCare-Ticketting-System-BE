"""Rotate passwords for EXISTING users and retire demo accounts.

Why this exists: `seed_initial_users` only runs on an empty `users` table, so a
database that was first booted with the old weak defaults (owner123 / admin123 /
balaji123 / ranjith123) still contains those accounts. Hardening the seed code
does NOT fix them — this one-shot script does.

USAGE (from backend/ directory, with the venv active and .env pointing at the
target database):

    # Rotate one account (repeatable):
    ROTATE_USER=owner ROTATE_PASSWORD='a-strong-password' python -m scripts.rotate_passwords

    # Deactivate the demo engineer logins entirely:
    python -m scripts.rotate_passwords --deactivate-demo

    # List accounts so you know what's there (no changes):
    python -m scripts.rotate_passwords --list

Nothing is changed unless you pass an explicit action. New passwords are read
from the environment so they never land in shell history files or this repo.
"""
from __future__ import annotations

import os
import sys

# Allow `python -m scripts.rotate_passwords` from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import _WEAK_SEED_PASSWORDS  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.auth import hash_password, verify_password  # noqa: E402

# Demo logins shipped for local dev that should not exist in production.
DEMO_USERNAMES = ("balaji", "ranjith")
MIN_PASSWORD_LEN = 8


def _list(db) -> int:
    users = db.query(User).order_by(User.id).all()
    if not users:
        print("No users in the database.")
        return 0
    print(f"{'id':>3}  {'username':<20} {'role':<10} active  weak-default?")
    for u in users:
        # We can only flag the *known* weak defaults by re-hashing a guess.
        weak = any(verify_password(p, u.password_hash) for p in _WEAK_SEED_PASSWORDS)
        flag = "YES <-- fix" if weak else "no"
        print(f"{u.id:>3}  {u.username:<20} {u.role:<10} {str(u.active):<6}  {flag}")
    return 0


def _deactivate_demo(db) -> int:
    changed = 0
    for username in DEMO_USERNAMES:
        u = db.query(User).filter(User.username == username).one_or_none()
        if u is None:
            print(f"  (no account '{username}')")
            continue
        if not u.active:
            print(f"  '{username}' already inactive")
            continue
        u.active = False
        changed += 1
        print(f"  deactivated '{username}'")
    db.commit()
    print(f"Done. {changed} account(s) deactivated.")
    return 0


def _rotate(db, username: str, new_password: str) -> int:
    if len(new_password) < MIN_PASSWORD_LEN:
        print(f"Refusing: password must be at least {MIN_PASSWORD_LEN} characters.",
              file=sys.stderr)
        return 2
    if new_password in _WEAK_SEED_PASSWORDS:
        print("Refusing: that is a known weak/default password.", file=sys.stderr)
        return 2
    u = db.query(User).filter(User.username == username).one_or_none()
    if u is None:
        print(f"Refusing: no user named '{username}'.", file=sys.stderr)
        return 2
    u.password_hash = hash_password(new_password)
    db.commit()
    print(f"Rotated password for '{username}'. Existing sessions stay valid until "
          "their JWT expires — rotate JWT_SECRET too if you need to force re-login.")
    return 0


def main() -> int:
    with SessionLocal() as db:
        if "--list" in sys.argv:
            return _list(db)
        if "--deactivate-demo" in sys.argv:
            return _deactivate_demo(db)

        username = os.environ.get("ROTATE_USER", "").strip()
        new_password = os.environ.get("ROTATE_PASSWORD", "")
        if username and new_password:
            return _rotate(db, username, new_password)

    print(__doc__)
    print("No action taken. Pass --list, --deactivate-demo, or set "
          "ROTATE_USER + ROTATE_PASSWORD.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
