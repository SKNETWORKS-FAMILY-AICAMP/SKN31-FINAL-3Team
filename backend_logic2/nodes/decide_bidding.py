"""
nodes/decide_bidding.py — 3번 모듈: 이 MR 품목이 비딩(경쟁견적) 대상인지 판별

기준 (하나라도 만족하면 비딩 필요 — OR조건):
  1. 금액 ≥ 20,000,000원 (과거 거래이력이 있으면 가장 최근 단가로 계산.
     이력이 없으면 계산 자체가 불가능한데, 그 경우는 아래 2번 기준이 대신 잡아줌)
  2. 신규거래 — 이 품목을 과거에 한 번도 구매한 적 없음
  3. 구매주기 불규칙 — 과거 주문 3건 이상 & 주문간격 변동계수(CV) ≥ 0.5
  4. 수량 ≥ 2,000개

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/decide_bidding.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statistics
from datetime import datetime
from erp_client import erp_get, erp_get_one

AMOUNT_THRESHOLD = 20_000_000       # 금액 기준(원)
QUANTITY_THRESHOLD = 2_000          # 수량 기준(개)
IRREGULAR_CV_THRESHOLD = 0.5        # 주문간격 변동계수 기준
MIN_ORDERS_FOR_IRREGULARITY = 3     # 불규칙성 판단에 필요한 최소 과거 주문 건수


def _get_past_purchases(item_code):
    """
    이 품목의 과거 확정 구매(Purchase Order) 이력을 오래된순으로 가져옴.
    날짜뿐 아니라 단가(rate)도 같이 가져와서, 신규거래·불규칙성·금액추정
    3가지 판단에 이 함수 하나만 재사용함 (API 호출 중복 방지).

    반환: [{"date": "YYYY-MM-DD", "rate": 숫자}, ...] (오래된순)
    """
    orders = erp_get(
        "Purchase Order",
        filters=[
            ["Purchase Order Item", "item_code", "=", item_code],
            ["docstatus", "=", 1],
        ],
        fields=["name", "transaction_date"],
    )
    if not orders:
        return []

    purchases = []
    for o in orders:
        po_doc = erp_get_one("Purchase Order", o["name"])
        for item_line in po_doc.get("items", []):
            if item_line["item_code"] == item_code:
                purchases.append({"date": o["transaction_date"], "rate": item_line.get("rate") or 0})
                break  # 같은 PO 안에 같은 품목이 여러 줄 있어도 첫 매칭만 사용

    purchases.sort(key=lambda p: p["date"])
    return purchases


def _is_new_transaction(purchases):
    """과거 구매이력이 아예 한 번도 없으면 신규거래로 판단"""
    return len(purchases) == 0


def _is_irregular_interval(purchases):
    """
    과거 주문이 3건 이상 있을 때만 판단.
    주문 간격(일수)들의 변동계수(CV = 표준편차/평균)가 기준 이상이면 불규칙.
    """
    if len(purchases) < MIN_ORDERS_FOR_IRREGULARITY:
        return False

    date_objs = [datetime.strptime(p["date"], "%Y-%m-%d") for p in purchases]
    intervals = [(date_objs[i + 1] - date_objs[i]).days for i in range(len(date_objs) - 1)]

    if not intervals or statistics.mean(intervals) == 0:
        return False

    cv = statistics.stdev(intervals) / statistics.mean(intervals) if len(intervals) > 1 else 0
    return cv >= IRREGULAR_CV_THRESHOLD


def _estimate_amount(purchases, qty):
    """과거 거래이력이 있으면 가장 최근(마지막) 단가로 금액 추정. 없으면 0
    (이 경우는 신규거래 기준이 대신 비딩 필요로 잡아줌)"""
    if not purchases:
        return 0
    latest_rate = purchases[-1]["rate"]
    return qty * latest_rate


def decide_bidding(mr_name):
    """
    MR 안의 각 품목마다 비딩이 필요한지 판단.
    반환: {item_code: {"needs_bidding": bool, "reasons": [...]}}
    """
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return {}

    results = {}

    for line in mr.get("items", []):
        item_code = line["item_code"]
        qty = line["qty"]
        purchases = _get_past_purchases(item_code)

        reasons = []

        amount = _estimate_amount(purchases, qty)
        if amount >= AMOUNT_THRESHOLD:
            reasons.append(f"금액 {amount:,.0f}원 ≥ 기준({AMOUNT_THRESHOLD:,}원)")

        if qty >= QUANTITY_THRESHOLD:
            reasons.append(f"수량 {qty}개 ≥ 기준({QUANTITY_THRESHOLD}개)")

        if _is_new_transaction(purchases):
            reasons.append("신규거래(과거 구매이력 없음)")

        if _is_irregular_interval(purchases):
            reasons.append(f"구매주기 불규칙(변동계수 ≥ {IRREGULAR_CV_THRESHOLD})")

        results[item_code] = {
            "needs_bidding": len(reasons) > 0,
            "reasons": reasons,
        }

    return results


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()
    results = decide_bidding(mr_name)

    if not results:
        print("해당 MR을 찾을 수 없거나 품목이 없습니다.")

    for item_code, info in results.items():
        status = "비딩 필요" if info["needs_bidding"] else "비딩 불필요 (카탈로그 등 기존절차)"
        print(f"\n[{item_code}] {status}")
        for r in info["reasons"]:
            print(f"  - {r}")