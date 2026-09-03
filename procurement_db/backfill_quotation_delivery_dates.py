"""Backfill proposed delivery dates missing from old workflow snapshots.

New quotation evaluations preserve ``expected_delivery_date`` directly. Cases
evaluated before that fix still contain the Supplier Quotation name, so this
idempotent utility can repair only the absent derived values without changing
workflow status, stage, tasks, or ERPNext documents.

Usage::

    python -m procurement_db.backfill_quotation_delivery_dates          # dry run
    python -m procurement_db.backfill_quotation_delivery_dates --apply  # update
"""

from __future__ import annotations

import argparse
from typing import Any

from psycopg.types.json import Jsonb

from backend_logic2.integrations.erp_client import erp_get_one
from procurement_db import get_connection


def _proposed_delivery_date(quotation_name: str) -> str | None:
    quotation = erp_get_one("Supplier Quotation", quotation_name) or {}
    for item in quotation.get("items") or []:
        value = (
            item.get("expected_delivery_date")
            or item.get("schedule_date")
            or item.get("delivery_date")
        )
        if value:
            return str(value)
    return None


def backfill(*, apply: bool = False) -> tuple[int, int]:
    inspected = 0
    updated = 0
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT case_id, mr_name, version, workflow_snapshot
            FROM procurement.procurement_case
            WHERE jsonb_typeof(workflow_snapshot->'values'->'quotation_ranking') = 'array'
              AND jsonb_array_length(workflow_snapshot->'values'->'quotation_ranking') > 0
            ORDER BY updated_at
            """
        ).fetchall()

        quotation_date_cache: dict[str, str | None] = {}
        for row in rows:
            snapshot: dict[str, Any] = dict(row.get("workflow_snapshot") or {})
            values: dict[str, Any] = dict(snapshot.get("values") or {})
            ranking = [dict(item) for item in values.get("quotation_ranking") or []]
            changed = False
            for quotation in ranking:
                if quotation.get("expected_delivery_date"):
                    continue
                quotation_name = str(quotation.get("name") or "").strip()
                if not quotation_name:
                    continue
                inspected += 1
                if quotation_name not in quotation_date_cache:
                    quotation_date_cache[quotation_name] = _proposed_delivery_date(quotation_name)
                expected_date = quotation_date_cache[quotation_name]
                if expected_date:
                    quotation["expected_delivery_date"] = expected_date
                    changed = True
                    print(f"[FOUND] {row['mr_name']} · {quotation_name} · {expected_date}")

            if not changed:
                continue
            updated += 1
            if apply:
                values["quotation_ranking"] = ranking
                snapshot["values"] = values
                result = connection.execute(
                    """
                    UPDATE procurement.procurement_case
                    SET workflow_snapshot = %(snapshot)s,
                        version = version + 1,
                        updated_at = now()
                    WHERE case_id = %(case_id)s
                      AND version = %(version)s
                    """,
                    {
                        "snapshot": Jsonb(snapshot),
                        "case_id": row["case_id"],
                        "version": row["version"],
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"동시 변경으로 보정하지 못했습니다: {row['mr_name']}"
                    )

    return inspected, updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="보정 결과를 PostgreSQL에 반영")
    args = parser.parse_args()
    inspected, updated = backfill(apply=args.apply)
    mode = "적용" if args.apply else "미리보기"
    print(f"[{mode} 완료] SQ {inspected}건 확인 · case {updated}건 보정 대상")


if __name__ == "__main__":
    main()
