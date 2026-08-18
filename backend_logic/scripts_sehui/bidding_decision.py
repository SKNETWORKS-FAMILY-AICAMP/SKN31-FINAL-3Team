"""ERPNext Material Request의 비딩 필요 여부를 판정하는 독립 모듈.

판정 기준은 아래 두 범주만 사용한다.
1. 대량 또는 고액 발주
2. 일회성 또는 비주기적 구매

공용 ``erp_client.py``는 수정하지 않고, 연결 정보와 예외 클래스만 재사용한다.
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
    """제출된 구매주문의 실제 거래일을 품목 기준으로 반환한다."""
    cutoff = date.today().toordinal() - lookback_days
    child_rows = _get_all(
        "Purchase Order Item",
        filters=[["item_code", "=", item_code], ["docstatus", "=", 1]],
        fields=["parent"],
    )

    dates: set[str] = set()
    for po_name in {row["parent"] for row in child_rows if row.get("parent")}:
        po = erp_get_one("Purchase Order", po_name)
        transaction_date = po.get("transaction_date") if po else None
        if not transaction_date:
            continue
        parsed = date.fromisoformat(transaction_date)
        if parsed.toordinal() >= cutoff:
            dates.add(parsed.isoformat())
    return tuple(sorted(dates))


def _is_irregular(dates: Iterable[str], policy: BiddingPolicy) -> bool:
    parsed = [date.fromisoformat(value) for value in sorted(dates)]
    if 0 < len(parsed) < policy.minimum_orders_for_cycle_check:
        # 반복성이 입증될 만큼의 이력이 없으므로 비주기 구매로 취급한다.
        return True
    if len(parsed) < policy.minimum_orders_for_cycle_check:
        return False

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
    """
    policy = policy or BiddingPolicy()
    request_doc = erp_get_one("Material Request", material_request_name)
    if not request_doc:
        raise ValueError(f"Material Request를 찾을 수 없습니다: {material_request_name}")
    if request_doc.get("material_request_type") != "Purchase":
        raise ValueError("구매 목적(Material Request Type = Purchase) 요청만 판정할 수 있습니다.")

    item_decisions: list[ItemDecision] = []
    total_amount = 0.0
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
        reasons: list[str] = []
        if is_large:
            reasons.append(
                f"대량 발주: {quantity:g} >= {policy.large_quantity_threshold:g}"
            )
        if is_one_time:
            reasons.append(
                f"일회성 구매: 최근 {policy.history_lookback_days}일 내 구매주문 {past_order_count}건"
            )
        if is_irregular:
            reasons.append("비주기적 구매: 구매 간격 변동계수가 기준 이상")

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
                reasons=tuple(reasons),
            )
        )

    is_high_amount = total_amount >= policy.high_amount_threshold
    reasons = [reason for item in item_decisions for reason in item.reasons]
    if is_high_amount:
        reasons.insert(
            0,
            f"고액 발주: {total_amount:,.0f} >= {policy.high_amount_threshold:,.0f}",
        )

    return BiddingDecision(
        material_request=material_request_name,
        bidding_required=is_high_amount or any(item.reasons for item in item_decisions),
        total_estimated_amount=total_amount,
        is_high_amount=is_high_amount,
        reasons=tuple(reasons),
        items=tuple(item_decisions),
    )


def is_bidding_required(
    material_request_name: str,
    policy: BiddingPolicy | None = None,
) -> bool:
    """기존 파이프라인에서 bool 값만 필요할 때 사용하는 간편 함수."""
    return decide_bidding(material_request_name, policy).bidding_required
