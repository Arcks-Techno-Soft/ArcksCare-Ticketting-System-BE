"""Authentication service - bcrypt password hashing + JWT issue/verify.

The login flow:
  1. POST /api/v1/auth/login {username, password}
  2. Service verifies bcrypt hash, issues short-lived JWT
  3. Client stores JWT in localStorage, sends as `Authorization: Bearer <jwt>`
  4. `get_current_user` dependency on /api/v1/admin/* routes validates JWT
     and loads the User row from the DB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import _WEAK_SEED_PASSWORDS, get_settings
from ..database import MIGRATION_SCHEMA, get_db, qualify
from ..models.user import User, UserRole

logger = logging.getLogger("sk-pos-care.auth")


# ----------------------------- password hashing -------------------------- #

def hash_password(plain: str) -> str:
    """Return a bcrypt hash for storage. Slow on purpose."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ----------------------------- JWT ---------------------------------------- #

def issue_token(user: User) -> tuple[str, datetime]:
    """Return (jwt_token, expires_at). Expiry comes from config."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.jwt_expires_hours)
    payload = {
        "sub": str(user.id),
        "uname": user.username,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, exp


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ----------------------------- FastAPI dependency ------------------------ #

def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the authenticated User from the Authorization header.

    Returns 401 if the header is missing, malformed, expired, or the user has
    been deactivated.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)

    user_id = int(payload.get("sub", 0))
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None or not user.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )
    return user


def get_optional_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Like `get_current_user`, but never raises — returns None when there's no
    usable credential.

    Used on the public ticket-intake endpoint: customers submit anonymously
    (no token), while staff submitting on a customer's behalf send their Bearer
    token, letting us record who raised the ticket. A bad/expired token is
    treated the same as anonymous rather than blocking the submission.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except HTTPException:
        return None
    user_id = int(payload.get("sub", 0))
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None or not user.active:
        return None
    return user


def require_role(*allowed: UserRole):
    """Dependency factory: only lets through users whose role is in `allowed`."""
    allowed_values = {r.value for r in allowed}
    # Legacy compatibility: OWNER is an alias for ADMIN. Any endpoint that
    # allows ADMIN also allows OWNER, until OWNER rows are migrated to ADMIN.
    if UserRole.ADMIN.value in allowed_values:
        allowed_values.add(UserRole.OWNER.value)

    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires one of: {sorted(allowed_values)}",
            )
        return user

    return _checker


# ----------------------------- Column migration ------------------------- #

def ensure_user_profile_columns(engine: Engine) -> None:
    """Add users.first_name / last_name / phone if missing.

    `create_all` doesn't add columns to existing tables; this runs the small
    ALTERs idempotently. Works on SQLite (dev) and Postgres (prod).
    """
    insp = inspect(engine)
    if "users" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.
    existing = {c["name"] for c in insp.get_columns("users", schema=MIGRATION_SCHEMA)}
    pending = [
        ("first_name", "VARCHAR(60)"),
        ("last_name", "VARCHAR(60)"),
        ("phone", "VARCHAR(20)"),
        ("district", "VARCHAR(80)"),
    ]
    with engine.begin() as conn:
        for name, sql_type in pending:
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE {qualify('users')} ADD COLUMN {name} {sql_type}"))
            logger.info("Added users.%s column", name)


# ----------------------------- Seed first-boot users --------------------- #

def seed_initial_users(db: Session) -> None:
    """If the users table is empty, create the configured seed accounts.

    This makes local dev painless: boot the app, log in immediately as
    owner/admin without any manual SQL. In production you'd run a one-shot
    script and never ship default passwords.
    """
    if db.query(User).count() > 0:
        return

    settings = get_settings()
    # (username, password, first_name, last_name, role)
    seeds = [
        (settings.seed_owner_username, settings.seed_owner_password,
         settings.seed_owner_name.split(" ")[0] if settings.seed_owner_name else "Admin",
         " ".join(settings.seed_owner_name.split(" ")[1:]) if settings.seed_owner_name and " " in settings.seed_owner_name else "",
         UserRole.ADMIN.value),
        (settings.seed_manager_username, settings.seed_manager_password,
         settings.seed_manager_name.split(" ")[0] if settings.seed_manager_name else "Manager",
         " ".join(settings.seed_manager_name.split(" ")[1:]) if settings.seed_manager_name and " " in settings.seed_manager_name else "",
         UserRole.MANAGER.value),
    ]
    # Demo engineer logins are convenience accounts for local dev only — they
    # carry hardcoded weak passwords and must never exist in production.
    if not settings.is_production:
        seeds += [
            ("balaji", "balaji123", "Balaji", "Kumar", UserRole.ENGINEER.value),
            ("ranjith", "ranjith123", "Ranjith", "Singh", UserRole.ENGINEER.value),
        ]

    # In production, never create an account on a known-weak password. This
    # only triggers on a fresh (empty) prod DB; it forces strong SEED_* values.
    if settings.is_production:
        weak = [u for u, p, *_ in seeds if p in _WEAK_SEED_PASSWORDS]
        if weak:
            raise RuntimeError(
                "Refusing to seed production accounts with weak/default "
                f"passwords: {weak}. Set strong SEED_OWNER_PASSWORD / "
                "SEED_MANAGER_PASSWORD in the environment."
            )

    for username, password, first_name, last_name, role in seeds:
        full_name = f"{first_name} {last_name}".strip() or username
        u = User(
            username=username,
            password_hash=hash_password(password),
            name=full_name,
            first_name=first_name or None,
            last_name=last_name or None,
            role=role,
            active=True,
        )
        db.add(u)
    db.commit()
    logger.info("Seeded %d initial users (owner, manager, %d engineers)",
                len(seeds), len([s for s in seeds if s[4] == UserRole.ENGINEER.value]))
