"""Safe connectivity check: python -m procurement_db."""

from .connection import get_connection


def main() -> None:
    """Verify connection and schema permissions without printing credentials."""

    with get_connection(autocommit=True) as connection:
        identity = connection.execute(
            "SELECT current_database() AS database, current_user AS username"
        ).fetchone()
        tables = connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'procurement'
            ORDER BY table_name
            """
        ).fetchall()
        privileges = connection.execute(
            """
            SELECT
                has_schema_privilege(current_user, 'procurement', 'USAGE') AS schema_usage,
                has_schema_privilege(current_user, 'procurement', 'CREATE') AS schema_create,
                has_table_privilege(
                    current_user,
                    'procurement.procurement_case',
                    'SELECT,INSERT,UPDATE,DELETE'
                ) AS case_dml
            """
        ).fetchone()

    print(f"Connected: database={identity['database']}, user={identity['username']}")
    print("Procurement tables:")
    for table in tables:
        print(f"  - procurement.{table['table_name']}")
    print(
        "Permissions: "
        f"usage={privileges['schema_usage']}, "
        f"create={privileges['schema_create']}, "
        f"dml={privileges['case_dml']}"
    )


if __name__ == "__main__":
    main()
