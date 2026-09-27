"""최종 선정 자동화 판정 - 자동화 v2의 5단계.

원칙은 4단계와 같다. **판단이 애매하면 사람에게 넘긴다.**

여기서 특히 중요한 구분이 하나 있다. **기다리는 것과 멈춘 것은 다르다.**
규격 평가가 아직 도는 중인 건 사람이 할 일이 없는데 아직 안 끝난 것뿐이다.
그걸 "멈춤"으로 처리하면 담당자가 할 일도 없이 불려 나오고, 진짜 사람이
결정해야 하는 건과 구분이 안 된다.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend_logic2.policies.runtime import policy_scope
from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services.auto_progress import evaluate_final_selection, score_gap


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


def _result(**overrides):
    result = {
        "ranking": [
            {
                "supplier": "동관컴퍼니",
                "quotation_id": "SQ-1",
                "overall_score": 95.0,
                "total_amount": "1000000",
                "penalties": [],
                "valid_till": "2099-12-31",
            },
            {"supplier": "세희상사", "quotation_id": "SQ-2", "overall_score": 70.0},
        ],
        "parse_failed": [],
        "excluded": [],
        "competition_count": 2,
        "single_bid": False,
        "specification_evaluation": {"status": "completed", "model": "qwen3.5-9b"},
    }
    result.update(overrides)
    return result


def _evaluate(policy=None, *, result=None, **kwargs):
    kwargs.setdefault("ready", True)
    kwargs.setdefault("known_supplier_names", {"동관컴퍼니"})
    with policy_scope(policy or _policy(automation_mode="on")):
        return evaluate_final_selection(result or _result(), **kwargs)


# ---------------------------------------------------------------------------
# 통과
# ---------------------------------------------------------------------------


def test_a_clean_ranking_is_selected_without_a_person() -> None:
    decision = _evaluate()

    assert decision.allowed is True
    assert decision.should_proceed is True
    assert decision.needs_person is False


def test_recording_mode_evaluates_but_never_selects() -> None:
    decision = _evaluate(_policy(automation_mode="shadow"))

    assert decision.allowed is True
    assert decision.should_proceed is False


def test_automation_off_never_selects() -> None:
    assert _evaluate(_policy()).should_proceed is False


def test_the_stage_switch_alone_can_stop_it() -> None:
    decision = _evaluate(_policy(automation_mode="on", auto_final_selection=False))

    assert decision.allowed is True
    assert decision.should_proceed is False


def test_it_applies_to_a_rebid_round_too() -> None:
    """차수와 무관하게 판정한다 - 2차도 조건은 같다."""
    assert _evaluate().should_proceed is True


# ---------------------------------------------------------------------------
# 기다린다 (사람을 부르지 않는다)
# ---------------------------------------------------------------------------


def test_a_running_spec_evaluation_waits_instead_of_calling_a_person() -> None:
    """⚠️ 사람이 할 일이 없는데 부르면 안 된다. 아직 안 끝난 것뿐이다."""
    decision = _evaluate(result=_result(
        specification_evaluation={"status": "running", "unevaluated": ["SQ-1"]},
    ))

    assert decision.should_proceed is False
    assert decision.needs_person is False
    assert "SPEC_EVALUATION" in {row["code"] for row in decision.waiting}
    assert "아직 판정할 때가 아닙니다" in decision.summary()


def test_not_being_due_yet_waits_instead_of_calling_a_person() -> None:
    decision = _evaluate(ready=False, not_ready_reason="마감 전입니다")

    assert decision.should_proceed is False
    assert decision.needs_person is False
    assert "마감 전입니다" in decision.summary()


def test_waiting_plus_a_real_blocker_still_calls_a_person() -> None:
    """기다릴 이유와 사람이 판단할 이유가 섞이면 사람을 부른다."""
    decision = _evaluate(
        result=_result(specification_evaluation={"status": "running", "unevaluated": ["SQ-1"]}),
        known_supplier_names=set(),
    )

    assert decision.needs_person is True
    assert "KNOWN_SUPPLIER" in {row["code"] for row in decision.blockers}


# ---------------------------------------------------------------------------
# 사람을 부른다
# ---------------------------------------------------------------------------


def test_no_response_calls_a_person() -> None:
    """회신 0건은 자동 연장하지 않고 사람을 부른다."""
    decision = _evaluate(result=_result(ranking=[], competition_count=0))

    assert decision.needs_person is True
    assert "HAS_RANKING" in {row["code"] for row in decision.blockers}


def test_a_single_bid_calls_a_person() -> None:
    decision = _evaluate(result=_result(
        ranking=[_result()["ranking"][0]], competition_count=1, single_bid=True,
    ))

    assert decision.needs_person is True
    assert "MIN_COMPETITION" in {row["code"] for row in decision.blockers}


def test_a_single_bid_is_refused_even_if_the_minimum_were_forced_to_one() -> None:
    """설정으로 하한을 1로 만들어도 단독 응찰은 통과하지 못한다.

    스키마가 하한 2로 막지만(test_automation_policy), 그걸 우회해도
    single_bid를 따로 보기 때문에 막힌다. 안전장치는 겹쳐 두는 게 맞다.
    """
    decision = _evaluate(
        _policy(automation_mode="on", auto_selection_min_quotations=1),
        result=_result(
            ranking=[_result()["ranking"][0]], competition_count=1, single_bid=True,
        ),
    )

    assert decision.needs_person is True
    assert "MIN_COMPETITION" in {row["code"] for row in decision.blockers}


def test_an_unreadable_quotation_calls_a_person() -> None:
    decision = _evaluate(result=_result(parse_failed=[{"quotation_id": "SQ-3"}]))

    assert decision.needs_person is True
    assert "PARSE_FAILED" in {row["code"] for row in decision.blockers}


def test_a_close_race_calls_a_person() -> None:
    ranking = _result()["ranking"]
    ranking[1]["overall_score"] = 90.0
    decision = _evaluate(result=_result(ranking=ranking))

    assert decision.needs_person is True
    assert "SCORE_GAP" in {row["code"] for row in decision.blockers}


def test_a_large_amount_calls_a_person() -> None:
    ranking = _result()["ranking"]
    ranking[0]["total_amount"] = "60000000"
    decision = _evaluate(result=_result(ranking=ranking))

    assert decision.needs_person is True
    assert "AMOUNT_LIMIT" in {row["code"] for row in decision.blockers}


def test_a_first_time_supplier_calls_a_person() -> None:
    decision = _evaluate(known_supplier_names=set())

    assert decision.needs_person is True
    assert "KNOWN_SUPPLIER" in {row["code"] for row in decision.blockers}
    assert "3년" in decision.summary()


def test_the_trade_history_window_comes_from_policy() -> None:
    decision = _evaluate(
        _policy(automation_mode="on", auto_known_supplier_years=7),
        known_supplier_names=set(),
    )

    assert "7년" in decision.summary()


def test_an_unknown_trade_history_is_not_treated_as_permission() -> None:
    """값을 알 수 없을 때도 통과가 아니라 정지다."""
    decision = _evaluate(known_supplier_names=None)

    assert decision.needs_person is True
    assert "KNOWN_SUPPLIER" in {row["code"] for row in decision.blockers}


def test_a_penalty_needing_confirmation_calls_a_person() -> None:
    ranking = _result()["ranking"]
    ranking[0]["penalties"] = [
        {"code": "LATE", "label": "납기 지연 이력", "requires_confirmation": True},
    ]
    decision = _evaluate(result=_result(ranking=ranking))

    assert decision.needs_person is True
    assert "TOP_PENALTY" in {row["code"] for row in decision.blockers}


def test_an_expired_quotation_is_never_auto_selected() -> None:
    """확정(Submit)은 되돌리기 어렵다. 조건 단계에서 막아야 한다."""
    ranking = _result()["ranking"]
    ranking[0]["valid_till"] = "2020-01-01"
    decision = _evaluate(result=_result(ranking=ranking), today=date(2026, 9, 27))

    assert decision.needs_person is True
    assert "QUOTATION_VALIDITY" in {row["code"] for row in decision.blockers}


def test_an_unreadable_validity_date_is_not_treated_as_permission() -> None:
    ranking = _result()["ranking"]
    ranking[0]["valid_till"] = "곧"
    decision = _evaluate(result=_result(ranking=ranking))

    assert decision.needs_person is True


def test_a_quotation_without_a_supplier_name_calls_a_person() -> None:
    ranking = _result()["ranking"]
    ranking[0]["supplier"] = ""
    ranking[0]["supplier_name"] = ""
    decision = _evaluate(result=_result(ranking=ranking))

    assert decision.needs_person is True
    assert "KNOWN_SUPPLIER" in {row["code"] for row in decision.blockers}


def test_the_verdict_says_which_step_it_came_from() -> None:
    assert _evaluate().as_payload()["node"] == "auto_final_selection"


def test_the_verdict_carries_the_numbers_it_judged_on() -> None:
    """서버 로그를 볼 수 없으므로 근거가 기록에 남아야 한다."""
    evidence = _evaluate().evidence

    assert evidence["competition_count"] == 2
    assert evidence["score_gap"] == 25.0
    assert evidence["top_supplier"] == "동관컴퍼니"
    assert evidence["top_amount"] == 1000000.0


# ---------------------------------------------------------------------------
# 점수차 계산
# ---------------------------------------------------------------------------


def test_score_gap_needs_two_quotations() -> None:
    assert score_gap([{"overall_score": 90.0}]) is None
    assert score_gap([]) is None


def test_score_gap_is_first_minus_second() -> None:
    assert score_gap([{"overall_score": 90.0}, {"overall_score": 77.5}]) == 12.5


def test_score_gap_is_unknown_when_a_score_is_missing() -> None:
    assert score_gap([{"overall_score": 90.0}, {}]) is None
