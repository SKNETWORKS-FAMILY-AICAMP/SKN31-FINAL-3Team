"""GPT-5.6 Luna based semantic specification comparison for quotations."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from .quotation_models import QuotationReview, RFQRequirements


LOGGER = logging.getLogger(__name__)


class SpecItemAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quotation_item: str
    rfq_item: str
    compliant: bool
    score: float = Field(ge=0, le=100)
    reason: str
    missing_or_conflicting_specs: list[str] = Field(default_factory=list)


class QuotationSpecAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quotation_id: str
    compliant: bool
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    reason: str
    items: list[SpecItemAssessment]


class QuotationSpecAssessmentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[QuotationSpecAssessment]


INSTRUCTIONS = """
당신은 산업 구매 견적의 기술 규격 적합성을 판정하는 심사자입니다.
RFQ 요구사항과 각 Supplier Quotation의 품목 설명, 원문 설명, 구조화된 specifications만 비교하세요.
가격, 공급사 인지도, 납기, 과거 실적은 규격 점수에 절대 반영하지 마세요.

판정 원칙:
- 동의어, 약어, 단위 환산, 표기 순서 차이는 의미가 같으면 일치로 봅니다.
- 수치 허용오차가 명시되면 그것을 적용하고, 명시되지 않으면 임의로 허용오차를 만들지 않습니다.
- 상위 규격이 요구 규격을 완전히 포함한다는 기술적 근거가 있을 때만 적합으로 봅니다.
- 필수 규격이 확인되지 않거나 상충하면 compliant=false로 판정하고 무엇이 부족한지 적습니다.
- 추측하지 말고 제공된 근거만 사용합니다.
- score는 모든 필수 규격 충족도를 0~100으로 표현하며, compliant는 필수 규격을 모두 충족할 때만 true입니다.
- 각 quotation_id마다 정확히 하나의 assessment를 반환합니다.
""".strip()


def _int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


class LunaQuotationSpecEvaluator:
    """Compare quotation specifications with RFQ requirements using Luna."""

    def __init__(self, client: OpenAI | None = None):
        self.model_name = (
            os.getenv("QUOTATION_SPEC_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna"
        )
        effort = os.getenv("QUOTATION_SPEC_REASONING_EFFORT", "medium").strip().lower()
        self.reasoning_effort = effort if effort in {
            "none", "low", "medium", "high", "xhigh", "max"
        } else "medium"
        self.timeout = _int_setting("QUOTATION_SPEC_TIMEOUT_SECONDS", 60, 10, 180)
        self.max_output_tokens = _int_setting(
            "QUOTATION_SPEC_MAX_OUTPUT_TOKENS", 4000, 1000, 12000
        )
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None or bool(os.getenv("OPENAI_API_KEY", "").strip())

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(timeout=self.timeout)
        return self._client

    def evaluate(
        self,
        rfq: RFQRequirements,
        reviews: list[QuotationReview],
    ) -> dict[str, QuotationSpecAssessment] | None:
        """Return one semantic assessment per quotation, or ``None`` on failure."""

        quotations = [review.quotation for review in reviews if review.quotation is not None]
        if not quotations:
            return {}
        if not self.available:
            LOGGER.warning("OPENAI_API_KEY가 없어 Luna 규격 평가를 건너뜁니다.")
            return None

        payload: dict[str, Any] = {
            "rfq": rfq.model_dump(mode="json"),
            "quotations": [
                {
                    "quotation_id": quotation.quotation_id,
                    "supplier_name": quotation.supplier_name,
                    "items": [
                        {
                            "item_code": item.item_code,
                            "item_name": item.item_name,
                            "description": item.description,
                            "raw_description": item.raw_description,
                            "specifications": item.specifications,
                        }
                        for item in quotation.items
                    ],
                    "notes": quotation.notes,
                }
                for quotation in quotations
            ],
        }
        try:
            response = self._get_client().responses.parse(
                model=self.model_name,
                instructions=INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False),
                reasoning={"effort": self.reasoning_effort},
                max_output_tokens=self.max_output_tokens,
                text_format=QuotationSpecAssessmentBatch,
                store=False,
                timeout=self.timeout,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("Luna가 구조화된 규격 평가를 반환하지 않았습니다.")
            result = {row.quotation_id: row for row in parsed.assessments}
            expected_ids = {quotation.quotation_id for quotation in quotations}
            if set(result) != expected_ids:
                missing = sorted(expected_ids - set(result))
                unexpected = sorted(set(result) - expected_ids)
                raise ValueError(
                    f"Luna 규격 평가의 견적 ID가 일치하지 않습니다: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            return result
        except Exception:
            LOGGER.exception("Luna 견적 규격 평가에 실패했습니다.")
            return None
