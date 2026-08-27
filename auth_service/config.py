"""Authentication settings loaded from environment variables."""

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class AuthConfigurationError(RuntimeError):
    """Raised when the authentication service is missing required settings."""


@dataclass(frozen=True)
class AuthSettings:
    database_url: str
    jwt_secret: str
    jwt_algorithm: str
    jwt_issuer: str
    jwt_audience: str
    access_token_expire_minutes: int
    refresh_token_expire_days: int
    session_encryption_key: str


@lru_cache(maxsize=1)
def get_auth_settings() -> AuthSettings:
    jwt_secret = os.environ.get("JWT_SECRET", "").strip()
    if len(jwt_secret) < 32:
        raise AuthConfigurationError(
            "JWT_SECRET must be configured with at least 32 characters."
        )

    return AuthSettings(
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        jwt_secret=jwt_secret,
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256").strip(),
        jwt_issuer=os.environ.get("JWT_ISSUER", "biddingflow-auth").strip(),
        jwt_audience=os.environ.get("JWT_AUDIENCE", "authenticated").strip(),
        access_token_expire_minutes=int(
            os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
        ),
        refresh_token_expire_days=int(
            os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "7")
        ),
        # A separate key is recommended. JWT_SECRET is a development fallback so
        # existing environments can be migrated without exposing the ERP sid.
        session_encryption_key=(
            os.environ.get("SESSION_ENCRYPTION_KEY", "").strip() or jwt_secret
        ),
    )


def require_database_url() -> str:
    database_url = get_auth_settings().database_url
    if not database_url:
        raise AuthConfigurationError(
            "DATABASE_URL is required for database-backed authentication."
        )
    return database_url
