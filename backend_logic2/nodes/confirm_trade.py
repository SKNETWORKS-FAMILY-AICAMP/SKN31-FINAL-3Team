from datetime import datetime
from typing import Dict, Any, List
# 필요 시 프로젝트 내 다른 노드나 도구 import
# from backend_logic2.erp_client import create_purchase_requisition_draft 

def confirm_trade_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    [8-3. 거래 확정 확인 노드]
    - PR 발송 후 공급사 응답 대기
    - 미응답 시 재확인 메일 자동 발송
    - 거절 또는 장기 미응답 시 차순위 Supplier로 강제 재진행
    """
    current_time = datetime.now()
    suppliers: List[Dict[str, Any]] = state.get("selected_suppliers", [])
    current_idx = state.get("current_supplier_index", 0)
    
    # 회사별 설정값 (기본값 설정)
    max_reminder_days = state.get("max_reminder_days", 2)
    max_no_response_days = state.get("max_no_response_days", 5)
    
    if not suppliers or current_idx >= len(suppliers):
        state["process_status"] = "FAILED_NO_SUPPLIERS"
        state["log_message"] = "진행 가능한 Supplier가 존재하지 않습니다."
        return state

    current_supplier = suppliers[current_idx]
    status = current_supplier.get("status", "PENDING")  # PENDING, CONFIRMED, REJECTED
    pr_sent_at = current_supplier.get("pr_sent_at", current_time)
    last_reminded_at = current_supplier.get("last_reminded_at", pr_sent_at)
    
    days_since_sent = (current_time - pr_sent_at).days
    days_since_reminder = (current_time - last_reminded_at).days

    # 1. 거래 확정 확인 완료 -> 8-4(PO 발송) 단계로 진행
    if status == "CONFIRMED":
        state["process_status"] = "TRADE_CONFIRMED"
        state["confirmed_supplier"] = current_supplier
        state["log_message"] = f"Supplier({current_supplier.get('name')}) 거래 확정 완료. PO 발행으로 이동합니다."
        return state

    # 2. 거래 거절 또는 장기 미응답 시 -> 차순위 Supplier로 강제 재진행
    if status == "REJECTED" or days_since_sent >= max_no_response_days:
        current_supplier["status"] = "REJECTED" if status == "REJECTED" else "TIMEOUT"
        next_idx = current_idx + 1
        
        if next_idx < len(suppliers):
            state["current_supplier_index"] = next_idx
            next_supplier = suppliers[next_idx]
            next_supplier["pr_sent_at"] = current_time
            next_supplier["status"] = "PENDING"
            
            # TODO: 차순위 공급사에게 PR 발송 처리 (erp_client 또는 email 도구 연동)
            # create_purchase_requisition_draft(next_supplier)
            
            state["process_status"] = "FALLBACK_NEXT_SUPPLIER"
            state["log_message"] = f"응답 지연/거절로 인해 차순위 Supplier({next_supplier.get('name')})로 재진행합니다."
        else:
            state["process_status"] = "ALL_SUPPLIERS_EXHAUSTED"
            state["log_message"] = "모든 후보 Supplier와의 협상이 결렬되었습니다. 담당자 검토가 필요합니다."
            
        return state

    # 3. 일정 기간 미수신 시 -> 재확인 메일 자동 발송
    if days_since_reminder >= max_reminder_days:
        current_supplier["reminder_count"] = current_supplier.get("reminder_count", 0) + 1
        current_supplier["last_reminded_at"] = current_time
        
        # TODO: Email 발송 함수 호출 (예: send_reminder_email(current_supplier))
        state["process_status"] = "REMINDER_SENT"
        state["log_message"] = f"미회신으로 인해 Supplier({current_supplier.get('name')})에 재확인 메일을 발송했습니다."
        return state

    state["process_status"] = "WAITING_RESPONSE"
    state["log_message"] = f"Supplier({current_supplier.get('name')})의 회신을 대기 중입니다."
    return state