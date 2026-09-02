"""
api/mr_substitute_routes.py — MR 화면의 "AI 대체품 확인" 버튼(Client Script)이
호출하는 API (2026-09-01, 엔드포인트+Client Script 방식).

배경: substitute_selection 노드는 interrupt()로 멈춰서 resume을 기다림.
예전엔 사람이 CLI로 직접 답했고, 그 다음엔 ERPNext 댓글을 폴링해서 파싱하는
방식(substitute_reply_watcher.py)을 시도했는데, Frappe 타임라인의 "답장"
기능이 원본 댓글을 인용해서 같이 보내는 바람에 파싱이 꼬이는 문제가 실사용
에서 확인됨("1"이라고 답했는데 인용된 "구매"라는 단어에 걸려 new_purchase로
오판정됨). 그래서 Client Script(진짜 버튼+Dialog)가 이 API를 직접 호출하는
방식으로 전환 - 사람이 버튼을 누르면 그 값이 그대로 오니까 텍스트 파싱
자체가 필요없어짐.

인증: 이 API를 부르는 쪽(ERPNext Client Script)은 우리 자체 로그인
세션(auth_service)이 없어서, 공유 시크릿 헤더(X-Client-Script-Secret)로
검증함 - 완벽한 사용자별 인증은 아니고, erp_client.py가 이미 공용 API키
하나로 돌아가는 것과 같은 수준의 신뢰모델임(item_validation.py 상단
docstring에도 같은 한계가 이미 명시돼있음).

라우트:
  GET  /api/mr/{mr_name}/substitutes         - 대체품 후보 조회
  POST /api/mr/{mr_name}/substitute-decision - 선택 결과 처리(그래프 resume)

create_mr_substitute_client_script.py(레포 루트)로 이 API를 부르는
Client Script를 ERPNext에 등록해야 실제로 버튼이 뜸.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from langgraph.types import Command

from backend_logic2.nodes.mr.find_substitute import flatten_substitute_candidates
from backend_logic2.workflow.process_commands import to_checkpoint_data
from backend_logic2.workflow.process_graph import get_process_app

router = APIRouter(prefix="/api/mr", tags=["MR Substitute Decision"])


def _require_client_script_secret(x_client_script_secret: Optional[str] = Header(default=None)):
    expected = os.environ.get("CLIENT_SCRIPT_SECRET")
    if not expected:
        raise HTTPException(status_code=500, detail="CLIENT_SCRIPT_SECRET이 서버에 설정되어 있지 않습니다.")
    if x_client_script_secret != expected:
        raise HTTPException(status_code=401, detail="인증 실패")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class SubstituteDecisionRequest(BaseModel):
    item_code: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None


@router.get("/{mr_name}/substitutes")
def get_substitutes(mr_name: str, _auth=Depends(_require_client_script_secret)):
    """
    현재 이 MR이 대체품 선택을 기다리는 중인지 + 후보 목록을 반환.
    supplier_search처럼 새로 재탐색하지 않고, 그래프가 interrupt()로 멈추며
    이미 들고 있는 state(substitute_results)를 그대로 읽음 - 재계산 없이,
    실제로 resume될 때와 항상 정확히 같은 후보/번호를 보장하기 위해서.
    """
    app = get_process_app()
    snapshot = app.get_state(_config(mr_name))
    values = snapshot.values or {}

    if values.get("status") != "awaiting_substitute_selection":
        return {
            "mr_name": mr_name,
            "awaiting": False,
            "status": values.get("status"),
            "candidates": [],
        }

    substitute_results = values.get("substitute_results", {})
    candidates = flatten_substitute_candidates(substitute_results)

    return to_checkpoint_data({
        "mr_name": mr_name,
        "awaiting": True,
        "status": values.get("status"),
        "candidates": candidates,
    })


@router.post("/{mr_name}/substitute-decision")
def submit_substitute_decision(
    mr_name: str,
    body: SubstituteDecisionRequest,
    _auth=Depends(_require_client_script_secret),
):
    """
    Client Script Dialog에서 사람이 실제로 누른 값을 그대로 받아서 그래프
    resume() - process_cli.py의 `resume --item-code`/`resume --decision`과
    완전히 같은 경로(같은 검증 로직: substitute_selection_command). 성공
    여부는 resume 이후 상태를 다시 읽어서 판단함 - 무효 입력이면
    substitute_selection_command 자체가 다시 awaiting_substitute_selection
    으로 되돌려놓기 때문에, 그 상태가 그대로면 실패로 봄.
    """
    if not body.item_code and not body.decision:
        raise HTTPException(status_code=400, detail="item_code 또는 decision 중 하나는 필요합니다.")

    normalized_decision = str(body.decision or "").strip().lower()
    if normalized_decision in {"reject", "force_reject"}:
        # Client Script의 강제 반려도 댓글만 남기고 Pending에 방치하지 않는다.
        # Draft는 Discard, 이미 Submit된 MR은 Cancel한다.
        from backend_logic2.nodes.mr.reject_material_request import reject_material_request
        from backend_logic2.repositories.cases import get_case_by_mr, transition_case
        from backend_logic2.repositories.tasks import cancel_pending_tasks

        reason = (body.reason or "대체품 검토 결과 구매 요청이 반려되었습니다.").strip()
        reject_material_request(
            mr_name,
            reason,
            reason_code="FORCE_REJECT" if normalized_decision == "force_reject" else "SUBSTITUTE_REJECTED",
        )
        case = get_case_by_mr(mr_name)
        if case:
            cancel_pending_tasks(str(case["case_id"]), reason=reason)
            transition_case(
                str(case["case_id"]),
                status="CANCELLED",
                stage="CANCELLED",
                reason=reason,
                triggered_by="erpnext_client_script",
            )
        return {
            "mr_name": mr_name,
            "success": True,
            "status": "cancelled",
            "reason": reason,
        }

    app = get_process_app()
    resume_data = {}
    if body.decision:
        resume_data["decision"] = body.decision
    if body.item_code:
        resume_data["item_code"] = body.item_code

    app.invoke(Command(resume=resume_data), config=_config(mr_name))

    new_values = (app.get_state(_config(mr_name)).values) or {}
    still_waiting = new_values.get("status") == "awaiting_substitute_selection"

    # ERPNext 요청자가 선택한 결과를 PostgreSQL에 투영하고 알림/SSE로
    # 구매 화면을 깨운다. 이 보조 처리가 실패해도 그래프 결정은 보존된다.
    try:
        from backend_logic2.services.workflow_service import project_substitute_decision

        is_new_purchase = (
            str(body.decision or "").strip().lower() == "new_purchase"
            or str(body.item_code or "").strip().lower() == "new_purchase"
        )
        project_substitute_decision(
            mr_name,
            new_purchase=is_new_purchase,
            selected_item_code=None if is_new_purchase else body.item_code,
        )
    except Exception as exc:
        print(f"[mr_substitute_routes] 구매 작업 상태 투영 실패({mr_name}): {exc}")

    return to_checkpoint_data({
        "mr_name": mr_name,
        "success": not still_waiting,
        "status": new_values.get("status"),
        "error": new_values.get("error") if still_waiting else None,
    })
