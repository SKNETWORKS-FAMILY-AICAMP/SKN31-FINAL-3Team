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


def _rfq(quantity: str = "1") -> RFQRequirements:
    return RFQRequirements(
        rfq_name="RFQ-1",
        items=[RFQItemRequirement(
            item_code="ITEM-1",
            item_name="산업용 밸브",
            quantity=Decimal(quantity),
            specifications={"재질": "SUS316L"},
        )],
    )


def _with_issue(review: QuotationReview, code: str, message: str = "문제") -> QuotationReview:
    return review.model_copy(update={
        "issues": [*review.issues, ReviewIssue(
            code=code,
            severity=IssueSeverity.ERROR,
            field=None,
            message=message,
            evidence=f"{code} 근거",
        )],
    })


def test_combined_score_can_prefer_stronger_spec_fit_over_lowest_price() -> None:
    reviews = [
        _review("SQ-CHEAP", "SUP-CHEAP", "100"),
        _review("SQ-SPEC", "SUP-SPEC", "120", rule_spec_match=False),
    ]

    result = rank_quotations_with_spec_scores(
        reviews,
        _rfq(),
        {
            "SQ-CHEAP": _assessment("SQ-CHEAP", 70),
            "SQ-SPEC": _assessment("SQ-SPEC", 100),
        },
    )

    # 유효 견적 2건 -> 가격 가중치 절반. 납기 미기재 -> 둘 다 납기 0점.
    # 평가이력 없음 -> 그 항목 제외 후 재정규화.
    assert [row.quotation_id for row in result.recommended] == ["SQ-SPEC", "SQ-CHEAP"]
    assert result.recommended[0].price_score == 60.0
    assert result.recommended[0].overall_score == 60.0
    assert result.recommended[1].overall_score == 57.04
    assert result.recommended[0].evaluation_source == "gpt-5.6-luna"
    assert result.competition_count == 2
    assert result.single_bid is False


def test_luna_can_resolve_rule_only_spec_mismatch_without_bypassing_quantity_guard() -> None:
    review = _review("SQ-1", "SUP-1", "100", rule_spec_match=False)

    result = rank_quotations_with_spec_scores(
        [review],
        _rfq(),
        {"SQ-1": _assessment("SQ-1", 96)},
    )

    assert [row.quotation_id for row in result.recommended] == ["SQ-1"]
    assert result.excluded == []
    # 단독 응찰이면 가격 비교가 불가해 가격 항목을 뺀다.
    assert result.single_bid is True
    assert result.recommended[0].price_score is None
    assert {row["factor"] for row in result.recommended[0].missing_factors} >= {"price", "scorecard"}


def test_ai_noncompliant_spec_is_ranked_with_warning_not_excluded() -> None:
    noncompliant = _assessment("SQ-2", 40).model_copy(update={
        "compliant": False,
        "reason": "필수 규격 불일치",
    })
    result = rank_quotations_with_spec_scores(
        [
            _review("SQ-1", "SUP-1", "100"),
            _review("SQ-2", "SUP-2", "110"),
        ],
        _rfq(),
        {
            "SQ-1": _assessment("SQ-1", 96),
            "SQ-2": noncompliant,
        },
        evaluation_source="configured-spec-model",
    )

    assert result.excluded == []
    assert [row.quotation_id for row in result.recommended] == ["SQ-1", "SQ-2"]
    assert result.recommended[0].evaluation_source == "configured-spec-model"
    assert any("필수 규격 불일치" in warning for warning in result.recommended[1].warnings)


def test_delivery_score_is_capped_at_100(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker._delivery_metrics",
        lambda _review, _rfq: (date(2026, 9, 1), -10),
    )

    result = rank_quotations_with_spec_scores(
        [_review("SQ-1", "SUP-1", "100")],
        _rfq(),
        {"SQ-1": _assessment("SQ-1", 100)},
    )

    assert result.recommended[0].delivery_score == 100.0
    assert result.recommended[0].numeric_score == 100.0
    assert result.recommended[0].overall_score == 100.0


def test_only_parse_failures_and_foreign_rfq_quotes_leave_the_ranking() -> None:
    parse_failed = QuotationReview(
        quotation=None,
        quotation_id="SQ-BROKEN",
        supplier_name="SUP-BROKEN",
        status=ReviewStatus.HUMAN_REVIEW,
        valid=False,
        specification_compliant=False,
        issues=[ReviewIssue(
            code="SCHEMA_TYPE_ERROR",
            severity=IssueSeverity.ERROR,
            field="items",
            message="List should have at least 1 item",
            evidence="입력값=[]",
        )],
        rejection_evidence=["입력값=[]"],
    )
    foreign = _with_issue(_review("SQ-OTHER", "SUP-OTHER", "90"), "RFQ_MISMATCH")
    expired = _with_issue(_review("SQ-OLD", "SUP-OLD", "95"), "QUOTATION_EXPIRED")

    result = rank_quotations_with_spec_scores(
        [parse_failed, foreign, expired, _review("SQ-OK", "SUP-OK", "100")],
        _rfq(),
        {"SQ-OLD": _assessment("SQ-OLD", 90), "SQ-OK": _assessment("SQ-OK", 90)},
    )

    assert {row["kind"] for row in result.excluded} == {"parse_failed", "rfq_mismatch"}
    assert [row["quotation_id"] for row in result.parse_failed] == ["SQ-BROKEN"]
    ranked = {row.quotation_id: row for row in result.recommended}
    assert set(ranked) == {"SQ-OLD", "SQ-OK"}
    assert ranked["SQ-OLD"].penalty_points == 15.0
    assert ranked["SQ-OLD"].requires_confirmation is True
    assert result.competition_count == 2


def test_insufficient_quantity_is_a_penalty_not_an_exclusion() -> None:
    short = _with_issue(_review("SQ-SHORT", "SUP-SHORT", "50"), "INSUFFICIENT_QUANTITY")
    result = rank_quotations_with_spec_scores(
        [short, _review("SQ-FULL", "SUP-FULL", "100")],
        _rfq(),
        {"SQ-SHORT": _assessment("SQ-SHORT", 90), "SQ-FULL": _assessment("SQ-FULL", 90)},
    )

    ranked = {row.quotation_id: row for row in result.recommended}
    assert ranked["SQ-SHORT"].penalty_points == 20.0
    assert ranked["SQ-SHORT"].requires_confirmation is True
    assert ranked["SQ-SHORT"].overall_score == round(ranked["SQ-SHORT"].base_score - 20.0, 2)


def test_price_score_drops_to_zero_at_fifty_percent_over_lowest() -> None:
    reviews = [
        _review("SQ-A", "SUP-A", "100"),
        _review("SQ-B", "SUP-B", "125"),
        _review("SQ-C", "SUP-C", "150"),
    ]
    result = rank_quotations_with_spec_scores(
        reviews,
        _rfq(),
        {quotation_id: _assessment(quotation_id, 80) for quotation_id in ("SQ-A", "SQ-B", "SQ-C")},
    )

    ranked = {row.quotation_id: row for row in result.recommended}
    assert ranked["SQ-A"].price_score == 100.0
    assert ranked["SQ-B"].price_score == 50.0
    assert ranked["SQ-C"].price_score == 0.0
    # 유효 견적 3건 이상이면 가격 가중치를 온전히 반영한다.
    assert ranked["SQ-A"].applied_weights["price"] > ranked["SQ-A"].applied_weights["delivery"]


def test_scorecard_history_is_weighted_by_evaluation_count() -> None:
    reviews = [_review("SQ-A", "SUP-A", "100"), _review("SQ-B", "SUP-B", "100")]
    result = rank_quotations_with_spec_scores(
        reviews,
        _rfq(),
        {"SQ-A": _assessment("SQ-A", 80), "SQ-B": _assessment("SQ-B", 80)},
        supplier_scorecards={
            "SUP-A": {"weighted_score": 5.0, "evaluation_count": 3},
        },
    )

    ranked = {row.quotation_id: row for row in result.recommended}
    assert ranked["SQ-A"].scorecard_score == 100.0
    assert ranked["SQ-A"].scorecard_count == 3
    assert ranked["SQ-B"].scorecard_score is None
    assert any(row["factor"] == "scorecard" for row in ranked["SQ-B"].missing_factors)
    assert "scorecard" not in ranked["SQ-B"].applied_weights
    assert result.recommended[0].quotation_id == "SQ-A"


def test_legacy_two_weight_policy_snapshot_still_loads() -> None:
    from backend_logic2.policies.schema import CompanyPolicy

    policy = CompanyPolicy.model_validate({
        "rules": {"quotation_numeric_score_weight": 60.0, "quotation_spec_score_weight": 40.0},
    })

    assert policy.rules.quotation_price_weight == 35.0
    assert policy.rules.quotation_delivery_weight == 20.0
    assert policy.rules.quotation_specification_weight == 30.0
    assert policy.rules.quotation_scorecard_weight == 15.0
