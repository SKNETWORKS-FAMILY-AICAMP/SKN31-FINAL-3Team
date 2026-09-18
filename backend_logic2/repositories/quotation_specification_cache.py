"""PostgreSQL cache for completed quotation specification assessments."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def load_matching(
    rfq_name: str,
    fingerprints: dict[str, str],
    evaluation_source: str,
) -> dict[str, dict[str, Any]]:
    """Return only rows whose current quotation input hash still matches."""

    if not fingerprints:
        return {}
    quotation_ids = list(fingerprints)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT quotation_id, input_hash, assessment_json
            FROM procurement.quotation_specification_cache
            WHERE rfq_name = %s
              AND evaluation_source = %s
              AND quotation_id = ANY(%s)
            """,
            (rfq_name, evaluation_source, quotation_ids),
        ).fetchall()
    return {
        str(row["quotation_id"]): dict(row["assessment_json"])
        for row in rows
        if fingerprints.get(str(row["quotation_id"])) == row["input_hash"]
    }


def save_assessments(
    rfq_name: str,
    fingerprints: dict[str, str],
    evaluation_source: str,
    assessments: dict[str, dict[str, Any]],
) -> None:
    """Persist successful assessments; identical inputs update in place."""

    rows = [
        (
            rfq_name,
            quotation_id,
            fingerprints[quotation_id],
            evaluation_source,
            Jsonb(assessment),
        )
        for quotation_id, assessment in assessments.items()
        if quotation_id in fingerprints
    ]
    if not rows:
        return
    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO procurement.quotation_specification_cache
                (rfq_name, quotation_id, input_hash, evaluation_source,
                 assessment_json)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (rfq_name, quotation_id, input_hash, evaluation_source)
            DO UPDATE SET assessment_json = EXCLUDED.assessment_json,
                          updated_at = now()
            """,
            rows,
        )
