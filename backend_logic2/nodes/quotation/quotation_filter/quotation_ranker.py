"""규칙 기반 수치 점수와 Qwen 규격 점수를 결합해 견적 순위를 계산한다.

"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

try:
    from .quotation_models import (
        QuotationReview,
        RankedQuotation,
        RankingResult,
        RFQRequirements,
        IssueSeverity,
        ReviewStatus,
        dump_json,
        load_json,
    )
    from .get_supplier_quotations import (
        get_quotations_for_rfq,
        get_quotations_for_rfqs,
        get_reviewable_quotations,
        get_reviewable_quotations_for_rfqs,
    )
    from .quotation_reviewer import load_rfq_requirements, match_requirement, review_quotation
except ImportError:
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import (
        QuotationReview,
        RankedQuotation,
        RankingResult,
        RFQRequirements,
        IssueSeverity,
        ReviewStatus,
        dump_json,
        load_json,
    )
    from backend_logic2.nodes.quotation.quotation_filter.get_supplier_quotations import (
        get_quotations_for_rfq,
        get_quotations_for_rfqs,
        get_reviewable_quotations,
        get_reviewable_quotations_for_rfqs,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import (
        load_rfq_requirements,
        match_requirement,
        review_quotation,
    )

from backend_logic2.repositories.deliveries import get_supplier_scorecard_history
from procurement_db import ProcurementDatabaseConfigurationError
try:
    from .quotation_spec_evaluator import (
        QuotationSpecEvaluator,
        QuotationSpecAssessment,
        build_quotation_spec_evaluator,
        specification_evaluation_fingerprint,
    )
except ImportError:
    from backend_logic2.nodes.quotation.quotation_filter.quotation_spec_evaluator import (
        QuotationSpecEvaluator,
        QuotationSpecAssessment,
        build_quotation_spec_evaluator,
        specification_evaluation_fingerprint,
    )


LOGGER = logging.getLogger(__name__)

AI_SPEC_ISSUE_CODES = frozenset({"MISSING_SPECIFICATION", "SPECIFICATION_MISMATCH"})
DEFAULT_NUMERIC_SCORE_WEIGHT = 0.6
DEFAULT_SPECIFICATION_SCORE_WEIGHT = 0.4
# Direct/library callers created before provider selection relied on this
# label. The production evaluate_quotations path always supplies the actual
# evaluator.model_name (Qwen by default).
DEFAULT_SPEC_EVALUATION_SOURCE = "gpt-5.6-luna"


def _weight_setting(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(0.0, value)


FACTOR_KEYS = ("price", "delivery", "specification", "scorecard")
FACTOR_LABELS = {
    "price": "가격",
    "delivery": "납기",
    "specification": "규격",
    "scorecard": "협력사 평가이력",
}
DEFAULT_FACTOR_WEIGHTS = {
    "price": 35.0,
    "delivery": 20.0,
    "specification": 30.0,
    "scorecard": 15.0,
}
# 가격: 최저가 100점, 최저가 대비 +50% 이상이면 0점, 그 사이는 선형.
# 예전의 '최저가/금액 x 100' 비율 방식은 극단적으로 싼 견적 하나가 있으면
# 나머지가 전부 0점 근처로 뭉개져 가격 하나가 순위를 결정했다.
PRICE_ZERO_SCORE_EXCESS = 0.5
# 납기: 요구일 이내 100점, 초과 하루당 10점 감점(10일 초과면 0점).
DELIVERY_POINTS_PER_LATE_DAY = 10.0
# 가격 비교 신뢰도: 유효 견적이 1건이면 비교할 대상이 없어 가격 항목을 빼고,
# 2건이면 절반만 반영한다. 3건 이상이면 온전히 반영.
PRICE_COMPETITION_FACTOR = {1: 0.0, 2: 0.5}
# 평가이력 신뢰도: 최근 평가 1건이면 절반, 2건이면 3/4, 3건 이상이면 온전히.
SCORECARD_COUNT_FACTOR = {1: 0.5, 2: 0.75}

# 제외가 아니라 총점에서 차감하는 페널티. requires_confirmation이면 선정 전에
# 사람이 한 번 더 확인하도록 화면에 표시한다(발주 사고 방지).
PENALTY_RULES: tuple[tuple[str, frozenset[str], float, str, bool], ...] = (
    ("INSUFFICIENT_QUANTITY", frozenset({"INSUFFICIENT_QUANTITY"}), 20.0, "요청 수량 미달", True),
    ("RFQ_ITEM_NOT_FOUND", frozenset({"RFQ_ITEM_NOT_FOUND"}), 20.0, "RFQ 품목과 연결되지 않는 품목 포함", True),
    ("QUOTATION_EXPIRED", frozenset({"QUOTATION_EXPIRED"}), 15.0, "견적 유효기간 만료", True),
    (
        "AMOUNT_INCONSISTENT",
        frozenset({"ITEM_AMOUNT_MISMATCH", "SUBTOTAL_MISMATCH", "TOTAL_MISMATCH", "UNUSUAL_VAT"}),
        10.0,
        "금액 계산 불일치(원본 확인 권장)",
        False,
    ),
    ("INVALID_BUSINESS_NUMBER", frozenset({"INVALID_BUSINESS_NUMBER"}), 5.0, "사업자등록번호 형식 오류", False),
)
# 점수 항목이 대신 반영하는 이슈 - 페널티를 따로 주지 않는다.
FACTOR_HANDLED_ISSUE_CODES = frozenset({
    "MISSING_DELIVERY_DATE",     # 납기 0점
    "MISSING_SPECIFICATION",     # 규격은 AI 점수
    "SPECIFICATION_MISMATCH",    # 규격은 AI 점수
    "RFQ_SCHEMA_ERROR",          # 우리 쪽 RFQ 문제 - 협력사 불이익 아님
})
# 순위 대상 자체가 아닌 경우(다른 RFQ 견적이 섞임).
NOT_A_CANDIDATE_ISSUE_CODES = frozenset({"RFQ_MISMATCH"})


def quotation_factor_weights(
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """가격·납기·규격·평가이력 가중치를 합계 1로 정규화해 돌려준다.

    워크플로 안에서는 케이스에 고정된 회사 정책을, 직접 호출(CLI/테스트)에서는
    인자 또는 기본값(35/20/30/15)을 쓴다.
    """
    if weights is None:
        from backend_logic2.policies.runtime import scoped_policy

        policy = scoped_policy()
        if policy is not None:
            rules = policy.rules
            weights = {
                "price": rules.quotation_price_weight,
                "delivery": rules.quotation_delivery_weight,
                "specification": rules.quotation_specification_weight,
                "scorecard": rules.quotation_scorecard_weight,
            }
        else:
            weights = dict(DEFAULT_FACTOR_WEIGHTS)
    resolved = {key: float(weights.get(key, 0.0)) for key in FACTOR_KEYS}
    if any(value < 0 for value in resolved.values()) or sum(resolved.values()) <= 0:
        raise ValueError("견적 평가 가중치 중 하나 이상은 양수여야 합니다.")
    total = sum(resolved.values())
    return {key: value / total for key, value in resolved.items()}


def _delivery_metrics(
    review: QuotationReview,
    rfq: RFQRequirements,
) -> tuple[date | None, int | None]:
    """전체 최종 납기와 품목별 요구일 대비 최대 지연일을 계산한다."""
    if not review.quotation:
        return None, None
    dates: list[date] = []
    item_late_days: list[int] = []
    for item in review.quotation.items:
        delivery = item.expected_delivery_date
        if (
            delivery is None
            and review.quotation.quotation_date
            and item.lead_time_days is not None
        ):
            delivery = review.quotation.quotation_date + timedelta(days=item.lead_time_days)
        if delivery:
            dates.append(delivery)
            required = match_requirement(item, rfq)
            if required and required.required_delivery_date:
                item_late_days.append(
                    (delivery - required.required_delivery_date).days
                )
    latest_delivery = max(dates) if dates else None
    late_days = max(0, max(item_late_days)) if item_late_days else None
    return latest_delivery, late_days


def classify_review(review: QuotationReview) -> dict[str, Any]:
    """견적 하나가 순위에서 어떤 대우를 받는지 판정한다.

    - kind="parse_failed": 견적 데이터를 스키마로 읽지 못함(review.quotation
      없음). 순위에서 빠지는 건 사실상 이 경우뿐이다.
    - kind="rfq_mismatch": 다른 RFQ 견적이 섞여 들어옴 - 이 MR 후보가 아님.
    - kind="candidate": 그 외 전부. 값이 비었거나 틀린 건 제외 대신 점수
      0점/페널티로 반영하고 순위에 남긴다.
    """
    errors = [issue for issue in review.issues if issue.severity == IssueSeverity.ERROR]
    if review.quotation is None:
        return {
            "kind": "parse_failed",
            "evidence": [issue.evidence for issue in errors] or list(review.rejection_evidence),
            "penalties": [],
            "warnings": [],
            "requires_confirmation": False,
        }
    if any(issue.code in NOT_A_CANDIDATE_ISSUE_CODES for issue in errors):
        return {
            "kind": "rfq_mismatch",
            "evidence": [
                issue.evidence for issue in errors if issue.code in NOT_A_CANDIDATE_ISSUE_CODES
            ],
            "penalties": [],
            "warnings": [],
            "requires_confirmation": False,
        }

    penalties: list[dict[str, Any]] = []
    matched_codes: set[str] = set()
    for key, codes, points, label, confirm in PENALTY_RULES:
        hits = [issue for issue in errors if issue.code in codes]
        if not hits:
            continue
        matched_codes.update(issue.code for issue in hits)
        penalties.append({
            "code": key,
            "points": points,
            "label": label,
            "requires_confirmation": confirm,
            "evidence": [issue.evidence for issue in hits],
        })
    warnings = [
        issue.message
        for issue in errors
        if issue.code not in matched_codes
        and issue.code not in FACTOR_HANDLED_ISSUE_CODES
        and issue.code not in NOT_A_CANDIDATE_ISSUE_CODES
    ]
    return {
        "kind": "candidate",
        "evidence": [],
        "penalties": penalties,
        "warnings": warnings,
        "requires_confirmation": any(row["requires_confirmation"] for row in penalties),
    }


def _is_structurally_rankable(review: QuotationReview) -> tuple[bool, list[str]]:
    """하위 호환용 - 이제는 파싱 실패/다른 RFQ 견적만 순위 대상이 아니다."""
    classified = classify_review(review)
    return classified["kind"] == "candidate", list(classified["evidence"])


def _scorecard_view(scorecard: Any) -> tuple[float | None, int]:
    """(0~100 점수, 평가 건수). 평가이력이 없으면 (None, 0)."""
    if not isinstance(scorecard, dict) or scorecard.get("weighted_score") is None:
        return None, 0
    try:
        weighted = float(scorecard["weighted_score"])
    except (TypeError, ValueError):
        return None, 0
    count = int(scorecard.get("evaluation_count") or 1)
    return max(0.0, min(100.0, weighted * 20.0)), max(1, count)


def rank_quotations_with_spec_scores(
    review_data: list[QuotationReview | dict[str, Any]],
    rfq_data: RFQRequirements | dict[str, Any],
    spec_assessments: dict[str, QuotationSpecAssessment],
    *,
    top_k: int = 3,
    supplier_scorecards: dict[str, dict[str, Any]] | None = None,
    weights: dict[str, float] | None = None,
    evaluation_source: str = DEFAULT_SPEC_EVALUATION_SOURCE,
) -> RankingResult:
    """가격·납기·규격·협력사 평가이력 4항목 가중합에서 페널티를 빼 순위를 매긴다.

    규칙(요약)
    - 순위에서 빠지는 건 파싱 실패와 다른 RFQ 견적뿐. 나머지는 전부 순위에 남는다.
    - 협력사가 안 낸 값(금액·납기)은 그 항목 0점.
    - 우리가 아직 못 가진 값(규격 AI 평가 미완료, 신규 협력사 평가이력, 회신
      1건이라 비교 불가한 가격)은 그 항목을 빼고 가중치를 재정규화한다.
    - 수량 미달·만료·금액 불일치 등은 페널티로 총점에서 차감한다.
    """

    base_weights = quotation_factor_weights(weights)
    if top_k < 1:
        raise ValueError("top_k는 1 이상이어야 합니다.")

    rfq = rfq_data if isinstance(rfq_data, RFQRequirements) else RFQRequirements.model_validate(rfq_data)
    reviews = [
        row if isinstance(row, QuotationReview) else QuotationReview.model_validate(row)
        for row in review_data
    ]
    supplier_scorecards = supplier_scorecards or {}

    excluded: list[dict[str, Any]] = []
    parse_failed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for review in reviews:
        classified = classify_review(review)
        if classified["kind"] != "candidate":
            entry = {
                "kind": classified["kind"],
                "quotation_id": review.quotation_id,
                "supplier_name": review.supplier_name,
                "status": review.status.value,
                "evidence": classified["evidence"],
            }
            excluded.append(entry)
            if classified["kind"] == "parse_failed":
                parse_failed.append(entry)
            continue
        candidates.append({"review": review, "classified": classified})

    if not candidates:
        return RankingResult(
            rfq_name=rfq.rfq_name,
            requested_top_k=top_k,
            recommended=[],
            excluded=excluded,
            parse_failed=parse_failed,
            competition_count=0,
            single_bid=False,
        )

    # 비교 금액: 통화가 섞이면 ERP 환산금액, 없으면 '가격 미제출'로 0점.
    currencies = {row["review"].quotation.currency for row in candidates}
    use_base_amount = len(currencies) > 1
    for row in candidates:
        quotation = row["review"].quotation
        amount = quotation.base_total_amount if use_base_amount else quotation.total_amount
        row["amount"] = amount if amount is not None and amount > 0 else None
        delivery, late_days = _delivery_metrics(row["review"], rfq)
        row["delivery"] = delivery
        row["late_days"] = late_days

    priced = [row["amount"] for row in candidates if row["amount"] is not None]
    lowest_amount = min(priced) if priced else None
    competition_count = len(candidates)
    price_factor = PRICE_COMPETITION_FACTOR.get(competition_count, 1.0)
    dated = [row["delivery"] for row in candidates if row["delivery"] is not None]
    earliest_delivery = min(dated) if dated else None

    for row in candidates:
        review = row["review"]
        quotation = review.quotation
        factors: dict[str, float | None] = {}
        factor_multipliers: dict[str, float] = {}
        missing: list[dict[str, str]] = []

        # 가격
        if price_factor <= 0:
            factors["price"] = None
            missing.append({"factor": "price", "reason": "유효 견적이 1건뿐이라 가격 비교 불가(단독 응찰)"})
        elif row["amount"] is None or lowest_amount is None:
            factors["price"] = 0.0
            factor_multipliers["price"] = price_factor
        else:
            excess = float((row["amount"] - lowest_amount) / lowest_amount)
            factors["price"] = round(max(0.0, min(100.0, 100.0 * (1 - excess / PRICE_ZERO_SCORE_EXCESS))), 2)
            factor_multipliers["price"] = price_factor

        # 납기
        if row["late_days"] is not None:
            factors["delivery"] = round(
                max(0.0, min(100.0, 100.0 - row["late_days"] * DELIVERY_POINTS_PER_LATE_DAY)), 2
            )
        elif row["delivery"] is not None and earliest_delivery is not None:
            behind = (row["delivery"] - earliest_delivery).days
            factors["delivery"] = round(max(0.0, 100.0 - behind * DELIVERY_POINTS_PER_LATE_DAY), 2)
        else:
            factors["delivery"] = 0.0
        factor_multipliers["delivery"] = 1.0

        # 규격(AI)
        assessment = spec_assessments.get(review.quotation_id)
        warnings = list(row["classified"]["warnings"])
        if assessment is None:
            factors["specification"] = None
            missing.append({"factor": "specification", "reason": "AI 규격 평가 미완료"})
        else:
            factors["specification"] = float(assessment.score)
            factor_multipliers["specification"] = 1.0
            if assessment.compliant is False:
                warnings.append(f"AI가 필수 규격 불일치로 판단: {assessment.reason}")

        # 협력사 평가이력
        card_score, card_count = _scorecard_view(
            supplier_scorecards.get(str(quotation.supplier_id or ""))
            or supplier_scorecards.get(str(quotation.supplier_name or ""))
        )
        if card_score is None:
            factors["scorecard"] = None
            missing.append({"factor": "scorecard", "reason": "평가 이력 없음(신규 협력사)"})
        else:
            factors["scorecard"] = round(card_score, 2)
            factor_multipliers["scorecard"] = SCORECARD_COUNT_FACTOR.get(card_count, 1.0)

        effective = {
            key: base_weights[key] * factor_multipliers[key]
            for key in FACTOR_KEYS
            if factors.get(key) is not None and key in factor_multipliers
        }
        weight_total = sum(effective.values())
        applied = {key: round(value / weight_total, 4) for key, value in effective.items()} if weight_total > 0 else {}
        base_score = round(
            sum(float(factors[key]) * applied[key] for key in applied), 2
        ) if applied else 0.0
        penalty_points = float(sum(item["points"] for item in row["classified"]["penalties"]))
        overall = round(max(0.0, base_score - penalty_points), 2)

        numeric_keys = [key for key in ("price", "delivery") if key in applied]
        numeric_weight_sum = sum(applied[key] for key in numeric_keys)
        numeric_score = (
            round(sum(float(factors[key]) * applied[key] for key in numeric_keys) / numeric_weight_sum, 2)
            if numeric_weight_sum > 0
            else None
        )

        row.update({
            "factors": factors,
            "applied": applied,
            "missing": missing,
            "base_score": base_score,
            "penalty_points": penalty_points,
            "overall": overall,
            "numeric_score": numeric_score,
            "assessment": assessment,
            "card_count": card_count,
            "warnings": warnings,
        })

    candidates.sort(key=lambda row: (
        -row["overall"],
        row["amount"] if row["amount"] is not None else Decimal("Infinity"),
        row["late_days"] if row["late_days"] is not None else 10**9,
        row["delivery"].toordinal() if row["delivery"] else 10**9,
        -(row["factors"]["scorecard"] if row["factors"].get("scorecard") is not None else -1.0),
        row["review"].quotation_id,
    ))

    recommended: list[RankedQuotation] = []
    previous_score: float | None = None
    current_rank = 0
    for position, row in enumerate(candidates, 1):
        if row["overall"] != previous_score:
            current_rank = position
            previous_score = row["overall"]
        if current_rank > top_k:
            break
        review = row["review"]
        quotation = review.quotation
        assessment = row["assessment"]
        factors = row["factors"]
        tied = sum(1 for other in candidates if other["overall"] == row["overall"]) > 1
        delivery_label = row["delivery"].isoformat() if row["delivery"] else "미기재"

        def _fmt(key: str) -> str:
            value = factors.get(key)
            return "제외" if value is None else f"{value:.0f}"

        penalty_text = (
            " 페널티 -" + ", -".join(
                f"{item['points']:.0f}({item['label']})" for item in row["classified"]["penalties"]
            ) + "."
            if row["classified"]["penalties"]
            else ""
        )
        spec_reason = f" {assessment.reason}" if assessment is not None else ""
        reason = (
            f"종합 {row['overall']:.2f}점 = 가격 {_fmt('price')} · 납기 {_fmt('delivery')} · "
            f"규격 {_fmt('specification')} · 평가이력 {_fmt('scorecard')}.{penalty_text}{spec_reason} "
            f"총액 {quotation.total_amount} {quotation.currency}, 납기 {delivery_label}."
        )
        recommended.append(RankedQuotation(
            rank=current_rank,
            quotation_id=quotation.quotation_id,
            supplier_id=quotation.supplier_id,
            supplier_name=quotation.supplier_name,
            total_amount=quotation.total_amount,
            comparison_amount=row["amount"] if row["amount"] is not None else Decimal("0"),
            currency=quotation.currency,
            expected_delivery_date=row["delivery"],
            late_days=row["late_days"],
            tied=tied,
            reason=reason,
            numeric_score=row["numeric_score"],
            specification_score=factors.get("specification"),
            overall_score=row["overall"],
            specification_reason=assessment.reason if assessment is not None else None,
            specification_items=(
                [item.model_dump(mode="json") for item in assessment.items]
                if assessment is not None
                else []
            ),
            evaluation_source=evaluation_source if assessment is not None else None,
            price_score=factors.get("price"),
            delivery_score=factors.get("delivery"),
            scorecard_score=factors.get("scorecard"),
            scorecard_count=row["card_count"],
            base_score=row["base_score"],
            penalty_points=row["penalty_points"],
            penalties=row["classified"]["penalties"],
            applied_weights=row["applied"],
            missing_factors=row["missing"],
            requires_confirmation=row["classified"]["requires_confirmation"],
            warnings=row["warnings"],
        ))

    return RankingResult(
        rfq_name=rfq.rfq_name,
        requested_top_k=top_k,
        recommended=recommended,
        excluded=excluded,
        parse_failed=parse_failed,
        competition_count=competition_count,
        single_bid=competition_count == 1,
    )


def rank_quotations(
    review_data: list[QuotationReview | dict[str, Any]],
    rfq_data: RFQRequirements | dict[str, Any],
    *,
    top_k: int = 3,
    supplier_scorecards: dict[str, dict[str, Any]] | None = None,
) -> RankingResult:
    """결정론적 규칙으로 정렬하며 top-k 경계의 동점은 모두 포함한다.

    가격과 납기가 모두 같은 후보에만 최근 공급사 평가를 최종 타이브레이커로
    사용한다. 같은 조건의 후보 중 평가가 없는 신규 업체가 하나라도 있으면
    과거 평가를 정렬에 사용하지 않아 신규 업체를 감점하지 않는다.
    """
    if top_k < 1:
        raise ValueError("top_k는 1 이상이어야 합니다.")
    from backend_logic2.policies.runtime import current_policy
    delivery_first = current_policy().rules.quotation_priority == "delivery_then_price"
    rfq = rfq_data if isinstance(rfq_data, RFQRequirements) else RFQRequirements.model_validate(rfq_data)
    reviews = [row if isinstance(row, QuotationReview) else QuotationReview.model_validate(row) for row in review_data]

    supplier_scorecards = supplier_scorecards or {}
    rankable_reviews: list[QuotationReview] = []
    excluded: list[dict[str, Any]] = []
    for review in reviews:
        if not (
            review.status == ReviewStatus.ACCEPTED
            and review.valid
            and review.specification_compliant
            and review.quotation is not None
        ):
            excluded.append({
                "quotation_id": review.quotation_id,
                "supplier_name": review.supplier_name,
                "status": review.status.value,
                "evidence": review.rejection_evidence or [issue.evidence for issue in review.issues],
            })
            continue
        rankable_reviews.append(review)

    currencies = {review.quotation.currency for review in rankable_reviews if review.quotation}
    use_base_amount = len(currencies) > 1
    if use_base_amount and any(
        review.quotation is None or review.quotation.base_total_amount is None
        for review in rankable_reviews
    ):
        raise ValueError(
            "서로 다른 통화의 견적을 비교하려면 모든 Supplier Quotation에 "
            "ERPNext base_grand_total 환산금액이 필요합니다."
        )
    candidates: list[tuple[tuple[Decimal | int, ...], QuotationReview, date | None, int | None]] = []
    for review in rankable_reviews:
        quotation = review.quotation
        if quotation is None:
            raise ValueError(f"검토 통과 견적에 원본 데이터가 없습니다: {review.quotation_id}")
        delivery, late_days = _delivery_metrics(review, rfq)
        comparison_amount = (
            quotation.base_total_amount
            if use_base_amount
            else quotation.total_amount
        )
        if comparison_amount is None:
            raise ValueError(f"견적 비교금액이 없습니다: {review.quotation_id}")
        # 규격 통과 견적만 남은 뒤 총액을 우선하고, 동액이면 납기를 비교한다.
        delivery_sort = delivery.toordinal() if delivery else 10**9
        late_sort = late_days if late_days is not None else 10**9
        key = (comparison_amount, late_sort, delivery_sort)
        if delivery_first:
            key = (late_sort, delivery_sort, comparison_amount)
        candidates.append((key, review, delivery, late_days))

    grouped: dict[tuple[Decimal | int, ...], list[tuple[tuple[Decimal | int, ...], QuotationReview, date | None, int | None]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate[0], []).append(candidate)

    candidates = []
    ranking_keys: dict[str, tuple[Any, ...]] = {}
    scorecard_tiebreaks: set[str] = set()
    for base_key in sorted(grouped):
        group = grouped[base_key]
        scored: list[tuple[Decimal, tuple[tuple[Decimal | int, ...], QuotationReview, date | None, int | None]]] = []
        for candidate in group:
            quotation = candidate[1].quotation
            if quotation is None:
                raise ValueError(f"순위 후보에 원본 견적이 없습니다: {candidate[1].quotation_id}")
            card = supplier_scorecards.get(str(quotation.supplier_id or ""))
            score = card.get("weighted_score") if isinstance(card, dict) else None
            if score is None:
                scored = []
                break
            scored.append((Decimal(str(score)), candidate))
        if scored:
            scored.sort(key=lambda row: (-row[0], row[1][1].quotation_id))
            candidates.extend(candidate for _, candidate in scored)
            for score, candidate in scored:
                quotation_id = candidate[1].quotation_id
                ranking_keys[quotation_id] = (*base_key, -score)
                scorecard_tiebreaks.add(quotation_id)
        else:
            sorted_group = sorted(group, key=lambda row: row[1].quotation_id)
            candidates.extend(sorted_group)
            for candidate in sorted_group:
                ranking_keys[candidate[1].quotation_id] = base_key
    recommended: list[RankedQuotation] = []
    previous_key: tuple[Any, ...] | None = None
    current_rank = 0
    for position, (key, review, delivery, late_days) in enumerate(candidates, 1):
        ranking_key = ranking_keys[review.quotation_id]
        if ranking_key != previous_key:
            current_rank = position
            previous_key = ranking_key
        if current_rank > top_k:
            break
        quotation = review.quotation
        if quotation is None:
            raise ValueError(f"순위 후보에 원본 견적이 없습니다: {review.quotation_id}")
        tied = sum(
            1
            for _, other_review, *_ in candidates
            if ranking_keys[other_review.quotation_id] == ranking_key
        ) > 1
        delivery_reason = f", 최종 납기 {delivery.isoformat()}" if delivery else ", 납기 미기재"
        late_reason = f" (RFQ 대비 {late_days}일 지연)" if late_days is not None else ""
        scorecard = supplier_scorecards.get(str(quotation.supplier_id or ""))
        score = scorecard.get("weighted_score") if isinstance(scorecard, dict) else None
        score_reason = (
            f", 동일 가격·납기 내 공급사 평가 {score}/5 반영"
            if review.quotation_id in scorecard_tiebreaks
            else ""
        )
        comparison_reason = (
            f", ERP 회사 기준 환산금액 {quotation.base_total_amount}"
            if use_base_amount
            else ""
        )
        recommended.append(RankedQuotation(
            rank=current_rank,
            quotation_id=quotation.quotation_id,
            supplier_id=quotation.supplier_id,
            supplier_name=quotation.supplier_name,
            total_amount=quotation.total_amount,
            comparison_amount=quotation.base_total_amount if use_base_amount else quotation.total_amount,
            currency=quotation.currency,
            expected_delivery_date=delivery,
            late_days=late_days,
            tied=tied,
            reason=f"{'납기 우선' if delivery_first else '금액 우선'} · 규격·수량·산식 검토 통과, 총금액 {quotation.total_amount} {quotation.currency}{comparison_reason}{delivery_reason}{late_reason}{score_reason}",
        ))

    return RankingResult(
        rfq_name=rfq.rfq_name,
        requested_top_k=top_k,
        recommended=recommended,
        excluded=excluded,
    )


def _attach_supplier_scorecards(quotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """최근 공급사 평가(최대 3건 평균 + 건수)를 붙인다.

    저장소 장애나 신규 업체는 평가이력 없음으로 두고, 순위 쪽에서 그 항목을
    빼고 가중치를 재정규화한다.
    """
    suppliers = [str(row.get("supplier") or "").strip() for row in quotations]
    try:
        scorecards = get_supplier_scorecard_history(suppliers)
    except Exception as exc:
        LOGGER.warning("Supplier Scorecard 집계 실패: %s", exc)
        scorecards = {}
    return [
        {
            **quotation,
            "supplier_scorecard": scorecards.get(str(quotation.get("supplier") or "").strip()),
        }
        for quotation in quotations
    ]


def _number(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _first_non_zero(*values: Any) -> float:
    for value in values:
        number = _number(value)
        if number:
            return number
    return 0.0


def _enrich_ranking_with_prices(
    ranking: list[dict[str, Any]],
    quotations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """검증된 순위 값을 보존하고 ERP 원문의 부가 정보만 결합한다."""
    by_name = {str(row.get("name") or "").strip(): row for row in quotations}
    enriched: list[dict[str, Any]] = []
    for ranked in ranking:
        quotation_id = str(
            ranked.get("name") or ranked.get("quotation_id") or ""
        ).strip()
        quotation = by_name.get(quotation_id)
        if quotation is None:
            LOGGER.warning(
                "검증된 순위 %s에 대응하는 ERP 원문을 찾지 못해 부가 정보 보강을 생략합니다.",
                quotation_id,
            )
            quotation = {}
        items = quotation.get("items") or []
        first_item = items[0] if items else {}
        total_amount = ranked.get("total_amount")
        if total_amount is None and quotation:
            total_amount = _first_non_zero(
                quotation.get("grand_total"),
                quotation.get("rounded_total"),
                quotation.get("net_total"),
                sum(_number(item.get("amount")) for item in items),
            )
        expected_delivery_date = ranked.get("expected_delivery_date")
        if expected_delivery_date is None and first_item:
            expected_delivery_date = (
                first_item.get("expected_delivery_date")
                or first_item.get("schedule_date")
                or first_item.get("delivery_date")
            )
        enriched.append({
            **ranked,
            "name": ranked.get("name") or ranked.get("quotation_id") or quotation.get("name"),
            "supplier": ranked.get("supplier") or quotation.get("supplier"),
            "currency": ranked.get("currency") or quotation.get("currency") or "KRW",
            "rate": (
                _first_non_zero(first_item.get("rate"), first_item.get("net_rate"))
                if first_item else None
            ),
            "amount": (
                _first_non_zero(first_item.get("amount"), first_item.get("net_amount"))
                if first_item else None
            ),
            "total_amount": total_amount,
            "grand_total": ranked.get("grand_total", total_amount),
            "expected_delivery_date": expected_delivery_date,
            "lead_time_days": first_item.get("lead_time_days") if first_item else None,
            "transaction_date": quotation.get("transaction_date") or ranked.get("transaction_date"),
            "supplier_scorecard": quotation.get("supplier_scorecard") or ranked.get("supplier_scorecard"),
            "valid_till": ranked.get("valid_till") or quotation.get("valid_till"),
        })
    return enriched


def evaluate_quotations(
    rfq_name: str,
    *,
    top_k: int = 3,
    spec_evaluator: QuotationSpecEvaluator | None = None,
    _rfq_names: list[str] | None = None,
    _round_by_rfq: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Validate ERP quotations and combine numeric metrics with semantic spec fit."""
    try:
        rfq = load_rfq_requirements(rfq_name)
    except Exception as exc:
        return {"error": f"RFQ를 찾거나 읽을 수 없습니다: {rfq_name} ({exc})"}

    rfq_names = list(dict.fromkeys(_rfq_names or [rfq_name]))
    quotations = _attach_supplier_scorecards(
        get_quotations_for_rfqs(rfq_names)
        if _rfq_names is not None
        else get_quotations_for_rfq(rfq_name)
    )
    if not quotations:
        return {
            "requirements": rfq.model_dump(mode="json"),
            "quotations": [],
            "ranking": [],
            "message": "제출된 견적이 아직 없습니다.",
        }

    reviewable = (
        get_reviewable_quotations_for_rfqs(rfq_names)
        if _rfq_names is not None
        else get_reviewable_quotations(rfq_name)
    )
    # known_rfq_names=rfq_names: 단일 라운드 평가에서는 rfq_names가
    # [rfq_name] 하나뿐이라 기존과 동일하게 동작하고, 여러 라운드를 함께
    # 평가할 때는(_rfq_names로 여러 개 넘어온 경우) 그 라운드들의 RFQ
    # 이름을 전부 알려줘서 지난 라운드 견적이 RFQ_MISMATCH로 잘못
    # 제외되지 않게 한다.
    reviews = [
        review_quotation(quotation, rfq, known_rfq_names=set(rfq_names))
        for quotation in reviewable
    ]
    scorecards = {
        str(row.get("supplier") or ""): row["supplier_scorecard"]
        for row in quotations
        if row.get("supplier_scorecard") is not None
    }
    spec_assessments: dict[str, QuotationSpecAssessment] = {}
    cache_misses: list[QuotationReview] = []
    cache_hits = 0
    fingerprints: dict[str, str] = {}
    evaluator: QuotationSpecEvaluator | None = None
    evaluator_error: str | None = None
    # 규격 평가기(RunPod)는 4항목 중 하나일 뿐이다. 예전엔 평가기가 없거나
    # 실패하면 순위 전체를 비우고 오류로 돌려줬는데, 이제는 규격 항목만
    # '평가 미완료'로 빼고(가중치 재정규화) 나머지 항목으로 순위를 매긴다.
    # 어느 견적이 규격 점수 없이 매겨졌는지는 missing_factors와
    # specification_evaluation.status로 화면에 그대로 드러난다.
    try:
        evaluator = spec_evaluator or build_quotation_spec_evaluator()
    except (TypeError, ValueError) as exc:
        evaluator_error = f"규격 평가기 설정이 올바르지 않습니다: {exc}"
        LOGGER.warning("Quotation specification evaluator unavailable: %s", exc)

    if evaluator is not None:
        fingerprints = {
            review.quotation_id: specification_evaluation_fingerprint(
                rfq, review.quotation, evaluator
            )
            for review in reviews
            if review.quotation is not None
        }
        try:
            from backend_logic2.repositories.quotation_specification_cache import (
                load_matching,
            )

            cached_rows = load_matching(
                rfq.rfq_name,
                fingerprints,
                evaluator.model_name,
            )
        except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
            # Cache infrastructure failure must not block fresh analysis.
            LOGGER.warning("Quotation specification cache read failed: %s", exc)
            cached_rows = {}
        for review in reviews:
            if review.quotation is None:
                continue
            try:
                cached = QuotationSpecAssessment.model_validate(
                    cached_rows.get(review.quotation_id)
                )
            except (TypeError, ValueError):
                cache_misses.append(review)
                continue
            spec_assessments[review.quotation_id] = cached
            cache_hits += 1

        try:
            fresh_assessments = (
                evaluator.evaluate(rfq, cache_misses) if cache_misses else {}
            )
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Quotation specification evaluation failed: %s", exc)
            fresh_assessments = None
            evaluator_error = f"{evaluator.model_name} 규격 평가 실패: {exc}"
        if fresh_assessments is None:
            evaluator_error = evaluator_error or (
                f"{evaluator.model_name} 규격 평가가 완료되지 않았습니다. "
                "규격 항목을 제외하고 순위를 매겼으며, '회신 새로 확인'으로 다시 평가할 수 있습니다."
            )
            fresh_assessments = {}
        for review in cache_misses:
            assessment = fresh_assessments.get(review.quotation_id)
            if assessment is not None:
                spec_assessments[review.quotation_id] = assessment
        if fresh_assessments:
            try:
                from backend_logic2.repositories.quotation_specification_cache import (
                    save_assessments,
                )

                save_assessments(
                    rfq.rfq_name,
                    fingerprints,
                    evaluator.model_name,
                    {
                        quotation_id: assessment.model_dump(mode="json")
                        for quotation_id, assessment in fresh_assessments.items()
                    },
                )
            except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
                # A cache write failure cannot invalidate good scores.
                LOGGER.warning("Quotation specification cache write failed: %s", exc)

    model_name = evaluator.model_name if evaluator is not None else "unavailable"
    evaluable_ids = [review.quotation_id for review in reviews if review.quotation is not None]
    unevaluated = [quotation_id for quotation_id in evaluable_ids if quotation_id not in spec_assessments]
    if not unevaluated:
        evaluation_status = "completed"
    elif len(unevaluated) == len(evaluable_ids):
        evaluation_status = "failed"
    else:
        evaluation_status = "partial"
    try:
        factor_weights = quotation_factor_weights()
        result = rank_quotations_with_spec_scores(
            reviews,
            rfq,
            spec_assessments,
            top_k=max(1, len(reviews)) if _rfq_names is not None else top_k,
            supplier_scorecards=scorecards,
            weights=factor_weights,
            evaluation_source=model_name,
        )
    except ValueError as exc:
        return {
            "requirements": rfq.model_dump(mode="json"),
            "quotations": quotations,
            "ranking": [],
            "error": str(exc),
        }
    reviews_by_id = {review.quotation_id: review for review in reviews}
    quotation_rounds = {
        str(row.get("name") or "").strip(): {
            "rfq_name": str(row.get("rfq_name") or rfq_name).strip(),
            "rfq_round": (_round_by_rfq or {}).get(
                str(row.get("rfq_name") or rfq_name).strip(),
                0,
            ),
            "valid_till": row.get("valid_till"),
        }
        for row in quotations
    }
    ranking: list[dict[str, Any]] = []
    for ranked in result.recommended:
        review = reviews_by_id[ranked.quotation_id]
        round_meta = quotation_rounds.get(ranked.quotation_id, {})
        ranking.append({
            "name": ranked.quotation_id,
            "quotation_id": ranked.quotation_id,
            "rfq_name": round_meta.get("rfq_name", rfq_name),
            "rfq_round": round_meta.get("rfq_round", 0),
            "valid_till": round_meta.get("valid_till"),
            "supplier": ranked.supplier_id or ranked.supplier_name,
            "supplier_name": ranked.supplier_name,
            "rank": ranked.rank,
            "fulfills_qty": all(item.quantity_compliant for item in review.item_compliance),
            "reason": ranked.reason,
            "issues": [issue.message for issue in review.issues],
            "currency": ranked.currency,
            "total_amount": ranked.total_amount,
            "grand_total": ranked.total_amount,
            "comparison_amount": ranked.comparison_amount,
            "expected_delivery_date": (
                ranked.expected_delivery_date.isoformat()
                if ranked.expected_delivery_date
                else None
            ),
            "late_days": ranked.late_days,
            "tied": ranked.tied,
            "numeric_score": ranked.numeric_score,
            "specification_score": ranked.specification_score,
            "overall_score": ranked.overall_score,
            "specification_reason": ranked.specification_reason,
            "specification_items": ranked.specification_items,
            "evaluation_source": ranked.evaluation_source,
            "price_score": ranked.price_score,
            "delivery_score": ranked.delivery_score,
            "scorecard_score": ranked.scorecard_score,
            "scorecard_count": ranked.scorecard_count,
            "base_score": ranked.base_score,
            "penalty_points": ranked.penalty_points,
            "penalties": ranked.penalties,
            "applied_weights": ranked.applied_weights,
            "missing_factors": ranked.missing_factors,
            "requires_confirmation": ranked.requires_confirmation,
            "warnings": ranked.warnings,
        })
    return {
        "requirements": rfq.model_dump(mode="json"),
        "quotations": quotations,
        "ranking": _enrich_ranking_with_prices(ranking, quotations),
        "excluded": result.excluded,
        "parse_failed": result.parse_failed,
        "competition_count": result.competition_count,
        "single_bid": result.single_bid,
        "specification_evaluation": {
            "status": evaluation_status,
            "model": model_name,
            "cache_hits": cache_hits,
            "cache_misses": len(cache_misses),
            "unevaluated": unevaluated,
            "error": evaluator_error,
            "weights": factor_weights,
        },
    }


def evaluate_quotations_for_rfqs(
    rfq_names: list[str],
    *,
    current_rfq_name: str,
    round_by_rfq: dict[str, int] | None = None,
    spec_evaluator: QuotationSpecEvaluator | None = None,
) -> dict[str, Any]:
    """Evaluate all archived and current RFQ quotations as one candidate pool."""
    normalized = list(dict.fromkeys(
        str(name or "").strip()
        for name in rfq_names
        if str(name or "").strip()
    ))
    if not normalized:
        return {"quotations": [], "ranking": [], "message": "평가할 RFQ가 없습니다."}
    return evaluate_quotations(
        current_rfq_name,
        spec_evaluator=spec_evaluator,
        _rfq_names=normalized,
        _round_by_rfq=round_by_rfq,
    )


def print_evaluation(result: dict[str, Any]) -> None:
    """워크플로 로그에 견적 점수와 순위를 간결하게 출력한다."""
    if result.get("error") or result.get("message"):
        print(result.get("error") or result.get("message"))
        return
    print(f"\n=== 견적 평가 결과 ({len(result.get('quotations') or [])}건 제출됨) ===")
    for row in result.get("ranking") or []:
        print(f"#{row.get('rank')} {row.get('supplier_name') or row.get('supplier')}: {row.get('reason')}")
    for row in result.get("excluded") or []:
        print(f"제외 {row.get('supplier_name') or row.get('quotation_id')}: {row.get('evidence')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="검토 통과 견적 우선순위 정렬")
    parser.add_argument("input", help="검토 결과 JSON 배열")
    parser.add_argument("--rfq", required=True, help="RFQ 요구사항 JSON")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    raw_reviews = load_json(args.input)
    if isinstance(raw_reviews, dict) and "reviews" in raw_reviews:
        raw_reviews = raw_reviews["reviews"]
    if not isinstance(raw_reviews, list):
        raw_reviews = [raw_reviews]
    result = rank_quotations(raw_reviews, load_json(args.rfq), top_k=args.top_k)
    rendered = dump_json(result, args.output)
    if args.output:
        print(f"정렬 완료: {args.output} ({len(result.recommended)}개 추천)")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
