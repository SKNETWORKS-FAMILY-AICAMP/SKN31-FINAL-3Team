"""Pure mapping from backend graph state to stable frontend stages."""

from __future__ import annotations

from typing import Any


STATUS_TO_STAGE = {
    "started": "MR_REVIEW",
    "checking_mr_item": "ITEM_CHECK",
    "awaiting_substitute_selection": "SUBSTITUTE_DECISION",
    "substitute_selected": "SUBSTITUTE_SELECTED",
    "urgent_no_supplier_cancelled": "CANCELLED",
    "checking_bidding": "BIDDING_DECISION",
    # Legacy checkpoints created before the direct-purchase path was connected
    # are exposed for an explicit bidding-decision restart.
    "catalog_purchase_required": "HUMAN_REVIEW",
    "resolving_suppliers": "SUPPLIER_RECOMMENDATION",
    "resolving_supplier_pool": "SUPPLIER_RECOMMENDATION",
    "searching_suppliers": "SUPPLIER_RECOMMENDATION",
    "supplier_search_completed": "SUPPLIER_RECOMMENDATION",
    "awaiting_supplier_approval": "RFQ_TARGET_SELECTION",
    "creating_rfq": "RFQ_SENDING",
    "awaiting_quotation_check": "QUOTATION_COLLECTION",
    "awaiting_final_selection": "SUPPLIER_SELECTION",
    "supplier_selected": "ORDER_START",
    "awaiting_po_approval": "PRE_PO_APPROVAL",
    "creating_po": "PO_CREATION",
    "po_sent": "DELIVERY",
    "human_review": "HUMAN_REVIEW",
}

INTERRUPT_STATUSES = {
    "awaiting_substitute_selection",
    "awaiting_supplier_approval",
    "awaiting_quotation_check",
    "awaiting_final_selection",
    "supplier_selected",
    "awaiting_po_approval",
}


def project_graph_status(graph_status: str | None) -> tuple[str, str]:
    """Return ``(case_status, frontend_stage)`` for one graph status."""

    value = (graph_status or "").strip()
    stage = STATUS_TO_STAGE.get(value, "PROCESSING")
    if value == "po_sent":
        return "RUNNING", stage
    if value in {"substitute_selected", "urgent_no_supplier_cancelled"}:
        return "CANCELLED", stage
    if value in {"human_review", "catalog_purchase_required"}:
        return "FAILED", stage
    if value in INTERRUPT_STATUSES:
        return "WAITING_INPUT", stage
    return "RUNNING", stage


def task_presentation(payload: dict[str, Any]) -> dict[str, Any]:
    task_type = str(payload.get("type") or "workflow_input")
    presentations = {
        "substitute_selection": (
            "REQUESTER",
            "ERP_NEXT",
            "대체품 선택이 필요합니다",
            "구매 요청자가 ERPNext에서 대체품 사용 또는 신규 구매를 선택합니다.",
        ),
        "supplier_approval": (
            "BUYER",
            "BIDDINGFLOW",
            "RFQ 발송 협력사를 확인해주세요",
            "추천 협력사와 이메일을 확인하고 RFQ 발송 대상을 선택합니다.",
        ),
        "rfq_target_selection": (
            "BUYER",
            "BIDDINGFLOW",
            "RFQ 발송 협력사를 확인해주세요",
            "추천 협력사와 이메일을 확인하고 RFQ 발송 대상을 선택합니다.",
        ),
        "select_rfq_targets": (
            "BUYER",
            "BIDDINGFLOW",
            "RFQ 발송 협력사를 확인해주세요",
            "추천 협력사와 이메일을 확인하고 RFQ 발송 대상과 견적 마감일을 선택합니다.",
        ),
        "quotation_check": (
            "BUYER",
            "BIDDINGFLOW",
            "견적 회신을 확인해주세요",
            "도착한 견적을 확인하거나 마감까지 대기하거나 최종 선정을 진행합니다.",
        ),
        "check_quotations": (
            "BUYER",
            "BIDDINGFLOW",
            "견적 회신을 확인해주세요",
            "도착한 견적을 확인하거나 마감까지 대기하거나 최종 선정을 진행합니다.",
        ),
        "final_selection": (
            "BUYER",
            "BIDDINGFLOW",
            "최종 협력사를 선정해주세요",
            "AI 견적 순위와 근거를 검토한 뒤 최종 협력사를 선택합니다.",
        ),
        "order_start": (
            "BUYER",
            "BIDDINGFLOW",
            "선정 완료 · 발주를 시작할까요?",
            "선정 결과를 확인하고 PO 관리의 최종 승인 단계로 이동합니다.",
        ),
        "po_approval": (
            "BUYER",
            "BIDDINGFLOW",
            "PO 발송 전 최종 승인이 필요합니다",
            "금액·납기·협력사를 확인한 뒤 PO 생성 및 발송을 승인하거나 반려합니다.",
        ),
    }
    audience, channel, title, description = presentations.get(
        task_type,
        ("BUYER", "BIDDINGFLOW", "확인이 필요한 구매 작업입니다", "내용을 확인하고 작업을 계속해주세요."),
    )
    return {
        "task_type": task_type,
        "audience": audience,
        "channel": channel,
        "title": title,
        "description": description,
    }


def task_input_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe the interrupt as a frontend-renderable form contract."""

    task_type = str(payload.get("type") or "workflow_input")
    if task_type == "substitute_selection":
        options = [
            {"label": str(code), "value": str(code)}
            for code in payload.get("allowed_item_codes", [])
        ]
        options.append({"label": "신규 구매 진행", "value": "new_purchase"})
        return {"type": "single_choice", "field": "item_code", "options": options}
    if task_type in {"supplier_approval", "rfq_target_selection", "select_rfq_targets"}:
        return {
            "type": "supplier_selection",
            "field": "suppliers",
            "allow_email_edit": True,
            "allow_manual_supplier": True,
        }
    if task_type in {"quotation_check", "check_quotations"}:
        return {
            "type": "single_choice",
            "field": "decision",
            "options": [
                {"label": "회신 새로 확인", "value": "check"},
                {"label": "나중에 확인", "value": "later"},
                {"label": "최종 선정 진행", "value": "finalize"},
            ],
        }
    if task_type == "final_selection":
        return {"type": "supplier_ranking_selection", "field": "supplier"}
    if task_type == "order_start":
        return {
            "type": "confirmation",
            "field": "decision",
            "confirm_value": "start_order",
            "confirm_label": "발주 시작",
        }
    if task_type == "po_approval":
        return {
            "type": "single_choice",
            "field": "decision",
            "options": [
                {"label": "승인 후 PO 발송", "value": "approve"},
                {"label": "반려", "value": "reject"},
            ],
        }
    if task_type == "supplier_scorecard":
        return {
            "type": "scorecard",
            "fields": ["leadTime", "quality", "price", "service", "communication"],
            "minimum": 1,
            "maximum": 5,
        }
    return {"type": "json"}
