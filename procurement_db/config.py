"""Configuration for the shared procurement PostgreSQL connection."""

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class ProcurementDatabaseConfigurationError(RuntimeError):
    """Raised when the operational database connection is not configured."""


def require_database_url() -> str:
    """Return the operational DB URL without ever logging its secret.

    Team development uses NEXTERP_DATABASE_URL. DATABASE_URL remains a fallback
    for a deployed backend that intentionally uses one service connection for
    auth and procurement.
    """

    database_url = (
        os.environ.get("NEXTERP_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not database_url:
        raise ProcurementDatabaseConfigurationError(
            "NEXTERP_DATABASE_URL is required. Copy .env.example to .env and "
            "set the team PostgreSQL connection URL."
        )
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ProcurementDatabaseConfigurationError(
            "NEXTERP_DATABASE_URL must be a PostgreSQL connection URL."
        )
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)
