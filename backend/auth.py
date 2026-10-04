"""
Authentication, password hashing and multi-tenant data scoping.

Design notes
------------
* Passwords are hashed with PBKDF2-HMAC-SHA256 from the standard library using a
  per-password random salt. No new dependency required.
* Sessions are opaque random tokens signed with HMAC and stored in a cookie.
  There is no JWT library dependency and no server-side session table.
* `get_current_school` is the single enforcement point for tenant isolation.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import School, User
from tenancy import bind_tenant

SESSION_COOKIE = "schoolpay_session"
SESSION_TTL_DAYS = 14

# Active plans and the student ceiling each one allows.
PLAN_LIMITS = {
    "trial": 50,
    "starter": 250,
    "pro": 1000,
    "enterprise": None,  # unlimited
}


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Return a string of the form pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>."""
    salt = secrets.token_bytes(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _signer_secret() -> bytes:
    """Signing key for session cookies.

    Must be set in the environment in any real deployment. There is deliberately
    NO usable default: falling back to a well-known value would let anyone
    forge a session cookie and become a platform administrator. Failing loudly
    here is the safe behaviour.
    """
    secret = os.getenv("SCHOOLPAY_SECRET")
    if not secret:
        raise RuntimeError(
            "SCHOOLPAY_SECRET is not set. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "and put it in your .env file before starting the server."
        )
    return secret.encode("utf-8")


def create_session_token(user_id: int) -> str:
    # NOTE: the expiry is an integer unix timestamp, NOT an ISO string. An ISO
    # string contains a "." for microseconds, which would collide with the "."
    # separator used below and corrupt the token.
    expires_at = int((datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).timestamp())
    payload = f"{user_id}.{expires_at}"
    signature = hmac.new(_signer_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def read_session_token(token: str) -> int | None:
    """Return the user id encoded in a valid, unexpired token, else None."""
    try:
        user_id_str, expires_at_str, signature = token.rsplit(".", 2)
        payload = f"{user_id_str}.{expires_at_str}"
        expected = hmac.new(_signer_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        if int(expires_at_str) < int(datetime.now(timezone.utc).timestamp()):
            return None
        return int(user_id_str)
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")

    user_id = read_session_token(session_token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired, please sign in again",
        )

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found or disabled")

    return user


def get_current_school(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> School:
    """Resolve the school whose data the caller is allowed to touch.

    Platform admins have no school of their own, so they get an explicit error
    rather than accidental blanket access.
    """
    if user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Platform admins must choose a school with ?school_id=<id>",
        )

    if not user.school_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account is not linked to a school")

    school = db.query(School).filter(School.id == user.school_id).first()
    if not school:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="School not found for this account")

    if school.subscription_status in {"cancelled", "expired"}:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Your {school.name} subscription is {school.subscription_status}. Please contact support.",
        )

    # Stamp the session so every ORM query in this request is automatically
    # filtered to this school (see tenancy.py). This is the single point that
    # makes cross-tenant reads impossible.
    bind_tenant(db, school.id)

    return school


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Restrict a route to school administrators."""
    if not user.is_platform_admin and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an administrator",
        )
    return user


def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    """Restrict a route to the platform operator (you), not to a school admin.

    This is what guards the cross-tenant administration routes. A school admin
    calling one of these must get 403, never their own school's data by accident.
    """
    if not user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This area is restricted to the system administrator",
        )
    return user


# ---------------------------------------------------------------------------
# Scoping helper
# ---------------------------------------------------------------------------

def scoped(db: Session, model, school: School):
    """Return a query for `model` restricted to one school.

    Used everywhere instead of a bare `db.query(model)` so tenant isolation is a
    single, consistent, greppable idiom.
    """
    return db.query(model).filter(model.school_id == school.id)