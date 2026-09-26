"""Public JSON contract. Defaults preserve the pre-policy purchasing behavior."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PurchasingRules(StrictModel):
    urgent_lead_days: int = Field(default=7, ge=0, le=90)
    bidding_amount: int = Field(default=20_000_000, ge=1, le=1_000_000_000_000)
    pattern_min_orders: int = Field(default=3, ge=3, le=100)
    irregular_cv: float = Field(default=0.5, gt=0, le=10, allow_inf_nan=False)
    cycle_overdue_multiplier: float = Field(default=1.5, ge=1, le=20, allow_inf_nan=False)
    inactive_months: int = Field(default=12, ge=1, le=120)
    min_competing_suppliers: int = Field(default=3, ge=1, le=20)
    supplier_refresh_years: int = Field(default=3, ge=1, le=20)
    quotation_priority: Literal["price_then_delivery", "delivery_then_price"] = "price_then_delivery"
    # 견적 종합평가 4항목 가중치(합계 100). 예전의 '가격·납기 60 / 규격 40'
    # 두 칸은 가격 비율 점수가 이상치 하나에 끌려가고 납기·협력사 평가이력이
    # 사실상 반영되지 않아 4항목으로 나눴다. 값이 없는 항목(평가이력이 없는
    # 신규 협력사, 규격 AI 평가 미완료, 회신 1건이라 비교 불가한 가격)은 그
    # 항목을 빼고 나머지 가중치를 다시 100%로 나눈다(quotation_ranker 참고).
    quotation_price_weight: float = Field(default=35.0, ge=0, le=100, allow_inf_nan=False)
    quotation_delivery_weight: float = Field(default=20.0, ge=0, le=100, allow_inf_nan=False)
    quotation_specification_weight: float = Field(default=30.0, ge=0, le=100, allow_inf_nan=False)
    quotation_scorecard_weight: float = Field(default=15.0, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_quotation_weights(cls, value):
        # DB에 저장된 예전 정책 스냅샷(케이스에 고정된 것 포함)은 2항목 키를
        # 갖고 있다. extra="forbid"라 그대로 두면 로드 자체가 실패하므로
        # 버리고 새 4항목 기본값을 쓴다 - 2항목을 4항목으로 의미 있게 나눌
        # 방법이 없다.
        if isinstance(value, dict):
            legacy = {"quotation_numeric_score_weight", "quotation_spec_score_weight"}
            if legacy & set(value):
                value = {key: item for key, item in value.items() if key not in legacy}
        return value

    @model_validator(mode="after")
    def quotation_weights_total_one_hundred(self):
        total = (
            self.quotation_price_weight
            + self.quotation_delivery_weight
            + self.quotation_specification_weight
            + self.quotation_scorecard_weight
        )
        if abs(total - 100.0) > 1e-6:
            raise ValueError("견적 평가 가중치(가격·납기·규격·평가이력)의 합계는 100%여야 합니다.")
        return self


class AIGuidance(StrictModel):
    # Empty means the original prompt. These fields cannot change response
    # schemas, permissions, email safety controls or human approval gates.
    item_specification: str = Field(default="", max_length=2000)
    substitute_selection: str = Field(default="", max_length=2000)


class CompanyPolicy(StrictModel):
    rules: PurchasingRules = Field(default_factory=PurchasingRules)
    guidance: AIGuidance = Field(default_factory=AIGuidance)
    # Legacy snapshots without this key retain the previous Tavily-only path.
    supplier_sources: list[Literal['tavily', 'narajangteo', 'db']] = Field(
        default_factory=lambda: ['tavily'], min_length=1, max_length=3)

    @field_validator('supplier_sources')
    @classmethod
    def unique_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('탐색 소스는 중복 선택할 수 없습니다.')
        return sorted(value)


class PublishPolicy(StrictModel):
    expected_version: int = Field(ge=1)
    policy: CompanyPolicy
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("policy", mode="before")
    @classmethod
    def require_complete_snapshot(cls, value):
        # A partial API payload must not silently reset unspecified settings.
        if isinstance(value, CompanyPolicy):
            return value
        if not isinstance(value, dict) or set(value) != {"rules", "guidance", "supplier_sources"}:
            raise ValueError("게시할 전체 정책을 전달해야 합니다.")
        for name, model in (("rules", PurchasingRules), ("guidance", AIGuidance)):
            if not isinstance(value[name], dict) or set(value[name]) != set(model.model_fields):
                raise ValueError(f"{name}의 모든 설정값을 전달해야 합니다.")
        return value
