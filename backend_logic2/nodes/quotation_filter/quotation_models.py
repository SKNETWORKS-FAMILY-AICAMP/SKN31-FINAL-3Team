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
    delivery_date: date | None = None
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
    # Project data contract: quotation_date is the supplier-promised delivery
    # date; valid_until is the quotation validity deadline.
    quotation_date: date | None = None
    valid_until: date | None = None
    currency: str = "KRW"
    subtotal: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
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
    currency: str = "KRW"
    items: list[RFQItemRequirement] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


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
    currency: str
    delivery_date: date | None = None
    late_days: int | None = None
    tied: bool = False
    reason: str


class RankingResult(StrictModel):
    rfq_name: str
    requested_top_k: int
    recommended: list[RankedQuotation] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)


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
