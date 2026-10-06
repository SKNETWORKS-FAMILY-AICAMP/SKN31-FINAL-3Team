"""Application service for read-only feature guidance and procurement lookup."""

from __future__ import annotations

import os
import re
import logging
from functools import lru_cache
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

from .adapters.json_feature_catalog import JsonFeatureCatalog
from .adapters.openai_responses import OpenAIResponsesAssistant
from .adapters.postgres_procurement_query import PostgresProcurementQuery
from .adapters.procurement_freshness import ProjectionOnlyFreshness
from .adapters.sqlite_help_knowledge import SQLiteHelpKnowledge
from .models import (
    AssistantAction,
    AssistantMessageRequest,
    AssistantMessageResponse,
    AssistantPlan,
    CaseQueryFilters,
    FeatureMatch,
    HelpMatch,
    ModelAnswer,
    DialogueContext,
    PendingClarification,
)
from .ports import (
    AssistantModelPort,
    FeatureCatalogPort,
    HelpKnowledgePort,
    ProcurementFreshnessPort,
    ProcurementQueryPort,
)
from .query_routing import waiting_group, item_keyword, asks_for_count, explicit_item_prefix, due_window, due_item_keyword, business_today, personal_scope, decision_question, requests_mutation
from .capabilities import normalize_capability
from .memory import planner_memory
from datetime import timedelta


MR_PATTERN = re.compile(r"\bMAT-MR-[0-9]{4}-[0-9]+\b", re.IGNORECASE)
CASE_WORDS = ("mr", "구매 요청", "승인 대기", "납기", "견적 회신", "진행 중", "반려")
HELP_WORDS = ("왜", "어떻게", "오류", "안 돼", "안돼", "실패", "설명", "사용법")
# Only presentation context is added. This never changes workflow state or permissions.
SCREEN_GUIDE_QUERIES = {
    "dashboard": "대시보드 사용법",
    "item-register": "아이템 목록 사용법",
    "mr-list": "구매 요청 검토 사용법",
    "vendor-select": "협력사 선정 화면 사용법",
    "po-manage": "PO 관리 화면 사용법",
    "company-policy": "회사 구매 정책 사용법",
    "ai-decision-log": "AI 판단 기록 사용법",
}
LOGGER = logging.getLogger(__name__)


def _truthy(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().casefold() in {"1", "true", "yes", "on"}


def _path_setting(name: str, default: Path) -> Path:
    configured = os.getenv(name, "").strip()
    return Path(configured) if configured else default


def actor_id(current_user: dict[str, Any]) -> str:
    return str(
        current_user.get("erp_user_id")
        or current_user.get("email")
        or current_user.get("id")
        or "unknown"
    ).strip()


def _heuristic_plan(message: str) -> AssistantPlan:
    lowered = message.casefold()
    mr_match = MR_PATTERN.search(message)
    filters = CaseQueryFilters()
    if mr_match:
        filters.exact_reference = mr_match.group(0).upper()
        # An exact reference is unambiguous, so include its terminal state too.
        filters.include_closed = True
        return AssistantPlan(
            intent="case_status",
            query=filters.exact_reference,
            filters=filters,
            require_freshness=any(word in lowered for word in ("erp", "최신", "방금", "갱신")),
        )
    if requests_mutation(message):
        return AssistantPlan(intent="feature_guide", query=message)
    # A usage question mentioning MR/납기 is not a request to query live cases.
    if any(word in lowered for word in (*HELP_WORDS, "어디", "방법", "버튼")):
        return AssistantPlan(intent="help", query=message)
    if decision_question(message):
        return AssistantPlan(intent='case_query', capability='my_tasks', query=message,
            filters=CaseQueryFilters(waiting_for='decision', task_scope=personal_scope(message) or 'visible', count_requested=asks_for_count(message)))
    group = waiting_group(message)
    if group:
        filters.waiting_for = group
        due_match = re.search(r"(\d+)\s*일\s*(?:이내|안)", message)
        if due_match:
            filters.due_within_days = min(int(due_match.group(1)), 365)
        filters.has_attachments = True if "첨부" in lowered else None
        return AssistantPlan(intent="case_query", query=message, filters=filters)
    window = due_window(message)
    if window is not None:
        filters.due_within_days = window
        return AssistantPlan(intent="case_query", query=message, filters=filters)
    if asks_for_count(message) and any(word in lowered for word in ("구매", "작업", "mr", "품목")):
        filters.keyword = explicit_item_prefix(message)
        filters.count_requested = True
        return AssistantPlan(intent="case_query", query=message, filters=filters)
    if any(word in lowered for word in ("보여", "찾아", "목록", "조회")):
        # Also usable during a model outage: product + purchase-list phrasing.
        keyword = explicit_item_prefix(message)
        if keyword or any(word in lowered for word in ("작업", "구매", "mr")):
            filters.keyword = keyword
            for word, status in (("완료", "COMPLETED"), ("반려", "REJECTED"), ("취소", "CANCELLED"), ("실패", "FAILED")):
                if word in lowered:
                    filters.status = status
                    filters.include_closed = True
            return AssistantPlan(intent="case_query", query=message, filters=filters)
    if any(word in lowered for word in CASE_WORDS):
        if "승인 대기" in lowered or "확인 대기" in lowered:
            filters.status = "WAITING_INPUT"
        if "견적 회신" in lowered or "견적 대기" in lowered:
            filters.stage = "QUOTATION_COLLECTION"
        elif "물품 도착" in lowered or "입고" in lowered:
            filters.stage = "DELIVERY"
        elif "평가" in lowered:
            filters.stage = "SCORECARD"
        elif "반려" in lowered:
            filters.status = "REJECTED"
            filters.include_closed = True
        due_match = re.search(r"(\d+)\s*일\s*(?:이내|안)", message)
        if due_match:
            filters.due_within_days = min(int(due_match.group(1)), 365)
        filters.has_attachments = True if "첨부" in lowered else None
        return AssistantPlan(intent="case_query", query=message, filters=filters)
    if any(word in lowered for word in HELP_WORDS):
        return AssistantPlan(intent="help", query=message)
    return AssistantPlan(intent="feature_guide", query=message)


class AssistantService:
    def __init__(
        self,
        *,
        feature_catalog: FeatureCatalogPort,
        help_knowledge: HelpKnowledgePort,
        procurement_query: ProcurementQueryPort,
        freshness: ProcurementFreshnessPort,
        model: AssistantModelPort,
    ):
        self.feature_catalog = feature_catalog
        self.help_knowledge = help_knowledge
        self.procurement_query = procurement_query
        self.freshness = freshness
        self.model = model
        # This graph owns no purchase checkpoint, graph lock, or ERP writer.
        from .graph import build_assistant_graph
        self.graph = build_assistant_graph(self)
        self._slots = BoundedSemaphore(2)

    def answer(
        self,
        request: AssistantMessageRequest,
        *,
        current_user: dict[str, Any],
    ) -> AssistantMessageResponse:
        if not self._slots.acquire(blocking=False):
            return AssistantMessageResponse(
                answer="현재 안내 요청이 많습니다. 잠시 뒤 다시 질문해 주세요. 구매 업무 처리는 영향을 받지 않습니다.",
                intent="general", dialogue=request.dialogue,
                meta={"read_only": True, "busy": True, "query_executed": False},
            )
        try:
            result = self.graph.invoke(
                {"request": request, "current_user": current_user, "original_message": request.message},
                config={"recursion_limit": 16},
            )
            return result["response"]
        finally:
            self._slots.release()

    def _plan_node(self, state):
        request = state["request"]
        message = request.message.strip()
        if state.get('resolved_plan'):
            return {'plan': normalize_capability(state['resolved_plan']), 'clarification': None}
        screen_question = (
            not MR_PATTERN.search(message)
            and any(word in message for word in ("현재 화면", "이 화면", "지금 화면"))
        )
        lookup_query = SCREEN_GUIDE_QUERIES[request.context.current_tab] if screen_question else message
        # Retrieve a small candidate set before model routing. This keeps the
        # prompt compact and provides a deterministic fallback if Luna is down.
        feature_candidates = self.feature_catalog.search(lookup_query, limit=5)
        help_candidates = self.help_knowledge.search(lookup_query, limit=4)
        # Assistant answers may contain private purchase facts. The planner
        # needs user utterances only; structured follow-ups are resolved locally.
        recent = [entry.model_dump() for entry in request.conversation[-16:] if entry.role == "user"][-8:]
        if request.dialogue and request.dialogue.memory.recent:
            recent = []  # Already represented with its paired interpretation.
        # Luna only produces a validated query plan here. It never receives a
        # database connection or a callable purchase-mutation tool.
        known_refinement = state.get('inherit') and (state.get('next_page') or waiting_group(message) or due_window(message) is not None or any(w in message for w in ('첨부', '완료', '반려', '취소', '실패')))
        deterministic_route = bool(MR_PATTERN.search(message) or screen_question or known_refinement
                                   or requests_mutation(message))
        plan = (None if deterministic_route else self.model.plan(
            message=message,
            context=request.context,
            recent_conversation=recent,
            feature_candidates=feature_candidates,
            help_candidates=help_candidates,
            memory=planner_memory(request.dialogue),
            feature_index=[{'id': f.id, 'title': f.title, 'purpose': f.summary, 'screen': f.target}
                for f in getattr(self.feature_catalog, 'all', lambda: [])()],
        )) or _heuristic_plan(message)
        plan = normalize_capability(plan)
        if plan.intent == 'clarification' or (plan.confidence == 'low' and plan.intent != 'unsupported'):
            question = plan.clarification_question or '어떤 작업을 찾으시나요? 구매 건 조회인지, 기능 사용법 안내인지 알려주세요.'
            return {'plan': plan, 'clarification': question,
                'pending': PendingClarification(original_message=message, question=question, choices=plan.choices, filters=plan.filters)}
        fallback_plan = _heuristic_plan(message)
        if MR_PATTERN.search(message):
            # An exact identifier must not be lost or combined with a guessed keyword.
            plan = fallback_plan
        elif requests_mutation(message):
            plan = fallback_plan
        elif fallback_plan.filters.waiting_for and plan.filters.waiting_for != 'decision':
            plan.intent = "case_query"
            plan.capability = None
            keyword = (due_item_keyword(plan.filters.keyword, message) if due_window(message) is not None
                       else item_keyword(plan.filters.keyword, message))
            plan.filters = fallback_plan.filters.model_copy(update={"keyword": keyword})
        elif fallback_plan.intent == "case_query" and due_window(message) is not None:
            # A missing LLM date constraint must not silently mean "all jobs".
            plan.intent = "case_query"
            plan.capability = None
            plan.filters = fallback_plan.filters.model_copy(update={"keyword": due_item_keyword(plan.filters.keyword, message)})
        elif fallback_plan.filters.count_requested and plan.intent not in {"case_query", "case_status"}:
            plan = fallback_plan
        elif fallback_plan.filters.keyword and not plan.filters.keyword and plan.intent in {"case_query", "case_status"}:
            plan.filters.keyword = fallback_plan.filters.keyword
        if screen_question:
            plan = AssistantPlan(intent="help", query=lookup_query)

        if decision_question(message) and plan.intent in {'case_query', 'case_status'}:
            plan.filters.waiting_for = 'decision'
            plan.filters.stage = plan.filters.status = None
            plan.filters.keyword = item_keyword(plan.filters.keyword, message)
        scope = personal_scope(message)
        if scope and plan.intent in {'case_query', 'case_status'}:
            plan.filters.task_scope = scope
        if plan.context_mode == 'refine' and plan.intent in {'case_query', 'case_status'} and not state.get('inherit'):
            previous = request.dialogue.filters if request.dialogue else None
            if previous is None and request.dialogue and request.dialogue.pending:
                previous = request.dialogue.pending.filters
            if previous is None:
                return {'plan': plan, 'clarification': '어떤 목록을 기준으로 좁힐까요? 품목이나 작업 단계를 먼저 알려주세요.'}
            merged = previous.model_copy(deep=True)
            changes = plan.filters.model_dump(exclude_none=True, exclude_defaults=True)
            changes.pop('offset', None)
            if 'waiting_for' in changes:
                merged.stage = merged.status = None
            elif 'status' in changes or 'stage' in changes:
                merged.waiting_for = None
            for key in plan.clear_filters:
                setattr(merged, key, None)
            plan.filters = merged.model_copy(update={**changes, 'offset': 0})

        # Never allow the model to widen the closed-record boundary implicitly.
        lowered = message.casefold()
        if plan.filters.exact_reference:
            plan.filters.include_closed = True
        elif plan.filters.include_closed and plan.context_mode != 'refine' and not any(
            word in lowered for word in ("완료", "취소", "반려", "종료", "전체")
        ):
            plan.filters.include_closed = False
        if plan.filters.limit > 10:
            plan.filters.limit = 10
        if plan.intent in {"case_query", "case_status"}:
            plan.filters.count_requested = plan.filters.count_requested or asks_for_count(message)
            if not plan.filters.exact_reference and due_window(message) is not None:
                plan.filters.due_within_days = due_window(message)
        from .graph import apply_context_filters, validate_plan
        plan = apply_context_filters(state, plan)
        plan = normalize_capability(plan)
        error = validate_plan(plan)
        if plan.feature_id:
            selected = next((f for f in getattr(self.feature_catalog, 'all', lambda: [])() if f.id == plan.feature_id), None)
            if selected is None:
                error = '어떤 화면의 기능을 찾으시나요? MR 목록, 협력사 선정, PO 관리 중에서 알려주세요.'
            elif plan.intent not in {'case_query', 'case_status'}:
                plan.query = selected.title
        return {"plan": plan, "clarification": error}

    def _read_node(self, state):
        plan = state["plan"]
        actor = actor_id(state["current_user"])
        freshness_attempted = False
        freshness_refreshed = False
        if plan.require_freshness and plan.filters.exact_reference:
            freshness_attempted = True
            try:
                freshness_refreshed = self.freshness.refresh_reference(
                    plan.filters.exact_reference,
                    actor=actor,
                )
            except Exception:
                LOGGER.exception("Assistant freshness adapter failed; using stored projection")

        records = []
        query_available = True
        if plan.intent in {"case_query", "case_status"}:
            try:
                # Permission scoping is repeated inside the query adapter; it
                # is never delegated to the prompt or trusted from the browser.
                records = self.procurement_query.query_cases(plan.filters, actor=actor)
            except Exception:
                LOGGER.exception("Assistant procurement projection query failed")
                query_available = False
        return {
            "records": records, "query_available": query_available,
            "freshness_attempted": freshness_attempted, "freshness_refreshed": freshness_refreshed,
        }

    def _guide_node(self, state):
        if state['plan'].intent == 'unsupported':
            return {'records': [], 'query_available': True, 'freshness_attempted': False, 'freshness_refreshed': False,
                'features': [], 'help_matches': []}
        query = state['plan'].query or state['request'].message
        selected = next((f for f in getattr(self.feature_catalog, 'all', lambda: [])() if f.id == state['plan'].feature_id), None)
        return {'records': [], 'query_available': True, 'freshness_attempted': False, 'freshness_refreshed': False,
            'features': [selected] if selected else self.feature_catalog.search(query, limit=3),
            'help_matches': self.help_knowledge.search(query, limit=3)}

    def _respond_node(self, state):
        request, plan = state["request"], state["plan"]
        message = request.message.strip()
        records = state["records"]
        query_available = state["query_available"]
        freshness_attempted = state["freshness_attempted"]
        freshness_refreshed = state["freshness_refreshed"]
        features = state.get('features', [])
        help_matches = state.get('help_matches', [])
        # The model may phrase help, but must never turn real records into
        # "none" or invent counts. Case answers are grounded by construction.
        query_executed = plan.intent in {"case_query", "case_status"}
        model_answer = None if not query_available or query_executed or plan.intent == 'unsupported' else self.model.compose(
            message=message,
            context=request.context,
            plan=plan,
            records=records,
            features=features,
            help_matches=help_matches,
        )
        if plan.intent == 'unsupported':
            # Do not turn an unsupported live-data request into a fabricated
            # answer merely because some vaguely related help was retrieved.
            model_answer = ModelAnswer(answer='현재 챗봇은 구매 작업의 목록·건수·단계 조회와 화면 사용법 안내를 지원합니다. 요청하신 데이터는 여기서 직접 조회할 수 없습니다. 관련 화면에서 확인하는 방법을 안내해 드릴까요?', followups=['현재 화면 사용법 알려줘', '내가 결정할 작업 보여줘'])
        if model_answer:
            answer = model_answer.answer
            followups = model_answer.followups[:3]
            source = "deterministic" if plan.intent == 'unsupported' else "model"
        else:
            fallback = (
                ModelAnswer(
                    answer="현재 구매 작업 저장소에 연결할 수 없습니다. 잠시 뒤 다시 조회해 주세요. 화면 사용법 안내는 계속 이용할 수 있습니다.",
                    followups=["RFQ 사용법 알려줘", "PO 승인 방법 알려줘"],
                )
                if not query_available
                else self._deterministic_answer(plan, records, features, help_matches)
            )
            answer = fallback.answer
            followups = fallback.followups
            source = "deterministic"

        # Navigation actions come from trusted catalog/record values, never
        # directly from free-form model output.
        # The fallback answer quotes the first help article: link that article's
        # screen too, rather than a different feature matched by a shared word.
        if query_executed:
            # Item searches must never navigate to an unrelated feature merely
            # because a catalog token (e.g. '작업') happened to overlap.
            actions = self._actions(records, [], [])
            if not actions:
                group = plan.filters.waiting_for
                target = ("po-manage" if group in {"po_approval", "supplier_confirmation", "delivery"}
                          else "vendor-select" if group == "quotation"
                          else "dashboard" if group in {"external", "approval"}
                          else "mr-list")
                actions = [AssistantAction(label="구매 작업 확인", target=target)]
        else:
            actions = self._actions(
                records, [] if plan.intent == "help" and help_matches and not plan.feature_id else features, help_matches,
            )
        has_more = getattr(records, "has_more", False)
        if query_executed and has_more:
            followups = ["다음 10건 보여줘", *followups[:2]]
        response = AssistantMessageResponse(
            answer=answer,
            intent=plan.intent,
            records=records,
            actions=actions,
            followups=followups,
            source=source,
            model=self.model.model_name if self.model.available else None,
            meta={
                "read_only": True,
                "record_count": len(records),
                "total_count": getattr(records, "total_count", None),
                "freshness_attempted": freshness_attempted,
                "freshness_refreshed": freshness_refreshed,
                "query_available": query_available,
                "query_executed": query_executed,
                "applied_filters": plan.filters.model_dump() if query_executed else None,
                "has_more": has_more,
                "graph": "read-only-assistant-v1",
                "capability": plan.capability,
            },
            dialogue=(DialogueContext(
                filters=plan.filters if query_executed else None,
                references=[record.reference for record in records[:10]],
                guide_query=(plan.query or message)[:300] if not query_executed else "",
                guide_target=actions[0].target if not query_executed and actions else None,
            ) if query_available else request.dialogue),
        )
        return {"response": response}

    @staticmethod
    def _deterministic_answer(plan, records, features, help_matches) -> ModelAnswer:
        if plan.intent in {"case_query", "case_status"}:
            total_count = getattr(records, "total_count", None)
            date_scope = ""
            scope_text = {'visible': '조회 가능한 담당 범위', 'assigned': '현재 계정에 배정된 작업', 'actionable': '현재 계정의 담당 범위·권한에서 직접 확인하거나 결정할 작업'}[plan.filters.task_scope]
            if plan.filters.due_within_days is not None:
                today = business_today()
                end = today + timedelta(days=plan.filters.due_within_days)
                date_scope = (f"한국 시간 기준 오늘({today.isoformat()})부터 {end.isoformat()}까지, "
                              f"{plan.filters.due_within_days}일 이내 납기인 작업을 납기순으로 조회했습니다. ")
            def display_record(record):
                due = f", 납기 {record.schedule_date}" if date_scope and record.schedule_date else ""
                return f"{record.reference}({record.stage_label}{due})"
            if total_count is not None and records:
                names = ', '.join(display_record(r) for r in records[:3])
                return ModelAnswer(
                    answer=(date_scope + f"{scope_text} 중 조건에 맞는 구매 작업은 총 {total_count}건입니다. "
                            f"{'완료·취소·반려를 포함한' if plan.filters.include_closed else '완료·취소·반려를 제외한'} 결과이며, "
                            f"{names}{' 등' if total_count > 3 else ''}이 있습니다. "
                            f"아래에 {len(records)}건을 표시합니다."),
                    followups=["승인 대기 중인 MR 보여줘", "완료된 MR 보여줘"],
                )
            if not records:
                group = plan.filters.waiting_for
                clarification = ("여기서 승인 대기는 구매 요청 검토와 PO 최종 승인을 뜻하며, RFQ 대상 선택이나 외부 회신 대기는 별도입니다. "
                                 if group == "approval" else "")
                return ModelAnswer(
                    answer=(date_scope + f"{scope_text} 중 조건에 맞는 구매 작업은 0건입니다. "
                            + clarification + ("품목명이나 조회 조건을 바꿔 다시 찾아볼 수 있습니다." if plan.filters.include_closed else "완료·취소 항목을 찾는 경우에는 해당 상태를 함께 말씀해 주세요.")),
                    followups=["승인 대기 중인 MR 보여줘", "MAT-MR 번호로 현재 단계 알려줘"],
                )
            if len(records) == 1:
                record = records[0]
                return ModelAnswer(
                    answer=(
                        date_scope + f"{display_record(record)}은(는) 현재 ‘{record.stage_label}’ 단계입니다. "
                        f"현재 상태는 ‘{record.status_label}’이며, 단계 안내는 “{record.next_action}”입니다. "
                        "아래 바로가기로 해당 업무 화면을 열 수 있습니다."
                    ),
                    followups=["이 단계에서 무엇을 확인해야 해?", "관련 화면으로 이동"],
                )
            return ModelAnswer(
                answer=(date_scope + f"조회 가능한 담당 범위에서 조건에 맞는 구매 요청 {len(records)}건을 표시합니다. "
                        f"{', '.join(display_record(r) for r in records[:3])}"
                        f"{' 등' if len(records) > 3 else ''}입니다. "
                        f"한 번에 최대 {plan.filters.limit}건까지 표시하며, 각 카드에서 현재 단계와 다음 행동을 확인할 수 있습니다."),
                followups=["승인 대기 항목만 보여줘", "납기가 가까운 항목 찾아줘"],
            )
        if plan.intent == "help" and help_matches:
            article = help_matches[0]
            return ModelAnswer(
                answer=f"{article.title}: {article.content}",
                followups=["관련 화면으로 이동", "다른 기능도 알려줘"],
            )
        if features:
            feature = features[0]
            steps = " → ".join(feature.steps[:4])
            suffix = f" 사용 순서는 {steps}입니다." if steps else ""
            return ModelAnswer(
                answer=f"{feature.title} 기능은 {feature.summary}{suffix} 제가 업무를 대신 실행하지는 않으며, 아래 버튼으로 해당 화면을 열어드릴 수 있습니다.",
                followups=["관련 화면으로 이동", "사용 시 주의사항 알려줘"],
            )
        return ModelAnswer(
            answer="구매 요청 상태 조회와 BiddingFlow 화면 사용법을 안내할 수 있습니다. MR 번호나 찾고 싶은 조건을 함께 적어 주세요.",
            followups=["승인 대기 중인 MR 보여줘", "RFQ는 어디서 보내?", "PO 승인 방법 알려줘"],
        )

    @staticmethod
    def _actions(records, features, help_matches) -> list[AssistantAction]:
        actions: list[AssistantAction] = []
        for record in records[:10]:
            actions.append(
                AssistantAction(
                    type="navigate_with_filters",
                    label=f"{record.reference} 열기",
                    target=record.target,
                    search_query=record.reference,
                    highlight_reference=record.reference,
                )
            )
        if not actions and features:
            feature = features[0]
            actions.append(
                AssistantAction(label=f"{feature.title} 화면 열기", target=feature.target)
            )
        if not actions and help_matches and help_matches[0].target:
            actions.append(
                AssistantAction(label="관련 화면 열기", target=help_matches[0].target)
            )
        return actions


@lru_cache(maxsize=1)
def get_assistant_service() -> AssistantService:
    package_root = Path(__file__).resolve().parent
    feature_path = _path_setting(
        "ASSISTANT_FEATURE_CATALOG_PATH",
        package_root / "data" / "features.json",
    )
    help_source_path = _path_setting(
        "ASSISTANT_HELP_SOURCE_PATH",
        package_root / "data" / "help_articles.json",
    )
    project_root = package_root.parent.parent
    help_db_path = _path_setting(
        "ASSISTANT_HELP_DB_PATH",
        project_root / ".runtime" / "assistant" / "help.sqlite3",
    )
    return AssistantService(
        feature_catalog=JsonFeatureCatalog(feature_path),
        help_knowledge=SQLiteHelpKnowledge(help_source_path, help_db_path),
        procurement_query=PostgresProcurementQuery(),
        freshness=ProjectionOnlyFreshness(),
        model=OpenAIResponsesAssistant(),
    )


def assistant_enabled() -> bool:
    return _truthy("ASSISTANT_ENABLED", "true")
