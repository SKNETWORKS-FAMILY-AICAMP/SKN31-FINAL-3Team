"""7번 견적 처리 모듈들이 공유하는 데이터 모델과 JSON 입출력 도구."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """알 수 없는 키를 조용히 버리지 않는 공통 모델."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceKind(str, Enum):
    EXCEL = "excel"
    DOCX = "docx"
    PDF = "pdf"
    IMAGE = "image"
    EMAIL = "email"
    TEXT = "text"
    PORTAL = "portal"


class ReviewStatus(str, Enum):
    ACCEPTED = "accepted"
    REEXTRACT = "reextract"
    EXCLUDED = "excluded"
    HUMAN_REVIEW = "human_review"
    RFQ_REWRITE = "rfq_rewrite"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class QuotationSource(StrictModel):
    kind: SourceKind
    filename: str
    path: str | None = None
    message_id: str | None = None
    content_type: str | None = None


class QuotationItem(StrictModel):
    item_code: str | None = None
    item_name: str
    description: str | None = None
    quantity: Decimal = Field(gt=0)
    unit: str | None = None
    unit_price: Decimal = Field(ge=0)
    amount: Decimal = Field(ge=0)
    # ERPNext Supplier Quotation Item.expected_delivery_date
    expected_delivery_date: date | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    specifications: dict[str, str | int | float] = Field(default_factory=dict)
    raw_description: str | None = None


class Quotation(StrictModel):
    quotation_id: str
    rfq_name: str
    supplier_id: str | None = None
    supplier_name: str
    status: str = "received"
    business_registration_no: str | None = None
    # quotation_date is the date the supplier issued/sent the quotation;
    # valid_until is the quotation validity deadline.
    quotation_date: date | None = None
    valid_until: date | None = None
    currency: str
    subtotal: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    # ERPNext 회사 기준통화로 환산된 총액. 서로 다른 통화의 견적 비교에만 사용한다.
    base_total_amount: Decimal | None = Field(default=None, ge=0)
    items: list[QuotationItem] = Field(min_length=1)
    notes: str | None = None
    source: QuotationSource
    extraction_attempt: int = Field(default=1, ge=1, le=3)
    extraction_evidence: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class RFQItemRequirement(StrictModel):
    item_code: str | None = None
    item_name: str
    quantity: Decimal = Field(gt=0)
    required_delivery_date: date | None = None
    specifications: dict[str, str | int | float] = Field(default_factory=dict)
    numeric_tolerance_percent: Decimal = Field(default=Decimal("0"), ge=0)


class RFQRequirements(StrictModel):
    rfq_name: str
    # 과거 JSON 입력 호환용이다. ERPNext RFQ에는 통화 제약이 없으며 검토에도 사용하지 않는다.
    currency: str | None = None
    items: list[RFQItemRequirement] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class ReviewIssue(StrictModel):
    code: str
    severity: IssueSeverity
    field: str | None = None
    message: str
    evidence: str


class ItemCompliance(StrictModel):
    item_code: str | None = None
    item_name: str
    matched_rfq_item: str | None = None
    specification_compliant: bool
    quantity_compliant: bool
    evidence: list[str] = Field(default_factory=list)


class QuotationReview(StrictModel):
    quotation: Quotation | None = None
    quotation_id: str
    supplier_name: str | None = None
    source_kind: SourceKind | None = None
    status: ReviewStatus
    valid: bool
    specification_compliant: bool
    issues: list[ReviewIssue] = Field(default_factory=list)
    item_compliance: list[ItemCompliance] = Field(default_factory=list)
    rejection_evidence: list[str] = Field(default_factory=list)


class RankedQuotation(StrictModel):
    rank: int = Field(ge=1)
    quotation_id: str
    supplier_id: str | None = None
    supplier_name: str
    total_amount: Decimal
    comparison_amount: Decimal
    currency: str
    expected_delivery_date: date | None = None
    late_days: int | None = None
    tied: bool = False
    reason: str
    numeric_score: float | None = Field(default=None, ge=0, le=100)
    specification_score: float | None = Field(default=None, ge=0, le=100)
    overall_score: float | None = Field(default=None, ge=0, le=100)
    specification_reason: str | None = None
    specification_items: list[dict[str, Any]] = Field(default_factory=list)
    evaluation_source: str | None = None
    # 4항목 점수(0~100). 값이 없어 가중치에서 빠진 항목은 None이다.
    price_score: float | None = Field(default=None, ge=0, le=100)
    delivery_score: float | None = Field(default=None, ge=0, le=100)
    scorecard_score: float | None = Field(default=None, ge=0, le=100)
    scorecard_count: int = Field(default=0, ge=0)
    # 페널티 차감 전 가중합과 차감 내역.
    base_score: float | None = Field(default=None, ge=0, le=100)
    penalty_points: float = Field(default=0, ge=0)
    penalties: list[dict[str, Any]] = Field(default_factory=list)
    # 실제로 적용된(재정규화된) 항목별 가중치 0~1, 빠진 항목과 그 이유.
    applied_weights: dict[str, float] = Field(default_factory=dict)
    missing_factors: list[dict[str, str]] = Field(default_factory=list)
    # 수량 미달·유효기간 만료처럼 선정 전에 사람이 한 번 확인해야 하는 견적.
    requires_confirmation: bool = False
    warnings: list[str] = Field(default_factory=list)


class RankingResult(StrictModel):
    rfq_name: str
    requested_top_k: int
    recommended: list[RankedQuotation] = Field(default_factory=list)
    # 순위 대상이 아닌 견적. kind = "parse_failed"(견적서를 읽지 못함) 또는
    # "rfq_mismatch"(다른 RFQ 견적이 섞여 들어옴). 그 외 문제는 전부 제외가
    # 아니라 페널티로 순위에 남는다.
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    parse_failed: list[dict[str, Any]] = Field(default_factory=list)
    # 순위 대상이 된 유효 견적 수. 1이면 단독 응찰이다.
    competition_count: int = Field(default=0, ge=0)
    single_bid: bool = False


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(data: Any, path: str | Path | None = None) -> str:
    if isinstance(data, BaseModel):
        payload = data.model_dump(mode="json")
    elif isinstance(data, list):
        payload = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in data]
    else:
        payload = data
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(rendered + "\n", encoding="utf-8")
    return rendered
