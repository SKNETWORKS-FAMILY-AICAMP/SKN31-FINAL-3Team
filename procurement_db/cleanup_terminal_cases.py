"""Back up and remove terminal procurement cases from the shared DB.

The command deliberately targets only CANCELLED/REJECTED cases. FAILED cases
remain recoverable and COMPLETED cases remain available for audit/review.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from procurement_db import get_connection


RELATED_TABLES = (
    "ai_decision_log",
    "case_status_history",
    "human_task",
    "notification",
    "purchase_order_delivery",
    "workflow_status_history",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value


def cleanup_terminal_cases(backup_dir: Path, *, execute: bool) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"terminal_procurement_cases_{timestamp}.json"

    with get_connection() as connection:
        cases = connection.execute(
            """
            SELECT *
            FROM procurement.procurement_case
            WHERE status IN ('CANCELLED', 'REJECTED')
            ORDER BY created_at
            FOR UPDATE
            """
        ).fetchall()
        case_rows = [dict(row) for row in cases]
        case_ids = [str(row["case_id"]) for row in case_rows]
        mr_names = [str(row["mr_name"]) for row in case_rows if row.get("mr_name")]

        snapshot: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc),
            "target_statuses": ["CANCELLED", "REJECTED"],
            "procurement_case": case_rows,
            "related": {},
        }
        if case_ids:
            for table in RELATED_TABLES:
                rows = connection.execute(
                    f"SELECT * FROM procurement.{table} WHERE case_id = ANY(%s::uuid[])",
                    (case_ids,),
                ).fetchall()
                snapshot["related"][table] = [dict(row) for row in rows]
            events = connection.execute(
                """
                SELECT * FROM procurement.integration_event
                WHERE external_id = ANY(%s::text[])
                """,
                (mr_names,),
            ).fetchall() if mr_names else []
            snapshot["related"]["integration_event"] = [dict(row) for row in events]

        temporary_path = backup_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(_json_value(snapshot), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(backup_path)

        deleted: dict[str, int] = {table: 0 for table in RELATED_TABLES}
        deleted["integration_event"] = 0
        deleted["procurement_case"] = 0
        if execute and case_ids:
            # ai_decision_log uses SET NULL by design; an explicit cleanup is
            # required here because this command intentionally removes all
            # records belonging to the discarded demo cases.
            deleted["ai_decision_log"] = connection.execute(
                "DELETE FROM procurement.ai_decision_log WHERE case_id = ANY(%s::uuid[])",
                (case_ids,),
            ).rowcount
            if mr_names:
                deleted["integration_event"] = connection.execute(
                    "DELETE FROM procurement.integration_event WHERE external_id = ANY(%s::text[])",
                    (mr_names,),
                ).rowcount
            # The remaining case-related tables are ON DELETE CASCADE.
            deleted["procurement_case"] = connection.execute(
                """
                DELETE FROM procurement.procurement_case
                WHERE case_id = ANY(%s::uuid[])
                  AND status IN ('CANCELLED', 'REJECTED')
                """,
                (case_ids,),
            ).rowcount

    return {
        "execute": execute,
        "backup_path": str(backup_path.resolve()),
        "target_case_count": len(case_rows),
        "target_mr_names": mr_names,
        "deleted": deleted,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(cleanup_terminal_cases(args.backup_dir, execute=args.execute), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
