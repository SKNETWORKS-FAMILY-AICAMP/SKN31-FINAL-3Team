"""Bounded, checkpoint-free assistant graph. Never resumes the purchase graph.

Browser dialogue is a hint only. Every selected reference is read again through
the authenticated query port. No database rows or assistant answers go to the
planner, and there are no mutation tools, graph retries, or background tasks.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .models import AssistantMessageRequest, AssistantMessageResponse, AssistantPlan, CaseQueryFilters
from .query_routing import asks_for_count, due_window, waiting_group
from .stage_presenter import STAGE_PRESENTATION, STATUS_LABELS


class AssistantState(TypedDict, total=False):
    request: AssistantMessageRequest
    current_user: dict[str, Any]
    plan: AssistantPlan
    clarification: str | None
    inherit: bool
    next_page: bool
    records: list
    query_available: bool
    freshness_attempted: bool
    freshness_refreshed: bool
    response: AssistantMessageResponse


def resolve_context(state: AssistantState):
    request = state["request"]
    message = request.message.strip()
    previous = request.dialogue
    # New explicit MR identifiers and execution requests are never rewritten.
    if re.search(r"MAT-MR-\d{4}-\d+", message, re.I) or any(
        word in message for word in ("시작해", "승인해", "반려해", "발송해", "보내줘", "삭제해", "진행해")
    ):
        return {"inherit": False}
    compact = re.sub(r"\s", "", message)
    ordinal = re.search(r"(?:(\d+)(?:번째|번)|(?P<word>첫|두|세|네|다섯)번째)", compact)
    referring = any(word in compact for word in ("이단계", "그단계", "이건", "그건", "이거", "그거", "해당건", "관련화면"))
    subset = any(word in compact for word in ("그중", "그중에서", "그가운데", "이중", "이목록", "방금조회"))
    next_page = bool(re.search(r"다음(?:10)?(?:건|개|페이지|목록)", compact))
    if ordinal or (referring and previous and previous.references and not subset):
        refs = previous.references if previous else []
        index = (int(ordinal.group(1)) - 1 if ordinal and ordinal.group(1)
                 else ["첫", "두", "세", "네", "다섯"].index(ordinal.group("word")) if ordinal
                 else 0 if len(refs) == 1 else -1)
        if not (0 <= index < len(refs)):
            return {"clarification": "어느 구매 건을 말씀하시는지 알려주세요. 방금 목록의 순서(예: 첫 번째 건)나 MAT-MR 번호를 적어 주세요."}
        reference = refs[index]
        if not re.fullmatch(r"MAT-MR-\d{4}-\d+", reference, re.I):
            return {"clarification": "올바른 MAT-MR 번호로 다시 질문해 주세요."}
        # Only the identifier is reused, never a stage or permission cached by a client.
        return {"request": request.model_copy(update={"message": f"{reference} 현재 단계와 다음 행동 알려줘"}), "inherit": False}
    if subset or next_page:
        if not previous or previous.filters is None:
            return {"clarification": "먼저 어떤 작업 목록인지 알려주세요. 예: ‘무선 마우스 구매 작업 보여줘’ 또는 ‘외부 응답 대기 작업 보여줘’."}
        if next_page and previous.filters.exact_reference:
            return {"clarification": "지금은 MR 한 건을 조회한 상태입니다. 다른 작업도 보려면 품목이나 대기 조건으로 목록을 요청해 주세요."}
        if next_page and previous.filters.offset + previous.filters.limit > 1990:
            return {"clarification": "조회 범위를 넘었습니다. 품목이나 대기 단계를 지정해 목록을 좁혀 주세요."}
        return {"inherit": True, "next_page": next_page}
    if referring or "주의사항" in compact:
        if previous and previous.guide_query:
            context = request.context.model_copy(update={"current_tab": previous.guide_target}) if previous.guide_target else request.context
            return {"request": request.model_copy(update={
                "message": (previous.guide_query + " 사용법 " + message)[:3000], "context": context,
            })}
        return {"clarification": "어떤 구매 건이나 화면을 말씀하시는지 알려주세요. MR 번호 또는 화면 이름을 적어 주시면 이어서 안내하겠습니다."}
    return {"inherit": False}


def apply_context_filters(state: AssistantState, plan: AssistantPlan) -> AssistantPlan:
    """Only an explicit follow-up inherits filters; a new topic starts clean."""
    if not state.get("inherit"):
        return plan
    request = state["request"]
    previous = request.dialogue.filters
    message = request.message
    filters = previous.model_copy(deep=True)
    filters.count_requested = asks_for_count(message)
    filters.offset = previous.offset + previous.limit if state.get("next_page") else 0
    if not state.get("next_page"):
        # Do not inherit the planner's guessed reference/keyword from free text
        # history. Only fields explicitly represented by this refinement apply.
        group = waiting_group(message)
        if group:
            filters.waiting_for, filters.stage, filters.status = group, None, None
        window = due_window(message)
        if window is not None:
            filters.due_within_days = window
        if "첨부" in message:
            filters.has_attachments = not any(word in message for word in ("없는", "없음", "없이"))
        for word, status in (("완료", "COMPLETED"), ("취소", "CANCELLED"), ("반려", "REJECTED"), ("실패", "FAILED")):
            if word in message:
                filters.status, filters.waiting_for = status, None
                filters.include_closed = True
    return AssistantPlan(intent="case_query", query=message[:300], filters=filters)


def validate_plan(plan: AssistantPlan) -> str | None:
    filters = plan.filters
    filters.limit = min(filters.limit, 10)
    if plan.intent not in {"case_query", "case_status"}:
        return None
    if filters.stage:
        filters.stage = filters.stage.upper()
        if filters.stage not in STAGE_PRESENTATION:
            return "해당 단계는 현재 조회 조건으로 구분하기 어렵습니다. MR 번호나 ‘견적 회신 대기’, ‘PO 승인 대기’처럼 원하는 단계를 알려주세요."
    if filters.status:
        filters.status = filters.status.upper()
        if filters.status not in {*STATUS_LABELS, "QUEUED"}:
            return "요청하신 상태를 정확히 구분하지 못했습니다. 승인 대기, 외부 응답 대기, 완료 중 어떤 작업을 찾으시나요?"
    return None


def clarify(state: AssistantState):
    return {"response": AssistantMessageResponse(
        answer=state["clarification"], intent="clarification", dialogue=state["request"].dialogue,
        followups=["외부 응답 대기 작업 보여줘", "현재 화면 사용법 알려줘"],
        meta={"read_only": True, "query_executed": False, "graph": "read-only-assistant-v1"},
    )}


def build_assistant_graph(service):
    graph = StateGraph(AssistantState)
    graph.add_node("resolve_context", resolve_context)
    graph.add_node("plan_and_validate", service._plan_node)
    graph.add_node("authorized_read", service._read_node)
    graph.add_node("grounded_response", service._respond_node)
    graph.add_node("clarify", clarify)
    graph.add_edge(START, "resolve_context")
    graph.add_conditional_edges("resolve_context", lambda s: "clarify" if s.get("clarification") else "plan_and_validate")
    graph.add_conditional_edges("plan_and_validate", lambda s: "clarify" if s.get("clarification") else "authorized_read")
    graph.add_edge("authorized_read", "grounded_response")
    graph.add_edge("grounded_response", END)
    graph.add_edge("clarify", END)
    return graph.compile()  # No checkpointer/store: no purchase SQLite or DB migrations.
