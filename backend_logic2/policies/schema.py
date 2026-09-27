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

    # --- 자동 진행 ---
    # 사람이 멈추는 곳을 줄이되, 조건에 하나라도 걸리면 그 자리에서 멈춰
    # 담당자를 부른다. 판단이 애매하면 통과가 아니라 정지다.
    #
    # off    : 지금까지처럼 모든 지점에서 사람을 기다린다. **기본값이다.**
    #          진행 중인 케이스에 고정된 옛 정책에는 이 키가 없어 자동으로
    #          off가 된다 - 운영 중 서비스가 갑자기 움직이지 않는다.
    # shadow : 조건을 평가해 "이렇게 진행했을 것"을 기록만 남기고 멈춘다.
    #          임계값을 실제 데이터로 검증하는 단계.
    # on     : 조건을 통과하면 사람 없이 진행한다.
    automation_mode: Literal["off", "shadow", "on"] = "off"
    # 단계별 스위치. automation_mode가 off이면 둘 다 의미가 없다.
    auto_rfq_dispatch: bool = True
    auto_final_selection: bool = True

    # RFQ를 사람 확인 없이 보내려면 기존 협력사가 이만큼은 있어야 한다.
    # ⚠️ min_competing_suppliers("몇 곳을 찾아 초대할까")와는 다른 질문이라
    # 따로 둔다. 그 값은 신규 탐색 여부를 정하고, 이 값은 "사람 확인을
    # 건너뛸 만큼 충분한가"를 정한다. 같은 값을 쓰면 한쪽을 조정할 때
    # 다른 쪽이 조용히 따라 움직인다.
    auto_rfq_min_existing_suppliers: int = Field(default=3, ge=1, le=20)
    # 자동 발송 시 견적 마감일: 발송 시각 + 이 일수의 18:00(KST).
    # 납기요청일이 있으면 min(이 값, 납기까지 남은 일수)를 쓴다.
    auto_rfq_deadline_days: int = Field(default=5, ge=1, le=60)

    # 자동 선정에 필요한 최소 경쟁 견적 수. 최솟값이 2라서 어떤 설정으로도
    # 단독 응찰은 자동 선정되지 않는다.
    auto_selection_min_quotations: int = Field(default=2, ge=2, le=20)
    # 1순위와 2순위의 종합점수 차이가 이보다 작으면 박빙으로 보고 사람에게.
    auto_selection_score_gap: float = Field(default=10.0, ge=0, le=100, allow_inf_nan=False)
    # 선정 금액이 이 값을 넘으면 금액만으로 사람 확인 대상이 된다.
    auto_selection_max_amount: int = Field(default=50_000_000, ge=1, le=1_000_000_000_000)
    # 1순위에게 이 기간 안의 확정 발주 이력이 없으면 "처음 거래하는 협력사"로
    # 보고 사람을 부른다. supplier_refresh_years(공급사 풀 갱신 주기)와는
    # 다른 질문이라 따로 둔다.
    auto_known_supplier_years: int = Field(default=3, ge=1, le=20)

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
