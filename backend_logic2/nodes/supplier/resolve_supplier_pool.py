"""
nodes/resolve_supplier_pool.py — 비딩 대상 품목들의 기존(ERPNext) 공급사를
확인해서, 신규탐색이 필요한지 자동판정.

규칙:
  ① 기존 등록 공급사가 MIN_COMPETING_SUPPLIERS(3곳) 미만이면 신규탐색 필요
  ② 3곳 이상이어도, 가장 최근 공급사 등록일(ERPNext Item Supplier
     하위테이블의 creation 필드)이 SUPPLIER_POOL_REFRESH_DAYS(1년)
     이상 지났으면 시장가 갱신 차원에서 그래도 신규탐색
     ⚠️ 이건 "신규탐색을 실행한 날짜"가 아니라 "새 공급사가 실제로
     등록된 날짜"라, 예전에 탐색했는데 새로 등록된 게 없었던 경우는
     못 잡아내는 근사치임. 나중에 실제 탐색이력을 별도 DB에 남기게
     되면 더 정확한 방식으로 바꿔야 함.
  ③ 위 조건에 안 걸리면 기존 공급사만 사용

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/resolve_supplier_pool.py
"""


from datetime import datetime
from backend_logic2.integrations.erp_client import erp_get_one

MIN_COMPETING_SUPPLIERS = 3
SUPPLIER_POOL_REFRESH_DAYS = 365


def _parse_erpnext_datetime(value):
    """ERPNext의 creation 필드는 보통 'YYYY-MM-DD HH:MM:SS.ffffff' 형태의
    문자열로 옴. 안전하게 파싱, 실패하면 None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        cleaned = str(value).split(".")[0]
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def resolve_supplier_pool(bidding_items: list) -> dict:
    """
    비딩 대상 품목코드 리스트를 받아서, 기존 공급사 현황을 확인하고
    신규탐색 필요여부를 판정.

    반환: {
        "needs_search": bool,
        "existing_candidates": [{"name","email","phone","source"}, ...],
        "log_lines": [...],  # 판정 근거, 호출부에서 원하는 대로 출력 가능
    }
    """
    existing_candidates: dict[str, dict] = {}
    needs_search = False
    log_lines = []

    for item_code in bidding_items:
        item = erp_get_one("Item", item_code) or {}
        supplier_rows = item.get("supplier_items", [])
        count = len(supplier_rows)

        for row in supplier_rows:
            name = row.get("supplier") or row.get("name")
            if not name:
                continue
            supplier_doc = erp_get_one("Supplier", name) or {}
            existing_candidates[name] = {
                "name": name,
                "email": supplier_doc.get("email_id"),
                "phone": supplier_doc.get("mobile_no") or supplier_doc.get("phone"),
                "source": "erpnext",
            }

        if count < MIN_COMPETING_SUPPLIERS:
            needs_search = True
            log_lines.append(
                f"  [{item_code}] 기존 공급사 {count}곳 < 최소기준 {MIN_COMPETING_SUPPLIERS}곳 -> 신규탐색 필요"
            )
            continue

        creation_dates = [_parse_erpnext_datetime(row.get("creation")) for row in supplier_rows]
        creation_dates = [d for d in creation_dates if d is not None]

        if not creation_dates:
            needs_search = True
            log_lines.append(f"  [{item_code}] 공급사 {count}곳이나 등록일 정보를 못 읽음 -> 안전하게 신규탐색")
            continue

        most_recent = max(creation_dates)
        age_days = (datetime.now() - most_recent).days

        if age_days >= SUPPLIER_POOL_REFRESH_DAYS:
            needs_search = True
            log_lines.append(
                f"  [{item_code}] 기존 공급사 {count}곳이나, 최근 등록일({most_recent.date()})이 "
                f"{age_days}일 전(기준 {SUPPLIER_POOL_REFRESH_DAYS}일) -> 시장가 갱신 위해 신규탐색"
            )
        else:
            log_lines.append(
                f"  [{item_code}] 기존 공급사 {count}곳 충분 + 최근 등록일({most_recent.date()}, "
                f"{age_days}일 전)도 최신 -> 기존만 사용"
            )

    return {
        "needs_search": needs_search,
        "existing_candidates": list(existing_candidates.values()),
        "log_lines": log_lines,
    }


if __name__ == "__main__":
    raw = input("품목코드 입력 (콤마로 구분): ").strip()
    item_codes = [c.strip() for c in raw.split(",") if c.strip()]

    result = resolve_supplier_pool(item_codes)

    print(f"\n[공급사풀 판정]")
    for line in result["log_lines"]:
        print(line)
    print(f"  -> 최종판정: {'신규탐색 필요' if result['needs_search'] else '기존 공급사만 사용'}")
    print(f"\n기존 공급사 후보 {len(result['existing_candidates'])}건: {result['existing_candidates']}")