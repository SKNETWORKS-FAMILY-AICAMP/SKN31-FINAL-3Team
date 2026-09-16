"""Public JSON contract. Defaults preserve the pre-policy purchasing behavior."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
