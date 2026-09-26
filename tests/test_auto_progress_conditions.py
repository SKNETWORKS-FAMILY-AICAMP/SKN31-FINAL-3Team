"""자동 진행 조건 평가 - 애매하면 사람에게 넘긴다."""

from __future__ import annotations

import pytest

from backend_logic2.policies.runtime import policy_scope
from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services.auto_progress import (
    evaluate_final_selection,
    evaluate_rfq_dispatch,
    score_gap,
)


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


def _ranking_result(**overrides):
    result = {
        "ranking": [
            {
                "supplier": "동관컴퍼니",
                "quotation_id": "SQ-1",
                "overall_score": 93.6,
                "total_amount": "8400000",
                "penalties": [],
            },
            {"supplier": "세희세희", "quotation_id": "SQ-2", "overall_score": 81.2},
            {"supplier": "진짜효민", "quotation_id": "SQ-3", "overall_score": 73.4},
        ],
        "parse_failed": [],
        "competition_count": 3,
        "single_bid": False,
        "specification_evaluation": {"status": "completed", "model": "qwen3.5-9b-4bit"},
    }
    result.update(overrides)
    return result


@pytest.fixture
def automation_on():
    with policy_scope(_policy(automation_mode="on")):
        yield


def test_all_conditions_pass_proceeds_without_a_person(automation_on) -> None:
    decision = evaluate_final_selection(
        _ranking_result(), deadline_passed=True, known_supplier_names={"동관컴퍼니"}
    )
    assert decision.allowed is True
    assert decision.should_proceed is True
    assert decision.evidence["score_gap"] == 12.4
    assert decision.evidence["top_supplier"] == "동관컴퍼니"


@pytest.mark.parametrize(
    "overrides, expected_code",
    [
        ({"parse_failed": [{"quotation_id": "SQ-9"}]}, "PARSE_FAILED"),
        ({"single_bid": True, "competition_count": 1}, "MIN_COMPETITION"),
        ({"specification_evaluation": {"status": "partial", "unevaluated": ["SQ-2"]}}, "SPEC_EVALUATION"),
        ({"ranking": []}, "HAS_RANKING"),
    ],
)
def test_each_risk_stops_the_flow(automation_on, overrides, expected_code) -> None:
    decision = evaluate_final_selection(_ranking_result(**overrides), deadline_passed=True)
    assert decision.allowed is False
    assert expected_code in {row["code"] for row in decision.blockers}
    assert decision.should_proceed is False


def test_close_scores_go_to_a_person(automation_on) -> None:
    result = _ranking_result(ranking=[
        {"supplier": "A", "overall_score": 84.0, "total_amount": "100", "penalties": []},
        {"supplier": "B", "overall_score": 80.0},
        {"supplier": "C", "overall_score": 70.0},
    ])
    decision = evaluate_final_selection(result, deadline_passed=True)
    assert decision.allowed is False
    assert "SCORE_GAP" in {row["code"] for row in decision.blockers}


def test_confirmation_penalty_on_the_top_quotation_stops_the_flow(automation_on) -> None:
    result = _ranking_result()
    result["ranking"][0]["penalties"] = [
        {"code": "INSUFFICIENT_QUANTITY", "label": "수량 부족", "requires_confirmation": True},
    ]
    decision = evaluate_final_selection(result, deadline_passed=True)
    assert decision.allowed is False
    assert "TOP_PENALTY" in {row["code"] for row in decision.blockers}


def test_two_quotations_are_enough_but_a_single_bid_never_is(automation_on) -> None:
    two = _ranking_result(
        ranking=[
            {"supplier": "동관컴퍼니", "quotation_id": "SQ-1", "overall_score": 93.6,
             "total_amount": "8400000", "penalties": []},
            {"supplier": "세희세희", "quotation_id": "SQ-2", "overall_score": 81.2},
        ],
        competition_count=2,
    )
    assert evaluate_final_selection(two, deadline_passed=True).allowed is True

    single = _ranking_result(
        ranking=[{"supplier": "동관컴퍼니", "quotation_id": "SQ-1", "overall_score": 93.6,
                  "total_amount": "8400000", "penalties": []}],
        competition_count=1,
        single_bid=True,
    )
    assert evaluate_final_selection(single, deadline_passed=True).allowed is False


def test_amount_over_the_limit_stops_the_flow() -> None:
    with policy_scope(_policy(automation_mode="on", auto_selection_max_amount=1_000_000)):
        decision = evaluate_final_selection(_ranking_result(), deadline_passed=True)
    assert decision.allowed is False
    assert "AMOUNT_LIMIT" in {row["code"] for row in decision.blockers}


def test_before_the_deadline_nothing_moves(automation_on) -> None:
    decision = evaluate_final_selection(_ranking_result(), deadline_passed=False)
    assert decision.allowed is False
    # 전원이 회신하면 마감을 기다리지 않는다.
    assert evaluate_final_selection(
        _ranking_result(), deadline_passed=False, all_responded=True
    ).allowed is True


def test_shadow_mode_judges_but_never_proceeds() -> None:
    with policy_scope(_policy(automation_mode="shadow")):
        decision = evaluate_final_selection(_ranking_result(), deadline_passed=True)
    assert decision.allowed is True
    assert decision.should_proceed is False
    assert decision.shadow_only is True
    assert "기록만" in decision.summary()


def test_default_policy_keeps_every_checkpoint_manual() -> None:
    decision = evaluate_final_selection(_ranking_result(), deadline_passed=True)
    assert decision.allowed is True
    assert decision.should_proceed is False
    assert decision.mode == "off"


def _pool(needs_search: bool = False):
    return {
        "needs_search": needs_search,
        "reasons": ["[ITEM-1] 기존 공급사 4곳 확보 + 마지막 등록 2년 전 -> 기존 공급사 풀 사용"],
    }


def test_rfq_goes_out_when_the_pool_decision_says_existing_only(automation_on) -> None:
    """판정의 뼈대는 resolve_supplier_pool이 이미 내려놓았다.

    거기서 '기존 풀만'으로 갈라졌다면 신규 협력사가 없고 경쟁 수도
    충족한다는 뜻이라, 같은 비교를 다시 하지 않는다.
    """
    decision = evaluate_rfq_dispatch(
        [{"name": "동관컴퍼니", "email": "a@x.com"}, {"name": "세희세희", "email": "b@x.com"}],
        pool_decision=_pool(),
    )
    assert decision.allowed is True
    assert decision.evidence["needs_search"] is False


def test_a_pool_that_needs_new_suppliers_goes_to_a_person(automation_on) -> None:
    decision = evaluate_rfq_dispatch(
        [{"name": "동관컴퍼니", "email": "a@x.com"}],
        pool_decision={"needs_search": True, "reasons": ["[ITEM-1] 과거 확정 구매이력 없음 -> 신규 공급사 탐색 필요"]},
    )
    assert decision.allowed is False
    blocked = {row["code"]: row["detail"] for row in decision.blockers}
    assert "SUPPLIER_POOL" in blocked
    # 판정 이유를 그대로 전달한다 - 다시 지어내지 않는다.
    assert "과거 확정 구매이력 없음" in blocked["SUPPLIER_POOL"]


def test_rfq_still_needs_contacts_and_someone_to_send_to(automation_on) -> None:
    no_email = evaluate_rfq_dispatch(
        [{"name": "동관컴퍼니", "email": "a@x.com"}, {"name": "세희세희"}],
        pool_decision=_pool(),
    )
    assert "MISSING_EMAIL" in {row["code"] for row in no_email.blockers}

    nobody = evaluate_rfq_dispatch([], pool_decision=_pool())
    assert "HAS_CANDIDATES" in {row["code"] for row in nobody.blockers}


def test_a_missing_pool_decision_is_not_treated_as_passing(automation_on) -> None:
    decision = evaluate_rfq_dispatch(
        [{"name": "동관컴퍼니", "email": "a@x.com"}], pool_decision=None
    )
    assert decision.allowed is False


def test_score_gap_needs_two_quotations() -> None:
    assert score_gap([{"overall_score": 90.0}]) is None
    assert score_gap([{"overall_score": 90.0}, {"overall_score": 77.5}]) == 12.5


def test_each_verdict_says_which_step_it_came_from(automation_on) -> None:
    """RFQ 발송 판정이 남아 있는데 최종 선정이 멈춘 것처럼 보이면 안 된다."""
    rfq = evaluate_rfq_dispatch(
        [{"name": "동관컴퍼니", "email": "a@x.com"}], pool_decision=_pool()
    )
    selection = evaluate_final_selection(_ranking_result(), deadline_passed=True)

    assert rfq.as_payload()["node"] == "auto_rfq_dispatch"
    assert selection.as_payload()["node"] == "auto_final_selection"
