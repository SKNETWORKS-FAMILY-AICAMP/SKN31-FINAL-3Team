"""Calculate supplier ratings from BiddingFlow's persisted procurement data."""

from datetime import date
from typing import Any

from backend_logic2.services.price_evaluation import build_price_evaluations


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

    snapshot = case.get("quotation_snapshot") or {}
    item_code = case.get("item_code") or (case.get("summary") or {}).get("item_code")
    supplier = delivery.get("supplier") or values.get("selected_supplier")
    evaluations = snapshot.get("price_evaluations")
    if evaluations is None:
        # Old cases already have quotations; no new ERP fetch or migration needed.
        evaluations = build_price_evaluations(snapshot.get("quotations") or [], item_code, snapshot.get("rfq_name"))
    basis = evaluations.get(supplier) or {}
    if basis.get("item_code") == item_code and isinstance(basis.get("score"), (int, float)):
        scores["price"] = basis["score"]
    reasons["price"] = basis.get("reason") or "선정 협력사의 동일 품목 견적 단가를 확인할 수 없습니다."
    return {"scores": scores, "reasons": reasons, "price_basis": basis}


def completed_scores(case: dict[str, Any], delivery: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    manual = {"quality", "service", "communication"}
    # Accept old clients' five-field payloads, but never trust their automatic scores.
    if not manual <= set(answer) or set(answer) - manual - {"leadTime", "price"}:
        raise ValueError("품질, 대응력, 커뮤니케이션을 각각 1~5점으로 평가해주세요.")
    if any(type(answer[key]) not in (int, float) or answer[key] not in (1, 2, 3, 4, 5) for key in manual):
        raise ValueError("직접 평가 항목은 각각 1~5점의 정수여야 합니다.")
    automatic = automatic_scores(case, delivery)
    if "leadTime" not in automatic["scores"]:
        raise ValueError("자동 평가에 필요한 약정 납기일과 실제 수령일을 확인해주세요.")
    return {**{key: answer[key] for key in manual}, **automatic["scores"],
            "calculation": {"version": 1, "reasons": automatic["reasons"],
                            "price_basis": automatic["price_basis"],
                            "excluded_fields": [] if "price" in automatic["scores"] else ["price"]}}
