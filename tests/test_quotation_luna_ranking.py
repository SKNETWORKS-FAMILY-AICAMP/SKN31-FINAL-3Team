from datetime import date
from decimal import Decimal

from backend_logic2.nodes.quotation.quotation_filter.quotation_models import (
    IssueSeverity,
    ItemCompliance,
    Quotation,
    QuotationItem,
    QuotationReview,
    QuotationSource,
    RFQItemRequirement,
    RFQRequirements,
    ReviewIssue,
    ReviewStatus,
    SourceKind,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
    rank_quotations_with_spec_scores,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_spec_evaluator import (
    QuotationSpecAssessment,
    SpecItemAssessment,
)


def _review(
    quotation_id: str,
    supplier: str,
    amount: str,
    *,
    rule_spec_match: bool = True,
) -> QuotationReview:
    quotation = Quotation(
        quotation_id=quotation_id,
        rfq_name="RFQ-1",
        supplier_id=supplier,
        supplier_name=supplier,
        currency="KRW",
        subtotal=Decimal(amount),
        tax_amount=Decimal("0"),
        total_amount=Decimal(amount),
        items=[QuotationItem(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            quantity=Decimal("1"),
            unit_price=Decimal(amount),
            amount=Decimal(amount),
            specifications={"재질": "SUS316"},
        )],
        source=QuotationSource(kind=SourceKind.TEXT, filename=f"{quotation_id}.txt"),
    )
    issues = [] if rule_spec_match else [ReviewIssue(
        code="SPECIFICATION_MISMATCH",
        severity=IssueSeverity.ERROR,
        field="items.0.specifications.재질",
        message="규칙 기반 문자열 비교 불일치",
        evidence="RFQ=SUS316L, 견적=SUS 316-L",
    )]
    return QuotationReview(
        quotation=quotation,
        quotation_id=quotation_id,
        supplier_name=supplier,
        source_kind=SourceKind.TEXT,
        status=ReviewStatus.ACCEPTED if rule_spec_match else ReviewStatus.EXCLUDED,
        valid=rule_spec_match,
        specification_compliant=rule_spec_match,
        issues=issues,
        item_compliance=[ItemCompliance(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            matched_rfq_item="ITEM-1",
            specification_compliant=rule_spec_match,
            quantity_compliant=True,
        )],
        rejection_evidence=[issue.evidence for issue in issues],
    )


def _assessment(quotation_id: str, score: float) -> QuotationSpecAssessment:
    return QuotationSpecAssessment(
        quotation_id=quotation_id,
        compliant=True,
        score=score,
        confidence=0.95,
        reason="표기 차이를 고려하면 모든 필수 규격을 충족합니다.",
        items=[SpecItemAssessment(
            quotation_item="산업용 밸브",
            rfq_item="산업용 밸브",
            compliant=True,
            score=score,
            reason="재질 규격 일치",
        )],
    )


def test_combined_score_can_prefer_stronger_spec_fit_over_lowest_price() -> None:
    rfq = RFQRequirements(
        rfq_name="RFQ-1",
        items=[RFQItemRequirement(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            quantity=Decimal("1"),
            specifications={"재질": "SUS316L"},
        )],
    )
    reviews = [
        _review("SQ-CHEAP", "SUP-CHEAP", "100"),
        _review("SQ-SPEC", "SUP-SPEC", "120", rule_spec_match=False),
    ]

    result = rank_quotations_with_spec_scores(
        reviews,
        rfq,
        {
            "SQ-CHEAP": _assessment("SQ-CHEAP", 70),
            "SQ-SPEC": _assessment("SQ-SPEC", 100),
        },
        numeric_weight=0.6,
        specification_weight=0.4,
    )

    assert [row.quotation_id for row in result.recommended] == ["SQ-SPEC", "SQ-CHEAP"]
    assert result.recommended[0].overall_score == 85.0
    assert result.recommended[0].evaluation_source == "gpt-5.6-luna"


def test_luna_can_resolve_rule_only_spec_mismatch_without_bypassing_quantity_guard() -> None:
    rfq = RFQRequirements(
        rfq_name="RFQ-1",
        items=[RFQItemRequirement(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            quantity=Decimal("1"),
        )],
    )
    review = _review("SQ-1", "SUP-1", "100", rule_spec_match=False)

    result = rank_quotations_with_spec_scores(
        [review],
        rfq,
        {"SQ-1": _assessment("SQ-1", 96)},
    )

    assert [row.quotation_id for row in result.recommended] == ["SQ-1"]
    assert result.excluded == []


def test_evaluation_source_uses_configured_model_name() -> None:
    rfq = RFQRequirements(
        rfq_name="RFQ-1",
        items=[RFQItemRequirement(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            quantity=Decimal("1"),
        )],
    )
    excluded_assessment = _assessment("SQ-2", 40).model_copy(update={
        "compliant": False,
        "reason": "필수 규격 불일치",
    })
    result = rank_quotations_with_spec_scores(
        [
            _review("SQ-1", "SUP-1", "100"),
            _review("SQ-2", "SUP-2", "110"),
        ],
        rfq,
        {
            "SQ-1": _assessment("SQ-1", 96),
            "SQ-2": excluded_assessment,
        },
        evaluation_source="configured-spec-model",
    )

    assert result.recommended[0].evaluation_source == "configured-spec-model"
    assert result.excluded[0]["evaluation_source"] == "configured-spec-model"


def test_delivery_score_is_capped_at_100(monkeypatch) -> None:
    rfq = RFQRequirements(
        rfq_name="RFQ-1",
        items=[RFQItemRequirement(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            quantity=Decimal("1"),
        )],
    )
    monkeypatch.setattr(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker._delivery_metrics",
        lambda _review, _rfq: (date(2026, 9, 1), -10),
    )

    result = rank_quotations_with_spec_scores(
        [_review("SQ-1", "SUP-1", "100")],
        rfq,
        {"SQ-1": _assessment("SQ-1", 100)},
    )

    assert result.recommended[0].numeric_score == 100.0
    assert result.recommended[0].overall_score == 100.0
