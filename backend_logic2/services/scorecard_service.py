"""Calculate supplier ratings from BiddingFlow's persisted procurement data."""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def automatic_scores(case: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    values = (case.get("workflow_snapshot") or {}).get("values") or {}
    promised = delivery.get("promised_delivery_date") or (case.get("summary") or {}).get("schedule_date")
    received = delivery.get("full_receipt_date")
    try:
        if delivery.get("delivery_status") != "FULL":
            raise ValueError()
        days = (date.fromisoformat(str(promised)[:10]) - date.fromisoformat(str(received)[:10])).days
        scores["leadTime"] = max(1, min(5, 3 + days))
        timing = f"{days}일 조기" if days > 0 else f"{-days}일 지연" if days < 0 else "약정일 당일"
        reasons["leadTime"] = f"약정 {str(promised)[:10]} · 수령 {str(received)[:10]} · {timing}"
    except (ValueError, TypeError):
        reasons["leadTime"] = "약정 납기일과 전체 입고의 실제 수령일이 필요합니다."

    quotations = (case.get("quotation_snapshot") or {}).get("quotations") or []
    item_code = case.get("item_code") or (case.get("summary") or {}).get("item_code")
    supplier = delivery.get("supplier") or values.get("selected_supplier")
    candidates = []
    for row in quotations:
        if row.get("item_code") != item_code or row.get("docstatus") == 2:
            continue
        try:
            rate = Decimal(str(row.get("rate")))
            if not rate.is_finite() or rate <= 0:
                continue
        except (InvalidOperation, TypeError, ValueError):
            continue
        candidates.append((row, rate))
    selected = [(row, rate) for row, rate in candidates if row.get("supplier") == supplier]
    if len(selected) != 1:
        reasons["price"] = "선정 협력사의 동일 품목 견적 단가를 하나로 확인할 수 없습니다."
    elif len({(row.get("currency"), row.get("uom")) for row, _ in candidates}) > 1:
        reasons["price"] = "견적의 통화와 단위가 같아야 단가를 비교할 수 있습니다."
    else:
        rate = selected[0][1]
        highest = max(value for _, value in candidates)
        scores["price"] = float(max(Decimal(1), rate / highest * 5).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        reasons["price"] = f"견적 단가 {rate:,.2f} ÷ 최고 단가 {highest:,.2f} × 5 (비교 {len(candidates)}건)"
    return {"scores": scores, "reasons": reasons}


def completed_scores(case: dict[str, Any], delivery: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    manual = {"quality", "service", "communication"}
    # Accept old clients' five-field payloads, but never trust their automatic scores.
    if not manual <= set(answer) or set(answer) - manual - {"leadTime", "price"}:
        raise ValueError("품질, 대응력, 커뮤니케이션을 각각 1~5점으로 평가해주세요.")
    if any(type(answer[key]) not in (int, float) or answer[key] not in (1, 2, 3, 4, 5) for key in manual):
        raise ValueError("직접 평가 항목은 각각 1~5점의 정수여야 합니다.")
    automatic = automatic_scores(case, delivery)
    if len(automatic["scores"]) != 2:
        raise ValueError("자동 평가에 필요한 납기일, 실제 수령일 또는 비교 견적을 확인해주세요.")
    return {**{key: answer[key] for key in manual}, **automatic["scores"],
            "calculation": {"version": 1, "reasons": automatic["reasons"]}}
