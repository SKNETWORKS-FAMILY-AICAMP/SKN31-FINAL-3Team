"""FastAPI dependencies for authenticated endpoints."""

from typing import Annotated, Any
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
import psycopg

from .config import AuthConfigurationError
from .repository import get_active_session_user
from .security import decode_access_token


bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def require_authenticated_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(credentials.credentials)
        user = get_active_session_user(
            session_id=str(payload["session_id"]),
            user_id=str(payload["sub"]),
        )
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 Access Token입니다.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except (AuthConfigurationError, psycopg.Error) as exc:
        logger.exception("Authentication session lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="인증 저장소에 연결할 수 없습니다.",
        ) from exc

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="종료되었거나 만료된 세션입니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


CurrentUser = Annotated[dict[str, Any], Depends(require_authenticated_user)]
