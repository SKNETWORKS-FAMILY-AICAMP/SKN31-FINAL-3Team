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


def merge_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """쓸 수 있는 행만 남기고, 같은 견적은 하나로 합친다.

    ⚠️ 한 문장 안에 같은 견적이 두 번 들어가면 Postgres가 거부한다
    (ON CONFLICT는 한 행을 두 번 못 고친다). 폴링과 이메일 처리가 겹치면
    실제로 그런 입력이 만들어진다.

    합치는 규칙은 ON CONFLICT와 같아야 한다 - 더 이른 시각을 남기고, source는
    email이 이긴다. 두 곳이 어긋나면 한 문장에 들어온 경우와 두 번에 나눠 들어온
    경우의 결과가 달라진다.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("quotation_name") or "").strip()
        rfq = str(row.get("rfq_name") or "").strip()
        source = str(row.get("source") or "")
        moment = parse_erp_datetime(row.get("submitted_at"))
        if not name or not rfq or source not in SOURCES or moment is None:
            continue
        candidate = {
            "quotation_name": name,
            "rfq_name": rfq,
            "supplier_id": row.get("supplier_id") or None,
            "supplier_name": row.get("supplier_name") or None,
            "source": source,
            "submitted_at": moment,
            "communication_name": row.get("communication_name") or None,
        }
        previous = merged.get(name)
        if previous is None:
            merged[name] = candidate
            continue
        previous["submitted_at"] = min(previous["submitted_at"], moment)
        if source == "email":
            previous["source"] = "email"
            previous["communication_name"] = (
                candidate["communication_name"] or previous["communication_name"]
            )
        previous["supplier_id"] = previous["supplier_id"] or candidate["supplier_id"]
        previous["supplier_name"] = (
            previous["supplier_name"] or candidate["supplier_name"]
        )
    return list(merged.values())


def record_submissions(rows: list[Any]) -> int:
    """여러 건의 제출 시각을 **연결 하나, 문장 하나**로 기록한다.

    ⚠️ 이 프로젝트에는 커넥션 풀이 없다. get_connection은 부를 때마다 새
    Postgres 연결을 연다. 이 함수를 부르는 곳이 10초마다 도는 폴러 안이라,
    건당 한 번씩 부르면 그만큼 연결이 새로 열린다 - 실제로 그것 때문에 화면이
    느려졌다. 그래서 건수와 무관하게 왕복 한 번으로 끝낸다.

    규칙은 한 건일 때와 같다.
      - 이미 더 이른 시각이 기록돼 있으면 그 값을 유지한다(LEAST).
      - source는 email이 이긴다. 이메일로 들어온 건은 SQ 생성 시각보다 메일
        시각이 항상 진실에 가깝다.

    쓸 수 없는 행을 버리고 같은 견적을 합치는 일은 merge_rows가 한다.
    """
    values = merge_rows(rows)
    if not values:
        return 0

    with get_connection() as connection:
        written = connection.execute(
            """
            INSERT INTO procurement.quotation_submission (
                quotation_name, rfq_name, supplier_id, supplier_name,
                source, submitted_at, communication_name
            )
            SELECT * FROM UNNEST(
                %(quotation_names)s::text[],
                %(rfq_names)s::text[],
                %(supplier_ids)s::text[],
                %(supplier_names)s::text[],
                %(sources)s::text[],
                %(submitted_ats)s::timestamptz[],
                %(communication_names)s::text[]
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
            RETURNING quotation_name, submitted_at
            """,
            {
                "quotation_names": [row["quotation_name"] for row in values],
                "rfq_names": [row["rfq_name"] for row in values],
                "supplier_ids": [row["supplier_id"] for row in values],
                "supplier_names": [row["supplier_name"] for row in values],
                "sources": [row["source"] for row in values],
                "submitted_ats": [row["submitted_at"] for row in values],
                "communication_names": [row["communication_name"] for row in values],
            },
        ).fetchall()
    # ⚠️ dict_row 연결이다. row[0]은 KeyError(0)이 된다.
    return len([row for row in written if "submitted_at" in row])


def record_submission(
    *,
    quotation_name: str,
    rfq_name: str,
    source: str,
    submitted_at: Any,
    supplier_id: str | None = None,
    supplier_name: str | None = None,
    communication_name: str | None = None,
) -> int:
    """한 건짜리 편의 함수. 기록한 건수를 돌려준다."""
    return record_submissions([{
        "quotation_name": quotation_name,
        "rfq_name": rfq_name,
        "source": source,
        "submitted_at": submitted_at,
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
        "communication_name": communication_name,
    }])


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
    "merge_rows",
    "record_submission",
    "record_submissions",
    "submitted_at_by_quotation",
]
