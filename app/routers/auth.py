"""Authentication endpoints: login + me."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.user import User
from ..schemas.auth import LoginRequest, TokenResponse, UserOut
from ..services.auth import (
    get_current_user,
    issue_token,
    verify_password,
)

logger = logging.getLogger("sk-pos-care.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = (
        db.query(User)
        .filter(User.username == payload.username.strip().lower())
        .one_or_none()
    )
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        # Same response shape for both wrong-username and wrong-password,
        # so attackers can't probe which usernames exist.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token, expires_at = issue_token(user)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    logger.info("Login OK: %s (%s)", user.username, user.role)
    return TokenResponse(access_token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
