"""Stable request/response contracts for the BiddingFlow assistant."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


# The frontend also keeps this allowlist. Returning a closed set from the API
# prevents model-generated arbitrary URLs or script-like actions.
NavigationTarget = Literal[
    "dashboard",
    "item-register",
    "mr-list",
    "vendor-select",
    "po-manage",
    "company-policy",
    "ai-decision-log",
]


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=3000)


class AssistantContext(BaseModel):
    current_tab: NavigationTarget = "dashboard"
    title: str | None = Field(default=None, max_length=200)
    detail: str | None = Field(default=None, max_length=500)


class AssistantMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    context: AssistantContext = Field(default_factory=AssistantContext)
    conversation: list[ConversationMessage] = Field(default_factory=list, max_length=16)
    dialogue: DialogueContext | None = None
    session_id: UUID | None = None
    session_version: int | None = Field(default=None, ge=0)


class AssistantSessionCreate(BaseModel):
    id: UUID


class CaseQueryFilters(BaseModel):
    exact_reference: str | None = Field(default=None, max_length=120)
    keyword: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=50)
    stage: str | None = Field(default=None, max_length=80)
    waiting_for: Literal["external", "requester", "quotation", "supplier_confirmation", "delivery", "po_approval", "mr_review", "approval", "decision"] | None = None
    task_scope: Literal["visible", "assigned", "actionable"] = "visible"
    count_requested: bool = False
    due_within_days: int | None = Field(default=None, ge=0, le=365)
    has_attachments: bool | None = None
    include_closed: bool = False
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=1990)


class MemoryTurn(BaseModel):
    question: str = Field(max_length=3000)
    capability: str = Field(default="", max_length=50)
    filters: CaseQueryFilters | None = None
    guide: str = Field(default="", max_length=300)
    clarification: str = Field(default="", max_length=500)


class ConversationMemory(BaseModel):
    recent: list[MemoryTurn] = Field(default_factory=list, max_length=8)
    summary: list[str] = Field(default_factory=list, max_length=8)
    compacted_turns: int = Field(default=0, ge=0)


class PendingClarification(BaseModel):
    original_message: str = Field(max_length=3000)
    question: str = Field(max_length=500)
    choices: list[str] = Field(default_factory=list, max_length=3)

    filters: CaseQueryFilters = Field(default_factory=CaseQueryFilters)
    kind: Literal["approval_meaning", "general"] = "general"


class DialogueContext(BaseModel):
    """Untrusted browser context, never an authorization or cached case record."""

    filters: CaseQueryFilters | None = None
    references: list[str] = Field(default_factory=list, max_length=10)
    guide_query: str = Field(default="", max_length=300)
    guide_target: NavigationTarget | None = None
    memory: ConversationMemory = Field(default_factory=ConversationMemory)
    pending: PendingClarification | None = None


AssistantMessageRequest.model_rebuild()


class AssistantPlan(BaseModel):
    intent: Literal["feature_guide", "help", "case_query", "case_status", "general", "clarification", "unsupported"]
    query: str = Field(default="", max_length=300)
    filters: CaseQueryFilters = Field(default_factory=CaseQueryFilters)
    require_freshness: bool = False
    capability: Literal["search_cases", "count_cases", "case_detail", "my_tasks", "feature_guide", "usage_help", "clarify", "unsupported"] | None = None
    confidence: Literal["high", "medium", "low"] = "high"
    context_mode: Literal["new", "refine"] = "new"
    clarification_question: str = Field(default="", max_length=500)
    choices: list[str] = Field(default_factory=list, max_length=3)
    feature_id: str | None = Field(default=None, max_length=120)
    unhandled_conditions: list[str] = Field(default_factory=list, max_length=4)
    clear_filters: list[Literal['keyword', 'waiting_for', 'status', 'stage', 'due_within_days', 'has_attachments', 'exact_reference']] = Field(default_factory=list, max_length=7)


class CaseQueryRecords(list):
    """Authorized display records, with an exact count only after a full scan.

    Remains list-compatible with query ports and test adapters. Never treat the
    display limit as the total number of matching purchases.
    """
    def __init__(self, records, *, total_count: int | None = None, has_more: bool = False):
        super().__init__(records)
        self.total_count = total_count
        self.has_more = has_more


class AssistantAction(BaseModel):
    type: Literal["navigate", "navigate_with_filters"] = "navigate"
    label: str
    target: NavigationTarget
    search_query: str | None = None
    highlight_reference: str | None = None


class AssistantRecord(BaseModel):
    case_id: str
    reference: str
    item_name: str
    stage: str
    stage_label: str
    status: str
    status_label: str
    schedule_date: str | None = None
    waiting_on: str
    next_action: str
    updated_at: str | None = None
    target: NavigationTarget


class FeatureMatch(BaseModel):
    id: str
    title: str
    summary: str
    target: NavigationTarget
    keywords: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)


class HelpMatch(BaseModel):
    id: str
    title: str
    content: str
    target: NavigationTarget | None = None
    score: float = 0.0


class ModelAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)
    followups: list[str] = Field(default_factory=list, max_length=3)


class AssistantMessageResponse(BaseModel):
    """Render contract: prose plus server-approved, read-only UI affordances."""

    answer: str
    intent: str
    records: list[AssistantRecord] = Field(default_factory=list)
    actions: list[AssistantAction] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    source: Literal["model", "deterministic"] = "deterministic"
    model: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    dialogue: DialogueContext | None = None


class AssistantCapabilities(BaseModel):
    enabled: bool
    read_only: bool = True
    model: str
    reasoning_effort: str
    supports: list[str]
