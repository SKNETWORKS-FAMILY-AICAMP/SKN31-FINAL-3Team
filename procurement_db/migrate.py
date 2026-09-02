"""Apply numbered migrations to the shared procurement PostgreSQL store.

Unlike ``auth_service.migrate``, this command uses NEXTERP_DATABASE_URL first.
The same ``public.schema_migrations`` ledger is shared so a migration is never
applied twice merely because two application modules can reach the database.
"""

from __future__ import annotations

from pathlib import Path

from .connection import get_connection


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
ADVISORY_LOCK_KEY = 310031005


def apply_migrations() -> list[str]:
    applied: list[str] = []
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    with get_connection() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        known = {
            row["version"]
            for row in connection.execute(
                "SELECT version FROM public.schema_migrations"
            ).fetchall()
        }
        for migration_file in migration_files:
            version = migration_file.name
            if version in known:
                continue
            connection.execute(migration_file.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO public.schema_migrations (version) VALUES (%s)",
                (version,),
            )
            applied.append(version)
    return applied


def main() -> None:
    applied = apply_migrations()
    if not applied:
        print("Procurement schema is already up to date.")
        return
    print("Applied procurement migrations:")
    for version in applied:
        print(f"  - {version}")


if __name__ == "__main__":
    main()
