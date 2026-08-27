"""Apply numbered SQL migrations to the configured PostgreSQL database."""

from pathlib import Path

import psycopg

from .config import require_database_url


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def apply_migrations() -> list[str]:
    applied: list[str] = []
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    with psycopg.connect(require_database_url()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        for migration_file in migration_files:
            version = migration_file.name
            already_applied = connection.execute(
                "SELECT 1 FROM public.schema_migrations WHERE version = %s",
                (version,),
            ).fetchone()
            if already_applied:
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
    if applied:
        print("Applied migrations:")
        for version in applied:
            print(f"  - {version}")
    else:
        print("Database schema is already up to date.")


if __name__ == "__main__":
    main()
