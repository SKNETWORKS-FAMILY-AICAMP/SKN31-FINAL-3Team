"""Stable request/response contracts for the BiddingFlow assistant."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# The frontend also keeps this allowlist. Returning a closed set from the API
# prevents model-generated arbitrary URLs or script-like actions.
NavigationTarget = Literal[
    "dashboard",
    "item-register",
    "mr-list",
    "vendor-select",
    "po-manage",
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
    conversation: list[ConversationMessage] = Field(default_factory=list, max_length=8)


class CaseQueryFilters(BaseModel):
    exact_reference: str | None = Field(default=None, max_length=120)
    keyword: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=50)
    stage: str | None = Field(default=None, max_length=80)
    due_within_days: int | None = Field(default=None, ge=0, le=365)
    has_attachments: bool | None = None
    include_closed: bool = False
    limit: int = Field(default=10, ge=1, le=20)


class AssistantPlan(BaseModel):
    intent: Literal["feature_guide", "help", "case_query", "case_status", "general"]
    query: str = Field(default="", max_length=300)
    filters: CaseQueryFilters = Field(default_factory=CaseQueryFilters)
    require_freshness: bool = False


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


class AssistantCapabilities(BaseModel):
    enabled: bool
    read_only: bool = True
    model: str
    reasoning_effort: str
    supports: list[str]
