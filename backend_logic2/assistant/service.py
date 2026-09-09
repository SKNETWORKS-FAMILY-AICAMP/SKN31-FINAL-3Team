"""Application service for read-only feature guidance and procurement lookup."""

from __future__ import annotations

import os
import re
import logging
from functools import lru_cache
from pathlib import Path
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
)
from .ports import (
    AssistantModelPort,
    FeatureCatalogPort,
    HelpKnowledgePort,
    ProcurementFreshnessPort,
    ProcurementQueryPort,
)


MR_PATTERN = re.compile(r"\bMAT-MR-[0-9]{4}-[0-9]+\b", re.IGNORECASE)
ACTION_WORDS = ("시작해", "승인해", "반려해", "발송해", "보내줘", "삭제해", "진행해")
CASE_WORDS = ("mr", "구매 요청", "승인 대기", "납기", "견적 회신", "진행 중", "반려")
HELP_WORDS = ("왜", "어떻게", "오류", "안 돼", "안돼", "실패", "설명", "사용법")
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
    if any(word in lowered for word in ACTION_WORDS):
        return AssistantPlan(intent="feature_guide", query=message)
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

    def answer(
        self,
        request: AssistantMessageRequest,
        *,
        current_user: dict[str, Any],
    ) -> AssistantMessageResponse:
        message = request.message.strip()
        # Retrieve a small candidate set before model routing. This keeps the
        # prompt compact and provides a deterministic fallback if Luna is down.
        feature_candidates = self.feature_catalog.search(message, limit=5)
        help_candidates = self.help_knowledge.search(message, limit=4)
        recent = [entry.model_dump() for entry in request.conversation[-6:]]
        # Luna only produces a validated query plan here. It never receives a
        # database connection or a callable purchase-mutation tool.
        plan = self.model.plan(
            message=message,
            context=request.context,
            recent_conversation=recent,
            feature_candidates=feature_candidates,
            help_candidates=help_candidates,
        ) or _heuristic_plan(message)

        # Never allow the model to widen the closed-record boundary implicitly.
        lowered = message.casefold()
        if plan.filters.exact_reference:
            plan.filters.include_closed = True
        elif plan.filters.include_closed and not any(
            word in lowered for word in ("완료", "취소", "반려", "종료", "전체")
        ):
            plan.filters.include_closed = False
        if plan.filters.limit > 10:
            plan.filters.limit = 10

        actor = actor_id(current_user)
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

        features = self.feature_catalog.search(plan.query or message, limit=3)
        help_matches = self.help_knowledge.search(plan.query or message, limit=3)
        model_answer = None if not query_available else self.model.compose(
            message=message,
            context=request.context,
            plan=plan,
            records=records,
            features=features,
            help_matches=help_matches,
        )
        if model_answer:
            answer = model_answer.answer
            followups = model_answer.followups[:3]
            source = "model"
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
        actions = self._actions(records, features, help_matches)
        return AssistantMessageResponse(
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
                "freshness_attempted": freshness_attempted,
                "freshness_refreshed": freshness_refreshed,
                "query_available": query_available,
            },
        )

    @staticmethod
    def _deterministic_answer(plan, records, features, help_matches) -> ModelAnswer:
        if plan.intent in {"case_query", "case_status"}:
            if not records:
                return ModelAnswer(
                    answer="조회할 수 있는 담당 범위에서 조건에 맞는 구매 요청을 찾지 못했습니다. 완료·취소 항목을 찾는 경우에는 해당 상태를 함께 말씀해 주세요.",
                    followups=["승인 대기 중인 MR 보여줘", "MAT-MR 번호로 현재 단계 알려줘"],
                )
            if len(records) == 1:
                record = records[0]
                return ModelAnswer(
                    answer=(
                        f"{record.reference}은(는) 현재 ‘{record.stage_label}’ 단계입니다. "
                        f"지금은 {record.waiting_on}의 처리를 기다리고 있으며, 다음 행동은 “{record.next_action}”입니다. "
                        "아래 바로가기로 해당 업무 화면을 열 수 있습니다."
                    ),
                    followups=["이 단계에서 무엇을 확인해야 해?", "관련 화면으로 이동"],
                )
            return ModelAnswer(
                answer=f"조건에 맞는 구매 요청 {len(records)}건을 찾았습니다. 각 카드에서 현재 단계와 다음 행동을 확인하고 바로 이동할 수 있습니다.",
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
        for record in records[:5]:
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
