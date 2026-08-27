"""PostgreSQL persistence for users, sessions, and refresh tokens."""

from datetime import datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .config import require_database_url


class InvalidRefreshTokenError(ValueError):
    pass


class ReusedRefreshTokenError(InvalidRefreshTokenError):
    pass


def _connect():
    return psycopg.connect(require_database_url(), row_factory=dict_row)


def create_login_session(
    *,
    user_profile: dict[str, Any],
    erp_sid_ciphertext: str,
    refresh_token_hash: str,
    expires_at: datetime,
    client_ip: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    user_id = str(uuid4())
    session_id = str(uuid4())
    refresh_token_id = str(uuid4())
    erp_user_id = user_profile["id"]

    with _connect() as connection:
        user = connection.execute(
            """
            INSERT INTO auth.users (
                id, erp_user_id, email, username, full_name, user_type,
                last_login_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (erp_user_id) DO UPDATE SET
                email = EXCLUDED.email,
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                user_type = EXCLUDED.user_type,
                is_active = TRUE,
                updated_at = now(),
                last_login_at = now()
            RETURNING id, erp_user_id, email, username, full_name, user_type
            """,
            (
                user_id,
                erp_user_id,
                user_profile.get("email") or None,
                user_profile.get("username") or erp_user_id,
                user_profile.get("full_name") or erp_user_id,
                user_profile.get("user_type") or "System User",
            ),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO auth.sessions (
                id, user_id, erp_sid_ciphertext, client_ip, user_agent,
                expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                user["id"],
                erp_sid_ciphertext,
                client_ip,
                user_agent,
                expires_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO auth.refresh_tokens (
                id, session_id, token_hash, expires_at
            )
            VALUES (%s, %s, %s, %s)
            """,
            (refresh_token_id, session_id, refresh_token_hash, expires_at),
        )

    return {"session_id": session_id, "user": dict(user)}


def rotate_refresh_token(
    *,
    current_token_hash: str,
    new_token_hash: str,
) -> dict[str, Any]:
    with _connect() as connection:
        token = connection.execute(
            """
            SELECT
                rt.id AS refresh_token_id,
                rt.session_id,
                rt.used_at AS refresh_used_at,
                rt.revoked_at AS refresh_revoked_at,
                rt.expires_at AS refresh_expires_at,
                s.revoked_at AS session_revoked_at,
                s.expires_at AS session_expires_at,
                u.id AS user_id,
                u.erp_user_id,
                u.email,
                u.username,
                u.full_name,
                u.user_type,
                u.is_active
            FROM auth.refresh_tokens rt
            JOIN auth.sessions s ON s.id = rt.session_id
            JOIN auth.users u ON u.id = s.user_id
            WHERE rt.token_hash = %s
            FOR UPDATE OF rt
            """,
            (current_token_hash,),
        ).fetchone()

        if token is None:
            raise InvalidRefreshTokenError("Refresh token was not found.")
        if token["refresh_used_at"] is not None:
            raise ReusedRefreshTokenError("Refresh token was already used.")
        if token["refresh_revoked_at"] is not None:
            raise InvalidRefreshTokenError("Refresh token was revoked.")
        if token["session_revoked_at"] is not None:
            raise InvalidRefreshTokenError("Session was revoked.")
        if not token["is_active"]:
            raise InvalidRefreshTokenError("User is inactive.")

        now = datetime.now(token["session_expires_at"].tzinfo)
        if token["refresh_expires_at"] <= now or token["session_expires_at"] <= now:
            raise InvalidRefreshTokenError("Session has expired.")

        connection.execute(
            "UPDATE auth.refresh_tokens SET used_at = now() WHERE id = %s",
            (token["refresh_token_id"],),
        )
        connection.execute(
            """
            INSERT INTO auth.refresh_tokens (
                id, session_id, token_hash, parent_token_id, expires_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                token["session_id"],
                new_token_hash,
                token["refresh_token_id"],
                token["session_expires_at"],
            ),
        )
        connection.execute(
            "UPDATE auth.sessions SET last_seen_at = now() WHERE id = %s",
            (token["session_id"],),
        )

    return {
        "session_id": str(token["session_id"]),
        "user": {
            "id": token["user_id"],
            "erp_user_id": token["erp_user_id"],
            "email": token["email"],
            "username": token["username"],
            "full_name": token["full_name"],
            "user_type": token["user_type"],
        },
    }


def get_active_session_user(session_id: str, user_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT
                s.id AS session_id,
                u.id,
                u.erp_user_id,
                u.email,
                u.username,
                u.full_name,
                u.user_type
            FROM auth.sessions s
            JOIN auth.users u ON u.id = s.user_id
            WHERE s.id = %s
              AND u.id = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND u.is_active = TRUE
            """,
            (session_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def revoke_session(session_id: str, reason: str = "logout") -> None:
    with _connect() as connection:
        connection.execute(
            """
            UPDATE auth.sessions
            SET revoked_at = COALESCE(revoked_at, now()), revoke_reason = %s
            WHERE id = %s
            """,
            (reason, session_id),
        )
        connection.execute(
            """
            UPDATE auth.refresh_tokens
            SET revoked_at = COALESCE(revoked_at, now())
            WHERE session_id = %s
            """,
            (session_id,),
        )
