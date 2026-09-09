"""GPT-5.6 Luna adapter using the OpenAI Responses API."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from ..models import (
    AssistantContext,
    AssistantPlan,
    AssistantRecord,
    FeatureMatch,
    HelpMatch,
    ModelAnswer,
)
from ..prompting import COMPOSER_INSTRUCTIONS, PLANNER_INSTRUCTIONS


LOGGER = logging.getLogger(__name__)


def _int_setting(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


class OpenAIResponsesAssistant:
    """Use structured outputs for routing/composition, without mutation tools."""

    def __init__(self, client: OpenAI | None = None):
        self._model = os.getenv("ASSISTANT_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
        effort = os.getenv("ASSISTANT_REASONING_EFFORT", "medium").strip().lower()
        self._reasoning_effort = effort if effort in {
            "none", "low", "medium", "high", "xhigh", "max"
        } else "medium"
        self._timeout = _int_setting(
            "ASSISTANT_TIMEOUT_SECONDS", 25, minimum=5, maximum=120
        )
        self._max_output_tokens = _int_setting(
            "ASSISTANT_MAX_OUTPUT_TOKENS", 1200, minimum=500, maximum=4000
        )
        self._client = client
        self._api_key_present = bool(os.getenv("OPENAI_API_KEY", "").strip())

    @property
    def available(self) -> bool:
        return self._client is not None or self._api_key_present

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(timeout=self._timeout)
        return self._client

    def plan(
        self,
        *,
        message: str,
        context: AssistantContext,
        recent_conversation: list[dict[str, str]],
        feature_candidates: list[FeatureMatch],
        help_candidates: list[HelpMatch],
    ) -> AssistantPlan | None:
        if not self.available:
            return None
        payload = {
            "message": message,
            "current_screen": context.model_dump(),
            "recent_conversation": recent_conversation[-6:],
            "feature_candidates": [entry.model_dump() for entry in feature_candidates[:5]],
            "help_candidates": [
                {"id": entry.id, "title": entry.title, "target": entry.target}
                for entry in help_candidates[:4]
            ],
        }
        try:
            # The SDK validates output against AssistantPlan. store=False keeps
            # operational questions out of durable Responses history.
            response = self._get_client().responses.parse(
                model=self._model,
                instructions=PLANNER_INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False),
                reasoning={"effort": self._reasoning_effort},
                max_output_tokens=self._max_output_tokens,
                text_format=AssistantPlan,
                store=False,
                timeout=self._timeout,
            )
            return response.output_parsed
        except Exception:
            LOGGER.exception("Assistant planner model call failed; using deterministic routing")
            return None

    def compose(
        self,
        *,
        message: str,
        context: AssistantContext,
        plan: AssistantPlan,
        records: list[AssistantRecord],
        features: list[FeatureMatch],
        help_matches: list[HelpMatch],
    ) -> ModelAnswer | None:
        if not self.available:
            return None
        payload: dict[str, Any] = {
            "user_message": message,
            "current_screen": context.model_dump(),
            "plan": plan.model_dump(),
            "case_records": [entry.model_dump() for entry in records[:10]],
            "feature_guides": [entry.model_dump() for entry in features[:3]],
            "help_articles": [
                {
                    "title": entry.title,
                    "content": entry.content[:1800],
                    "target": entry.target,
                }
                for entry in help_matches[:3]
            ],
        }
        try:
            # Only authorized records and reviewed help text reach this call.
            # AssistantService creates the final navigation actions afterward.
            response = self._get_client().responses.parse(
                model=self._model,
                instructions=COMPOSER_INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False),
                reasoning={"effort": self._reasoning_effort},
                max_output_tokens=self._max_output_tokens,
                text_format=ModelAnswer,
                store=False,
                timeout=self._timeout,
            )
            return response.output_parsed
        except Exception:
            LOGGER.exception("Assistant answer model call failed; using deterministic response")
            return None
