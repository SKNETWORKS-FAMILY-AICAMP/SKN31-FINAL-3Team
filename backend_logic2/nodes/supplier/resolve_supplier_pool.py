"""
nodes/supplier/resolve_supplier_pool.py - 비딩 대상 품목별 공급사 풀(기존/신규탐색) 판정
실행: python -m backend_logic2.nodes.supplier.resolve_supplier_pool
"""

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from backend_logic2.integrations.erp_client import erp_get, erp_get_one
from backend_logic2.nodes.supplier.tools.case_logging import log_status_change

MIN_COMPETING_SUPPLIERS = 3
SUPPLIER_POOL_REFRESH_YEARS = 3
SUPPLIER_POOL_REFRESH_DAYS = 365 * SUPPLIER_POOL_REFRESH_YEARS


def _parse_erpnext_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        cleaned = str(value).split(".")[0]
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _has_purchase_history(item_code):
    orders = erp_get(
        "Purchase Order",
        filters=[["Purchase Order Item", "item_code", "=", item_code], ["docstatus", "=", 1]],
        fields=["name"],
        limit=1,
    )
    return bool(orders)


def _get_supplier_info(supplier_name):
    supplier_doc = erp_get_one("Supplier", supplier_name) or {}
    return {
        "name": supplier_name,
        "email": supplier_doc.get("email_id"),
        "phone": supplier_doc.get("mobile_no") or supplier_doc.get("phone"),
        "creation": supplier_doc.get("creation"),  # Supplier 자체 생성일 추가
        "source": "erpnext",
    }


def _resolve_one_item(item_code, case_id=None):
    """
    case_id(2026-08-31 MR 단위로 재설계): 예전엔 이 함수가 품목마다 직접
    케이스를 만들었는데, 실제 `--mr` 파이프라인을 까보니 MR 1건이 전체
    프로세스의 단위였음(사용자 확인: "MR 1건 = 품목 1건") - 그래서 이제
    케이스는 process_graph.py의 route_entrypoint_command가 MR당 1번만
    만들고, 여기는 그 case_id를 넘겨받아서 재사용만 함(자체 생성 안 함).
    """
    print(f"  [{item_code}] 공급사 풀 판정 시작")
    item = erp_get_one("Item", item_code) or {}
    supplier_rows = item.get("supplier_items") or []

    # 공급사 이름 추출 및 중복 제거
    supplier_names = [row.get("supplier") for row in supplier_rows if row.get("supplier")]
    unique_suppliers = sorted(list(set(supplier_names)))
    supplier_count = len(unique_suppliers)

    def _finish(needs_search, reason):
        log_status_change(
            case_id, "pool_resolved",
            reason=f"[{item_code}] 기존 공급사 {supplier_count}곳 - {reason}",
        )
        return item_code, needs_search, reason, unique_suppliers, supplier_rows

    # 1. 과거 거래이력 없음
    if not _has_purchase_history(item_code):
        reason = "과거 확정 구매이력 없음 -> 신규 공급사 탐색 필요"
        return _finish(True, reason)

    # 2. 기존 Supplier가 2곳 이하
    if supplier_count < MIN_COMPETING_SUPPLIERS:
        reason = f"기존 공급사 {supplier_count}곳 < 최소 경쟁기준 {MIN_COMPETING_SUPPLIERS}곳 -> 신규 공급사 탐색 필요"
        return _finish(True, reason)

    # 3. 각 공급사(Supplier) 본체 DocType의 creation 날짜 조회
    supplier_creation_dates = []
    if unique_suppliers:
        with ThreadPoolExecutor(max_workers=min(len(unique_suppliers), 8)) as executor:
            futures = [executor.submit(erp_get_one, "Supplier", s_name) for s_name in unique_suppliers]
            for future in as_completed(futures):
                s_doc = future.result() or {}
                parsed_dt = _parse_erpnext_datetime(s_doc.get("creation"))
                if parsed_dt:
                    supplier_creation_dates.append(parsed_dt)

    if not supplier_creation_dates:
        reason = f"기존 공급사 {supplier_count}곳이나 공급사 등록 시점을 확인할 수 없음 -> 안전하게 신규 공급사 탐색"
        return _finish(True, reason)

    most_recent = max(supplier_creation_dates)
    age_days = (datetime.now() - most_recent).days

    # 4. 마지막 공급사 등록(시장조사)이 3년 이상 지난 경우
    if age_days >= SUPPLIER_POOL_REFRESH_DAYS:
        reason = (
            f"기존 공급사 {supplier_count}곳이나 마지막 공급사 등록 시점 {most_recent.date()} ({age_days}일 전) "
            f"-> 기준 {SUPPLIER_POOL_REFRESH_YEARS}년 이상 경과, 신규 공급사 탐색 필요"
        )
        return _finish(True, reason)

    # 5. 기존 풀 사용
    reason = (
        f"기존 공급사 {supplier_count}곳 확보 + 마지막 공급사 등록 시점 {most_recent.date()} ({age_days}일 전) "
        f"< {SUPPLIER_POOL_REFRESH_YEARS}년 -> 기존 공급사 풀 사용"
    )
    return _finish(False, reason)


def resolve_supplier_pool(bidding_items: list, case_id: str = None) -> dict:
    """
    case_id: process_graph.py의 route_entrypoint_command가 만든 MR 케이스를
    그대로 받아서, 품목별 판정 결과를 그 케이스의 case_status_history에
    'pool_resolved'로 기록함. 단독 실행(__main__)이면 None으로 두면
    됨(로깅만 스킵).
    """
    if not bidding_items:
        return {
            "needs_search": False,
            "search_items": [],
            "existing_candidates": [],
            "item_decisions": {},
            "log_lines": [],
        }

    item_decisions = {}
    search_items = []
    log_lines = []
    all_supplier_names = set()

    with ThreadPoolExecutor(max_workers=min(len(bidding_items), 8) or 1) as executor:
        futures = [executor.submit(_resolve_one_item, item_code, case_id) for item_code in bidding_items]
        for future in as_completed(futures):
            item_code, needs_search, reason, unique_suppliers, _ = future.result()

            item_decisions[item_code] = {"needs_search": needs_search, "reason": reason}
            log_lines.append(f"  [{item_code}] {reason}")

            if needs_search:
                search_items.append(item_code)

            all_supplier_names.update(unique_suppliers)

    existing_candidates = []
    if all_supplier_names:
        with ThreadPoolExecutor(max_workers=min(len(all_supplier_names), 8) or 1) as executor:
            futures = [executor.submit(_get_supplier_info, s_name) for s_name in all_supplier_names]
            for future in as_completed(futures):
                existing_candidates.append(future.result())

    needs_search = len(search_items) > 0

    return {
        "needs_search": needs_search,
        "search_items": search_items,
        "existing_candidates": existing_candidates,
        "item_decisions": item_decisions,
        "log_lines": log_lines,
    }


if __name__ == "__main__":
    raw = input("품목코드 입력 (콤마로 구분): ").strip()
    item_codes = [code.strip() for code in raw.split(",") if code.strip()]

    result = resolve_supplier_pool(item_codes)

    print("\n[공급사풀 판정]")
    for line in result["log_lines"]:
        print(line)

    print(f"\n최종판정: {'신규탐색 필요' if result['needs_search'] else '기존 공급사만 사용'}")
    if result["search_items"]:
        print(f"신규탐색 대상 품목: {result['search_items']}")

    print(f"\n기존 공급사 후보 {len(result['existing_candidates'])}건:")
    for supplier in result["existing_candidates"]:
        print(
            f"  - {supplier['name']}"
            f" | 등록일: {supplier.get('creation') or '(없음)'}"
            f" | 이메일: {supplier.get('email') or '(없음)'}"
            f" | 전화: {supplier.get('phone') or '(없음)'}"
        )