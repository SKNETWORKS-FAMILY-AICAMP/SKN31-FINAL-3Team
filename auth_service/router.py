"""ERPNext-backed login with local PostgreSQL session management."""

import os
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
import psycopg
import requests

from .config import AuthConfigurationError, get_auth_settings
from .dependencies import CurrentUser
from .repository import (
    InvalidRefreshTokenError,
    ReusedRefreshTokenError,
    create_login_session,
    revoke_session,
    rotate_refresh_token,
)
from .security import (
    create_access_token,
    encrypt_erp_sid,
    generate_refresh_token,
    hash_refresh_token,
    utc_now,
)


router = APIRouter(prefix="/api", tags=["Authentication"])
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    # ERPNext supports IDs such as Administrator as well as email addresses.
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


def _erp_base_url() -> str:
    return os.environ.get(
        "ERPNEXT_BASE_URL",
        "http://13.209.103.102:8080",
    ).rstrip("/")


def _get_erp_user_profile(erp_sid: str, user_id: str) -> dict[str, str]:
    fallback = {
        "id": user_id,
        "email": user_id if "@" in user_id else "",
        "username": user_id,
        "full_name": user_id,
        "user_type": "System User",
    }

    try:
        response = requests.get(
            f"{_erp_base_url()}/api/resource/User/{quote(user_id, safe='')}",
            params={
                "fields": '["name","full_name","first_name","last_name",'
                '"email","username","user_type"]'
            },
            cookies={"sid": erp_sid},
            timeout=10,
        )
        if response.status_code != 200:
            return fallback

        user = response.json().get("data") or {}
        composed_name = " ".join(
            part.strip()
            for part in (user.get("first_name"), user.get("last_name"))
            if isinstance(part, str) and part.strip()
        )
        return {
            "id": user.get("name") or user_id,
            "email": user.get("email") or fallback["email"],
            "username": user.get("username") or user.get("name") or user_id,
            "full_name": (
                user.get("full_name")
                or composed_name
                or user.get("name")
                or user_id
            ),
            "user_type": user.get("user_type") or fallback["user_type"],
        }
    except (requests.RequestException, ValueError, TypeError):
        # Profile lookup failure should not invalidate a successful ERP login.
        return fallback


def _serialize_user(user: dict[str, Any]) -> dict[str, Any]:
    erp_user_id = user["erp_user_id"]
    email = user.get("email") or ""
    username = user.get("username") or erp_user_id
    full_name = user.get("full_name") or username
    user_type = user.get("user_type") or "System User"
    return {
        "id": erp_user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "email": email,
        "username": username,
        "full_name": full_name,
        "user_type": user_type,
        "app_metadata": {
            "provider": "erpnext",
            "providers": ["erpnext"],
        },
        "user_metadata": {
            "email": email,
            "erp_user_id": erp_user_id,
            "username": username,
            "full_name": full_name,
            "user_type": user_type,
        },
    }


@router.post("/login")
def login_to_erp(login_request: LoginRequest, request: Request):
    try:
        response = requests.post(
            f"{_erp_base_url()}/api/method/login",
            data={"usr": login_request.email, "pwd": login_request.password},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ERP 서버 연결 오류: {exc}",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="아이디나 비밀번호가 올바르지 않습니다.",
        )

    erp_sid = response.cookies.get("sid")
    if not erp_sid:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ERP 로그인에 성공했지만 sid 쿠키를 받지 못했습니다.",
        )

    try:
        settings = get_auth_settings()
        profile = _get_erp_user_profile(erp_sid, login_request.email)
        refresh_token = generate_refresh_token()
        session = create_login_session(
            user_profile=profile,
            erp_sid_ciphertext=encrypt_erp_sid(erp_sid),
            refresh_token_hash=hash_refresh_token(refresh_token),
            expires_at=utc_now()
            + timedelta(days=settings.refresh_token_expire_days),
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        access_token, expires_in = create_access_token(
            session["user"],
            session["session_id"],
        )
    except (AuthConfigurationError, psycopg.Error) as exc:
        logger.exception("Failed to persist ERP login session")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="인증 저장소를 준비하지 못했습니다.",
        ) from exc

    return {
        "success": True,
        "message": "로그인 성공",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": _serialize_user(session["user"]),
    }


@router.post("/refresh")
def refresh_session(refresh_request: RefreshRequest):
    new_refresh_token = generate_refresh_token()
    try:
        session = rotate_refresh_token(
            current_token_hash=hash_refresh_token(refresh_request.refresh_token),
            new_token_hash=hash_refresh_token(new_refresh_token),
        )
        access_token, expires_in = create_access_token(
            session["user"],
            session["session_id"],
        )
    except ReusedRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이미 사용된 Refresh Token입니다.",
        ) from exc
    except InvalidRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 Refresh Token입니다.",
        ) from exc
    except (AuthConfigurationError, psycopg.Error) as exc:
        logger.exception("Refresh token rotation failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="인증 저장소에 연결할 수 없습니다.",
        ) from exc

    return {
        "success": True,
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(current_user: CurrentUser):
    try:
        revoke_session(str(current_user["session_id"]), reason="logout")
    except (AuthConfigurationError, psycopg.Error) as exc:
        logger.exception("Session revocation failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="로그아웃을 처리하지 못했습니다.",
        ) from exc
    return None


@router.get("/me")
def get_me(current_user: CurrentUser):
    return {"success": True, "user": _serialize_user(current_user)}
