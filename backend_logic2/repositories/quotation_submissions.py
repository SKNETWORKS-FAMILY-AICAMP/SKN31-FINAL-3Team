"""협력사가 견적을 "낸 시각" 기록.

마감이 지났는지 판정하는 기준은 협력사가 낸 시각이다. ERPNext에 Supplier
Quotation이 만들어진 시각이 아니다. 이메일 회신은 첨부를 읽어 등록하기까지
시간이 걸리고, 그 지연 때문에 마감 전에 낸 견적을 버리면 안 된다.

  portal : Supplier Quotation.creation
  email  : 회신 메일의 communication_date (없으면 creation)

⚠️ 기록한 제출 시각은 절대 뒤로 미루지 않는다. 같은 견적에 SQ 변경 웹훅이
여러 번 오는데 그때마다 SQ 생성 시각으로 덮어쓰면 메일 시각이 날아간다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from procurement_db import get_connection

SOURCES = frozenset({"portal", "email"})

# ERPNext가 돌려주는 날짜/시간 문자열에는 표준시 정보가 없다. 사이트가 한국
# 시간으로 설정돼 있어서 전부 KST로 읽는다(기존 독촉메일 판정도 같은 가정).
ERP_TIMEZONE = ZoneInfo("Asia/Seoul")

_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _aware(value: datetime) -> datetime:
    """표준시가 없는 값은 KST로 본다(ERPNext 사이트가 한국 시간이다)."""
    return value if value.tzinfo else value.replace(tzinfo=ERP_TIMEZONE)


def parse_erp_datetime(value: Any) -> datetime | None:
    """ERPNext 날짜/시간 문자열을 KST 기준 aware datetime으로 읽는다.

    마감 시각은 +09:00이 붙은 aware 값이므로, 여기서 naive를 돌려주면
    비교하는 순간 TypeError가 난다. 읽을 수 없으면 None - 호출부가 "모른다"로
    다루게 한다(모르는 값을 지금 시각으로 채우면 멀쩡한 견적이 늦은 것으로
    바뀔 수 있다).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value).strip()
    if not text:
        return None
    parsed = _read_any_format(text)
    # ⚠️ aware로 만드는 곳은 여기 한 군데다. 분기마다 붙이면 한 분기를 빼먹고도
    # 다른 분기 테스트가 통과한다.
    return _aware(parsed) if parsed is not None else None


def _read_any_format(text: str) -> datetime | None:
    # fromisoformat은 3.10에서 "…:30.1"처럼 소수 자리가 6자리가 아니면 거부한다.
    # 그래서 strptime 자리는 살아 있는 경로다.
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def record_submission(
    *,
    quotation_name: str,
    rfq_name: str,
    source: str,
    submitted_at: Any,
    supplier_id: str | None = None,
    supplier_name: str | None = None,
    communication_name: str | None = None,
) -> datetime | None:
    """제출 시각을 기록하고, 기록 후 남은 값을 돌려준다.

    이미 더 이른 시각이 기록돼 있으면 그 값을 유지한다(LEAST). source는
    email이 이긴다 - 이메일로 들어온 건은 SQ 생성 시각보다 메일 시각이 항상
    진실에 가깝다.
    """
    name = str(quotation_name or "").strip()
    rfq = str(rfq_name or "").strip()
    moment = parse_erp_datetime(submitted_at)
    if not name or not rfq or source not in SOURCES or moment is None:
        return None
    with get_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO procurement.quotation_submission (
                quotation_name, rfq_name, supplier_id, supplier_name,
                source, submitted_at, communication_name
            ) VALUES (
                %(quotation_name)s, %(rfq_name)s, %(supplier_id)s, %(supplier_name)s,
                %(source)s, %(submitted_at)s, %(communication_name)s
            )
            ON CONFLICT (quotation_name) DO UPDATE SET
                submitted_at = LEAST(
                    quotation_submission.submitted_at, EXCLUDED.submitted_at
                ),
                source = CASE
                    WHEN quotation_submission.source = 'email' THEN 'email'
                    ELSE EXCLUDED.source
                END,
                supplier_id = COALESCE(
                    EXCLUDED.supplier_id, quotation_submission.supplier_id
                ),
                supplier_name = COALESCE(
                    EXCLUDED.supplier_name, quotation_submission.supplier_name
                ),
                communication_name = COALESCE(
                    EXCLUDED.communication_name, quotation_submission.communication_name
                ),
                updated_at = now()
            RETURNING submitted_at
            """,
            {
                "quotation_name": name,
                "rfq_name": rfq,
                "supplier_id": supplier_id or None,
                "supplier_name": supplier_name or None,
                "source": source,
                "submitted_at": moment,
                "communication_name": communication_name or None,
            },
        ).fetchone()
    # ⚠️ dict_row 연결이다. row[0]은 KeyError(0)이 된다.
    if not row or "submitted_at" not in row:
        return None
    return row["submitted_at"]


def submitted_at_by_quotation(rfq_names: list[str]) -> dict[str, datetime]:
    """견적 문서명 -> 제출 시각. 기록이 없는 견적은 키 자체가 없다."""
    names = sorted({str(name).strip() for name in rfq_names if str(name).strip()})
    if not names:
        return {}
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT quotation_name, submitted_at
            FROM procurement.quotation_submission
            WHERE rfq_name = ANY(%(names)s)
            """,
            {"names": names},
        ).fetchall()
    return {
        str(row["quotation_name"]): _aware(row["submitted_at"])
        for row in rows
        if row.get("submitted_at") is not None
    }


def list_submissions(rfq_names: list[str]) -> list[dict[str, Any]]:
    """화면이 "언제 냈고 언제 처리됐는지"를 보여줄 수 있게 전부 돌려준다."""
    names = sorted({str(name).strip() for name in rfq_names if str(name).strip()})
    if not names:
        return []
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT quotation_name, rfq_name, supplier_id, supplier_name, source,
                   submitted_at, recorded_at, communication_name
            FROM procurement.quotation_submission
            WHERE rfq_name = ANY(%(names)s)
            ORDER BY submitted_at
            """,
            {"names": names},
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "ERP_TIMEZONE",
    "SOURCES",
    "list_submissions",
    "parse_erp_datetime",
    "record_submission",
    "submitted_at_by_quotation",
]
