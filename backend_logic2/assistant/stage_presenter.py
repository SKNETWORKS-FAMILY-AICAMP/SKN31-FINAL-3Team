"""User-facing workflow language and navigation for canonical stages."""

from __future__ import annotations

from typing import Any

from .models import NavigationTarget


STAGE_PRESENTATION: dict[str, tuple[str, str, str, NavigationTarget]] = {
    "MR_REVIEW": ("구매 요청 검토", "구매 담당자", "요청 내용을 검토하고 승인 또는 반려합니다.", "mr-list"),
    "ITEM_CHECK": ("품목 확인", "AI 자동 처리", "품목 정보와 규격 확인이 끝날 때까지 기다립니다.", "mr-list"),
    "SUBSTITUTE_DECISION": ("대체품 선택 대기", "요청자", "ERPNext에서 대체품 사용 또는 신규 구매를 선택합니다.", "mr-list"),
    "SUBSTITUTE_SELECTED": ("대체품 선택 완료", "처리 완료", "대체품 사용으로 구매 요청이 종료되었습니다.", "mr-list"),
    "BIDDING_DECISION": ("구매 방식 판단", "AI 자동 처리", "입찰 또는 최근 거래처 즉시 구매 경로를 판단합니다.", "mr-list"),
    "SUPPLIER_RECOMMENDATION": ("공급사 탐색", "AI 자동 처리", "후보 공급사를 찾고 연락 정보를 확인합니다.", "vendor-select"),
    "RFQ_TARGET_SELECTION": ("견적 요청 대상 선택", "구매 담당자", "협력사와 견적 마감일을 확인한 뒤 견적 요청을 보냅니다.", "vendor-select"),
    "RFQ_SENDING": ("견적 요청 발송", "시스템", "선택한 협력사에 견적 요청을 보내는 중입니다.", "vendor-select"),
    "QUOTATION_COLLECTION": ("견적 회신 대기", "공급사", "회신 현황을 확인하거나 견적 마감일까지 기다립니다.", "vendor-select"),
    "SUPPLIER_SELECTION": ("최종 공급사 선택", "구매 담당자", "회신 견적을 비교하고 최종 공급사를 선택합니다.", "vendor-select"),
    "ORDER_START": ("발주 시작 대기", "구매 담당자", "선정 결과를 확인하고 발주 시작을 누릅니다.", "vendor-select"),
    "PRE_PO_APPROVAL": ("발주 승인 대기", "구매 담당자", "금액·납기·공급사를 확인하고 최종 승인합니다.", "po-manage"),
    "PR_REQUEST": ("공급사 수주 확인 요청", "구매 담당자", "공급사에 수주 가능 여부 확인을 요청합니다.", "po-manage"),
    "PR_SENDING": ("수주 확인 요청 발송", "시스템", "공급사에 확인 요청을 보내는 중입니다.", "po-manage"),
    "PR_RESPONSE_WAITING": ("공급사 응답 대기", "공급사", "공급사의 수주 가능 응답을 기다립니다.", "po-manage"),
    "PR_REJECTED": ("공급사 거절 검토", "구매 담당자", "거절 사유를 확인하고 다음 공급사를 선택합니다.", "po-manage"),
    "PO_CREATION": ("발주서 생성", "시스템", "승인된 내용으로 발주서를 생성하고 발송합니다.", "po-manage"),
    "DELIVERY": ("물품 도착 대기", "공급사", "입고가 등록될 때까지 기다립니다.", "po-manage"),
    "SCORECARD": ("협력사 평가", "구매 담당자", "도착한 물품과 거래 경험을 평가합니다.", "po-manage"),
    "COMPLETED": ("구매 완료", "처리 완료", "구매와 후속 처리가 완료되었습니다.", "po-manage"),
    "HUMAN_REVIEW": ("확인 필요", "구매 담당자", "중단 사유를 확인한 뒤 안내된 방식으로 다시 진행합니다.", "mr-list"),
    "CANCELLED": ("취소됨", "처리 완료", "취소 또는 반려 사유를 확인합니다.", "mr-list"),
    "PROCESSING": ("자동 처리 중", "AI 자동 처리", "현재 자동 처리 단계가 끝날 때까지 기다립니다.", "mr-list"),
}


STATUS_LABELS = {
    "DRAFT": "시작 전",
    "PENDING": "진행 대기",
    "RUNNING": "진행 중",
    "WAITING_INPUT": "사용자 확인 대기",
    "FAILED": "확인 필요",
    "REJECTED": "반려됨",
    "CANCELLED": "취소됨",
    "COMPLETED": "완료",
}


def present_stage(stage: str | None) -> tuple[str, str, str, NavigationTarget]:
    normalized = str(stage or "PROCESSING").strip().upper()
    return STAGE_PRESENTATION.get(normalized, STAGE_PRESENTATION["PROCESSING"])


def status_label(status: str | None) -> str:
    value = str(status or "").strip().upper()
    return STATUS_LABELS.get(value, value or "상태 미확인")


def summarize_case(row: dict[str, Any]) -> dict[str, str]:
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    stage = str(row.get("stage") or "PROCESSING").upper()
    label, waiting_on, next_action, target = present_stage(stage)
    item_name = str(
        summary.get("item_name")
        or summary.get("item_code")
        or "품목명 미지정"
    )
    return {
        "reference": str(row.get("mr_name") or summary.get("mr_name") or ""),
        "item_name": item_name,
        "stage": stage,
        "stage_label": label,
        "status": str(row.get("status") or "").upper(),
        "status_label": status_label(row.get("status")),
        "waiting_on": waiting_on,
        "next_action": next_action,
        "target": target,
    }
