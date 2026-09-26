"""견적서 자동 읽기(파싱/추출) 실패 기록.

실패한 첨부는 Supplier Quotation이 만들어지지 않아 화면에서 '미회신'으로만
보이므로, 여기 남겨 협력사 선정 화면에서 원본 파일 확인을 안내한다.
"""

from __future__ import annotations

from typing import Any

from procurement_db import get_connection

FAILURE_KINDS = frozenset({"parse", "arithmetic", "extraction"})


def record_failure(
    *,
    rfq_name: str,
    failure_kind: str,
    supplier_id: str | None = None,
    supplier_name: str | None = None,
    source_filename: str | None = None,
    file_id: str | None = None,
    communication_name: str | None = None,
    error: str | None = None,
) -> None:
    """같은 메일/첨부의 실패는 한 줄로 유지하고 최신 사유로 덮어쓴다."""
    if failure_kind not in FAILURE_KINDS or not str(rfq_name or "").strip():
        return
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO procurement.quotation_intake_failure (
                rfq_name, supplier_id, supplier_name, source_filename, file_id,
                communication_name, failure_kind, error
            ) VALUES (
                %(rfq_name)s, %(supplier_id)s, %(supplier_name)s, %(source_filename)s,
                %(file_id)s, %(communication_name)s, %(failure_kind)s, %(error)s
            )
            ON CONFLICT (rfq_name, (COALESCE(communication_name, '')), (COALESCE(file_id, '')))
            DO UPDATE SET failure_kind = EXCLUDED.failure_kind,
                          error = EXCLUDED.error,
                          supplier_id = COALESCE(EXCLUDED.supplier_id, quotation_intake_failure.supplier_id),
                          supplier_name = COALESCE(EXCLUDED.supplier_name, quotation_intake_failure.supplier_name),
                          source_filename = COALESCE(EXCLUDED.source_filename, quotation_intake_failure.source_filename),
                          updated_at = now()
            """,
            {
                "rfq_name": str(rfq_name).strip(),
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "source_filename": source_filename,
                "file_id": file_id,
                "communication_name": communication_name,
                "failure_kind": failure_kind,
                "error": (error or "")[:500] or None,
            },
        )


def list_failures(rfq_names: list[str]) -> list[dict[str, Any]]:
    names = sorted({str(name).strip() for name in rfq_names if str(name).strip()})
    if not names:
        return []
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT rfq_name, supplier_id, supplier_name, source_filename, file_id,
                   communication_name, failure_kind, error, created_at, updated_at
            FROM procurement.quotation_intake_failure
            WHERE rfq_name = ANY(%(names)s)
            ORDER BY updated_at DESC
            """,
            {"names": names},
        ).fetchall()
    return [dict(row) for row in rows]
