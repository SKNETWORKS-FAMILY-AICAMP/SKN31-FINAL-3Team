"""Ports that keep the assistant independent from ERP and persistence details."""

from __future__ import annotations

from typing import Any, Protocol

from .models import (
    AssistantContext,
    AssistantPlan,
    AssistantRecord,
    CaseQueryFilters,
    FeatureMatch,
    HelpMatch,
    ModelAnswer,
)


class FeatureCatalogPort(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[FeatureMatch]: ...


class HelpKnowledgePort(Protocol):
    def search(self, query: str, *, limit: int = 4) -> list[HelpMatch]: ...


class ProcurementQueryPort(Protocol):
    def query_cases(
        self,
        filters: CaseQueryFilters,
        *,
        actor: str,
    ) -> list[AssistantRecord]: ...


class ProcurementFreshnessPort(Protocol):
    def refresh_reference(self, reference: str, *, actor: str) -> bool: ...


class AssistantModelPort(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def model_name(self) -> str: ...

    def plan(
        self,
        *,
        message: str,
        context: AssistantContext,
        recent_conversation: list[dict[str, str]],
        feature_candidates: list[FeatureMatch],
        help_candidates: list[HelpMatch],
    ) -> AssistantPlan | None: ...

    def compose(
        self,
        *,
        message: str,
        context: AssistantContext,
        plan: AssistantPlan,
        records: list[AssistantRecord],
        features: list[FeatureMatch],
        help_matches: list[HelpMatch],
    ) -> ModelAnswer | None: ...
