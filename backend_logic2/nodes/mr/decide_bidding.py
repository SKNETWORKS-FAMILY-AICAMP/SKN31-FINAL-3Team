"""
nodes/mr/decide_bidding.py - MR 품목별 비딩(경쟁견적) 필요 여부 룰 기반 판정
우선순위: 1. 신규거래 -> 2. 제조사 직거래 -> 3. 긴급발주 -> 4. 고액구매 -> 5. 구매패턴 분석
실행: python -m backend_logic2.nodes.mr.decide_bidding
"""

import statistics
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from backend_logic2.integrations.erp_client import erp_get, erp_get_one

# 비딩 정책 설정값
AMOUNT_THRESHOLD = 20_000_000
MIN_ORDERS_FOR_PATTERN = 3
IRREGULAR_CV_THRESHOLD = 0.5
CYCLE_OVERDUE_MULTIPLIER = 1.5
INACTIVE_MONTHS = 12
URGENT_LEAD_TIME_DAYS = 7


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _fetch_po_line(order, item_code):
    po_doc = erp_get_one("Purchase Order", order["name"])
    if not po_doc:
        return None
    for item in po_doc.get("items", []):
        if item.get("item_code") == item_code:
            return {
                "date": order["transaction_date"],
                "rate": item.get("rate") or 0,
                "supplier": po_doc.get("supplier"),
                "purchase_order": po_doc.get("name") or order["name"],
            }
    return None


def _get_past_purchases(item_code):
    orders = erp_get(
        "Purchase Order",
        filters=[["Purchase Order Item", "item_code", "=", item_code], ["docstatus", "=", 1]],
        fields=["name", "transaction_date"],
    )
    if not orders:
        return []

    purchases = []
    with ThreadPoolExecutor(max_workers=min(len(orders), 8) or 1) as executor:
        futures = [executor.submit(_fetch_po_line, order, item_code) for order in orders]
        for future in as_completed(futures):
            res = future.result()
            if res:
                purchases.append(res)

    purchases.sort(key=lambda p: _parse_date(p["date"]))
    return purchases


def _analyze_purchase_pattern(purchases):
    empty_res = {"enough_history": False, "irregular": None, "cv": None, "average_interval_days": None}
    if len(purchases) < MIN_ORDERS_FOR_PATTERN:
        return empty_res

    dates = [_parse_date(p["date"]) for p in purchases]
    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not intervals:
        return empty_res

    avg_interval = statistics.mean(intervals)
    if avg_interval <= 0:
        return empty_res

    cv = statistics.stdev(intervals) / avg_interval if len(intervals) > 1 else 0
    return {
        "enough_history": True,
        "irregular": cv >= IRREGULAR_CV_THRESHOLD,
        "cv": cv,
        "average_interval_days": avg_interval,
    }


def _days_since_last_purchase(purchases):
    if not purchases:
        return None
    last_date = _parse_date(purchases[-1]["date"])
    return (date.today() - last_date).days if last_date else None


def _normalize_name(value):
    return str(value).strip().casefold() if value else ""


def _find_brand_supplier(item):
    brand = item.get("brand")
    if not brand:
        return None

    normalized_brand = _normalize_name(brand)
    for row in item.get("supplier_items") or []:
        supplier_name = row.get("supplier")
        if supplier_name and _normalize_name(supplier_name) == normalized_brand:
            return supplier_name
    return None


def _direct_purchase_fields(purchases, *, supplier=None):
    """Return the traceable source needed to create a non-bidding PO.

    A `needs_bidding=False` decision is actionable only when a submitted past
    PO supplies both the vendor and its last confirmed unit price.  Keeping
    these values in the graph state lets the PO approval screen explain which
    transaction is being reused instead of silently guessing later.
    """

    matching = [
        purchase
        for purchase in purchases
        if not supplier or _normalize_name(purchase.get("supplier")) == _normalize_name(supplier)
    ]
    if not matching:
        return {}
    latest = matching[-1]
    direct_supplier = supplier or latest.get("supplier")
    rate = latest.get("rate") or 0
    if not direct_supplier or not rate:
        return {}
    return {
        "direct_supplier": direct_supplier,
        "last_rate": rate,
        "reference_po": latest.get("purchase_order"),
        "reference_date": str(latest.get("date") or ""),
    }


def _decide_one_item(line):
    item_code, qty = line["item_code"], line["qty"]
    print(f"  [{item_code}] 판정 시작 (요청수량: {qty})")

    item = erp_get_one("Item", item_code) or {}
    purchases = _get_past_purchases(item_code)
    print(f"    -> 과거 확정구매 이력: {len(purchases)}건")

    schedule_date = _parse_date(line.get("schedule_date"))
    remaining_days = (schedule_date - date.today()).days if schedule_date else None

    # 1. 신규거래. 단, 긴급인데 최근 확정 거래가 없다면 새 공급사를
    # 탐색할 시간도 없으므로 후속 command가 표준 사유로 MR을 중단한다.
    if not purchases:
        if remaining_days is not None and remaining_days <= URGENT_LEAD_TIME_DAYS:
            reason = (
                f"긴급발주이나 최근 거래 협력사 없음 "
                f"(납기까지 {remaining_days}일, 긴급 기준 {URGENT_LEAD_TIME_DAYS}일 이하)"
            )
            print(f"    -> 판정: 비딩 불필요(구매 중단) | {reason}")
            return item_code, {"needs_bidding": False, "reasons": [reason]}
        reason = "신규거래(과거 구매이력 없음)"
        print(f"    -> 판정: 비딩 필요 | {reason}")
        return item_code, {"needs_bidding": True, "reasons": [reason]}

    # 2. 제조사 직거래 (Brand == Supplier)
    brand_supplier = _find_brand_supplier(item)
    if brand_supplier:
        direct_fields = _direct_purchase_fields(purchases, supplier=brand_supplier)
        if not direct_fields:
            reason = "제조사 직거래 공급사의 확정 구매단가가 없어 경쟁견적 필요"
            print(f"    -> 판정: 비딩 필요 | {reason}")
            return item_code, {"needs_bidding": True, "reasons": [reason]}
        reason = f"제조사 직거래 (Brand '{item.get('brand')}' = Supplier '{brand_supplier}')"
        print(f"    -> 판정: 비딩 불필요 | {reason}")
        return item_code, {
            "needs_bidding": False,
            "reasons": [reason],
            **direct_fields,
        }

    # 3. 긴급발주
    if remaining_days is not None:
        print(f"    -> 납기일까지 남은 기간: {remaining_days}일")
        if remaining_days <= URGENT_LEAD_TIME_DAYS:
            direct_fields = _direct_purchase_fields(purchases)
            if not direct_fields:
                reason = "긴급발주이나 재사용할 최근 협력사·확정단가가 없어 구매 중단 필요"
                print(f"    -> 판정: 비딩 불필요(구매 중단) | {reason}")
                return item_code, {"needs_bidding": False, "reasons": [reason]}
            reason = f"긴급발주 (납기까지 {remaining_days}일, 긴급 기준 {URGENT_LEAD_TIME_DAYS}일 이하)"
            print(f"    -> 판정: 비딩 불필요 | {reason}")
            return item_code, {
                "needs_bidding": False,
                "reasons": [reason],
                **direct_fields,
            }
    else:
        print("    -> 납기일 정보 없음: 긴급발주 판단 생략")

    # 4. 고액구매
    amount = qty * (purchases[-1].get("rate") or 0)
    print(f"    -> 최근단가 기준 예상금액: {amount:,.0f}원")
    if amount >= AMOUNT_THRESHOLD:
        reason = f"고액구매 ({amount:,.0f}원 ≥ {AMOUNT_THRESHOLD:,}원)"
        print(f"    -> 판정: 비딩 필요 | {reason}")
        return item_code, {"needs_bidding": True, "reasons": [reason]}

    # 5. 구매패턴 분석
    pattern = _analyze_purchase_pattern(purchases)
    days_since_last = _days_since_last_purchase(purchases)

    if pattern["enough_history"]:
        cv, avg_interval = pattern["cv"], pattern["average_interval_days"]
        print(f"    -> 평균 구매주기: {avg_interval:.1f}일 | CV: {cv:.3f}")

        if pattern["irregular"]:
            reason = f"구매주기 불규칙 (CV {cv:.2f} ≥ {IRREGULAR_CV_THRESHOLD})"
            print(f"    -> 판정: 비딩 필요 | {reason}")
            return item_code, {"needs_bidding": True, "reasons": [reason]}

        allowed_days = avg_interval * CYCLE_OVERDUE_MULTIPLIER
        print(f"    -> 구매주기 규칙적 | 마지막 구매 후 {days_since_last}일 경과 (허용: {allowed_days:.1f}일)")

        if days_since_last and days_since_last > allowed_days:
            reason = f"정상 구매주기 초과 (마지막 구매 후 {days_since_last}일, 허용 {allowed_days:.0f}일)"
            print(f"    -> 판정: 비딩 필요 | {reason}")
            return item_code, {"needs_bidding": True, "reasons": [reason]}

        reason = "기존 저액거래 + 구매주기 규칙적 + 정상 구매시점"
        print(f"    -> 판정: 비딩 불필요 | {reason}")
        return item_code, {
            "needs_bidding": False,
            "reasons": [reason],
            **_direct_purchase_fields(purchases),
        }

    # 구매이력 부족 (N건 미만)
    print(f"    -> 구매패턴 판단 이력 부족 ({len(purchases)}건 < {MIN_ORDERS_FOR_PATTERN}건)")
    if days_since_last and days_since_last > (INACTIVE_MONTHS * 30.44):
        reason = f"장기 미거래 (마지막 거래 후 {INACTIVE_MONTHS}개월 이상)"
        print(f"    -> 판정: 비딩 필요 | {reason}")
        return item_code, {"needs_bidding": True, "reasons": [reason]}

    reason = f"구매이력은 부족하지만 마지막 거래가 {INACTIVE_MONTHS}개월 이내"
    print(f"    -> 판정: 비딩 불필요 | {reason}")
    return item_code, {
        "needs_bidding": False,
        "reasons": [reason],
        **_direct_purchase_fields(purchases),
    }


def decide_bidding(mr_name):
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return {}

    items = mr.get("items", [])
    print(f"\n[비딩판정] '{mr_name}' 품목 {len(items)}건, 병렬 판정 시작")

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(items), 8) or 1) as executor:
        futures = [executor.submit(_decide_one_item, line) for line in items]
        for future in as_completed(futures):
            item_code, info = future.result()
            results[item_code] = info

    bidding_count = sum(1 for info in results.values() if info["needs_bidding"])
    print(f"[비딩판정 완료] 총 {len(results)}개 품목 중 {bidding_count}개 비딩 필요\n")
    return results


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()
    results = decide_bidding(mr_name)

    if not results:
        print("해당 MR을 찾을 수 없거나 품목이 없습니다.")

    for item_code, info in results.items():
        status = "비딩 필요" if info["needs_bidding"] else "비딩 불필요"
        print(f"\n[{item_code}] {status}")
        for reason in info["reasons"]:
            print(f"  - {reason}")
