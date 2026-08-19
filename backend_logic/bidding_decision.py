"""ERPNext Material Request의 비딩 필요 여부를 판정하는 독립 모듈.

판정 기준은 아래 두 범주만 사용한다.
1. 대량 또는 고액 발주
2. 일회성 또는 비주기적 구매

공용 ``erp_client.py``는 수정하지 않고, 연결 정보와 예외 클래스만 재사용한다.

[수정 이력]
- _purchase_dates(): Purchase Order Item(자식테이블) 직접조회는 권한(403)
  문제가 반복 발생해서, 부모(Purchase Order)만 필터링하고 items는 파이썬에서
  대조하는 안전한 방식으로 되돌림. Role Permissions Manager에서
  'Purchase Order Item'에 Read 권한을 확실히 열어두면, 성능을 위해 다시
  2단계 조회 방식으로 바꿔도 됨 (그 경우를 위해 파일 맨 아래에 대안 버전 주석으로 남겨둠).
- decide_bidding(): is_high_amount/reasons/return이 for 루프 안에 잘못
  들여쓰기 되어있던 버그 수정. 이전 버전은 품목이 여러 개인 MR에서 첫
  품목만 처리하고 바로 종료되는 문제가 있었음.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from statistics import mean, pstdev
from typing import Any, Iterable
import json

import requests

from erp_client import ERPNextAPIError, HEADERS, SITE_URL, erp_get_one


@dataclass(frozen=True)
class BiddingPolicy:
    """회사별로 확정해야 하는 비딩 정책값."""

    large_quantity_threshold: float = 500
    high_amount_threshold: float = 10_000_000
    history_lookback_days: int = 730
    one_time_max_past_orders: int = 0
    minimum_orders_for_cycle_check: int = 3
    irregular_interval_cv_threshold: float = 0.50


@dataclass(frozen=True)
class ItemDecision:
    item_code: str
    quantity: float
    estimated_amount: float
    past_order_count: int
    purchase_dates: tuple[str, ...]
    is_large_quantity: bool
    is_one_time_purchase: bool
    is_irregular_purchase: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BiddingDecision:
    material_request: str
    bidding_required: bool
    total_estimated_amount: float
    is_high_amount: bool
    reasons: tuple[str, ...]
    items: tuple[ItemDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _get_all(doctype: str, filters: list, fields: list[str]) -> list[dict]:
    """Frappe REST 목록 API를 페이지 끝까지 조회한다."""
    rows: list[dict] = []
    page_length = 100
    start = 0

    while True:
        response = requests.get(
            f"{SITE_URL}/api/resource/{doctype}",
            headers=HEADERS,
            params={
                "filters": json.dumps(filters),
                "fields": json.dumps(fields),
                "limit_start": start,
                "limit_page_length": page_length,
                "order_by": "creation asc",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise ERPNextAPIError(
                f"GET {doctype}: {response.status_code} - {response.text[:300]}"
            )

        batch = response.json().get("data", [])
        rows.extend(batch)
        if len(batch) < page_length:
            return rows
        start += page_length


def _purchase_dates(item_code: str, lookback_days: int) -> tuple[str, ...]:
    """제출된 구매주문의 실제 거래일을 품목 기준으로 반환한다.

    ⚠️ Purchase Order Item(자식테이블) 직접조회는 권한 에러(403)가 반복
    발생해서, 부모(Purchase Order)를 기간으로 먼저 필터링해서 가져온 뒤,
    각 문서의 items 배열 안에서 파이썬으로 이 품목이 있는지 대조하는
    방식으로 되돌림. API 호출 수는 PO 건수에 비례해서 늘지만, 권한 문제
    없이 항상 동작하는 게 우선.
    """
    cutoff = date.today().toordinal() - lookback_days
    cutoff_date_str = date.fromordinal(cutoff).isoformat()

    purchase_orders = _get_all(
        "Purchase Order",
        filters=[
            ["docstatus", "=", 1],
            ["transaction_date", ">=", cutoff_date_str],
        ],
        fields=["name"],
    )

    dates: set[str] = set()
    for po_summary in purchase_orders:
        po_doc = erp_get_one("Purchase Order", po_summary["name"])
        if not po_doc:
            continue
        transaction_date = po_doc.get("transaction_date")
        if not transaction_date:
            continue
        if any(item.get("item_code") == item_code for item in po_doc.get("items", [])):
            dates.add(transaction_date)

    return tuple(sorted(dates))


def _is_irregular(dates: Iterable[str], policy: BiddingPolicy) -> bool:
    parsed = [date.fromisoformat(value) for value in sorted(dates)]
    if len(parsed) < policy.minimum_orders_for_cycle_check:
        return len(parsed) > 0

    intervals = [(right - left).days for left, right in zip(parsed, parsed[1:])]
    average_interval = mean(intervals)
    if average_interval <= 0:
        return False
    return pstdev(intervals) / average_interval >= policy.irregular_interval_cv_threshold


def _item_amount(item: dict, item_master: dict | None) -> float:
    """MR 금액을 우선 사용하고 없으면 단가 또는 품목 최근 구매가로 추정한다."""
    for field in ("base_amount", "amount"):
        amount = _to_float(item.get(field))
        if amount > 0:
            return amount

    qty = _to_float(item.get("qty"))
    for field in ("base_rate", "rate", "valuation_rate"):
        rate = _to_float(item.get(field))
        if rate > 0:
            return qty * rate
    return qty * _to_float((item_master or {}).get("last_purchase_rate"))


def decide_bidding(
    material_request_name: str,
    policy: BiddingPolicy | None = None,
) -> BiddingDecision:
    """Material Request 전체를 조회해 비딩 여부와 근거를 반환한다.

    네 조건 중 하나라도 참이면 비딩 대상으로 판정한다. 금액은 요청서 전체,
    대량·일회성·비주기성은 각 품목 단위로 평가한다.

    ⚠️ 수정: is_high_amount 계산과 최종 return을 for 루프 밖으로 뺌.
    이전 버전은 이게 루프 안에 있어서, 품목이 여러 개면 첫 품목만
    처리하고 함수가 끝나버리는 버그가 있었음.
    """
    policy = policy or BiddingPolicy()
    request_doc = erp_get_one("Material Request", material_request_name)
    if not request_doc:
        raise ValueError(f"Material Request를 찾을 수 없습니다: {material_request_name}")
    if request_doc.get("material_request_type") != "Purchase":
        raise ValueError("구매 목적(Material Request Type = Purchase) 요청만 판정할 수 있습니다.")

    item_decisions: list[ItemDecision] = []
    total_amount = 0.0

    # ----- 품목별 평가 (루프 안에서는 품목 단위 판단만 함) -----
    for item in request_doc.get("items", []):
        item_code = item.get("item_code")
        if not item_code:
            raise ValueError("Material Request 품목에 item_code가 없습니다.")

        item_master = erp_get_one("Item", item_code)
        amount = _item_amount(item, item_master)
        total_amount += amount
        quantity = _to_float(item.get("qty"))
        purchase_dates = _purchase_dates(item_code, policy.history_lookback_days)
        past_order_count = len(purchase_dates)

        is_large = quantity >= policy.large_quantity_threshold
        is_one_time = past_order_count <= policy.one_time_max_past_orders
        is_irregular = _is_irregular(purchase_dates, policy)

        item_reasons: list[str] = []
        if is_large:
            item_reasons.append(
                f"대량 발주: {quantity:g} >= {policy.large_quantity_threshold:g}"
            )
        if is_one_time:
            item_reasons.append(
                f"일회성 구매: 최근 {policy.history_lookback_days}일 내 구매주문 {past_order_count}건"
            )
        if is_irregular:
            item_reasons.append("비주기적 구매: 구매 간격 변동계수가 기준 이상")

        item_decisions.append(
            ItemDecision(
                item_code=item_code,
                quantity=quantity,
                estimated_amount=amount,
                past_order_count=past_order_count,
                purchase_dates=purchase_dates,
                is_large_quantity=is_large,
                is_one_time_purchase=is_one_time,
                is_irregular_purchase=is_irregular,
                reasons=tuple(item_reasons),
            )
        )

    # ----- 여기서부터는 루프 밖 — MR 전체를 다 본 뒤 최종 판단 -----
    is_high_amount = total_amount >= policy.high_amount_threshold
    overall_reasons: list[str] = []
    if is_high_amount:
        overall_reasons.append(
            f"고액 발주: {total_amount:,.0f} >= {policy.high_amount_threshold:,.0f}"
        )
    for item_decision in item_decisions:
        overall_reasons.extend(item_decision.reasons)

    return BiddingDecision(
        material_request=material_request_name,
        bidding_required=is_high_amount or any(item.reasons for item in item_decisions),
        total_estimated_amount=total_amount,
        is_high_amount=is_high_amount,
        reasons=tuple(overall_reasons),
        items=tuple(item_decisions),
    )


def is_bidding_required(
    material_request_name: str,
    policy: BiddingPolicy | None = None,
) -> bool:
    """기존 파이프라인에서 bool 값만 필요할 때 사용하는 간편 함수."""
    return decide_bidding(material_request_name, policy).bidding_required


# ============================================================
# 참고: Role Permissions Manager에서 'Purchase Order Item'에 Read
# 권한을 확실히 열어두셨다면, 아래 버전으로 _purchase_dates()를
# 교체하면 API 호출 수가 줄어들어 더 빠름 (PO 건수와 무관하게 최대 2회).
# 지금은 권한 문제 재발 방지를 위해 위쪽의 안전한 버전을 기본으로 둠.
# ============================================================
#
# def _purchase_dates(item_code, lookback_days):
#     cutoff = date.today().toordinal() - lookback_days
#     cutoff_date_str = date.fromordinal(cutoff).isoformat()
#
#     item_rows = _get_all(
#         "Purchase Order Item",
#         filters=[["item_code", "=", item_code]],
#         fields=["parent"],
#     )
#     parent_names = list({row["parent"] for row in item_rows})
#     if not parent_names:
#         return tuple()
#
#     purchase_orders = _get_all(
#         "Purchase Order",
#         filters=[
#             ["name", "in", parent_names],
#             ["docstatus", "=", 1],
#             ["transaction_date", ">=", cutoff_date_str],
#         ],
#         fields=["transaction_date"],
#     )
#     dates = {po["transaction_date"] for po in purchase_orders if po.get("transaction_date")}
#     return tuple(sorted(dates))