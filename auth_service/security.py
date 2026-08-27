"""Token, hashing, and ERP session encryption helpers."""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet
import jwt

from .config import get_auth_settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_refresh_token() -> str:
    """Return an opaque, high-entropy token suitable for one-time rotation."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_fernet() -> Fernet:
    material = get_auth_settings().session_encryption_key.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def encrypt_erp_sid(erp_sid: str) -> str:
    return _get_fernet().encrypt(erp_sid.encode("utf-8")).decode("ascii")


def decrypt_erp_sid(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def create_access_token(user: dict[str, Any], session_id: str) -> tuple[str, int]:
    settings = get_auth_settings()
    now = utc_now()
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    erp_user_id = user["erp_user_id"]
    email = user.get("email") or ""
    username = user.get("username") or erp_user_id
    full_name = user.get("full_name") or username

    payload = {
        "iss": settings.jwt_issuer,
        "sub": str(user["id"]),
        "aud": settings.jwt_audience,
        "role": "authenticated",
        "email": email,
        "name": full_name,
        "session_id": str(session_id),
        "erp_user_id": erp_user_id,
        "username": username,
        "user_type": user.get("user_type") or "System User",
        "app_metadata": {
            "provider": "erpnext",
            "providers": ["erpnext"],
        },
        "user_metadata": {
            "email": email,
            "erp_user_id": erp_user_id,
            "username": username,
            "full_name": full_name,
            "user_type": user.get("user_type") or "System User",
        },
        "token_type": "access_token",
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return encoded, settings.access_token_expire_minutes * 60


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_auth_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        leeway=30,
        options={
            "require": ["exp", "iat", "sub", "session_id", "token_type"],
        },
    )
    if payload.get("token_type") != "access_token":
        raise jwt.InvalidTokenError("An access token is required.")
    return payload
