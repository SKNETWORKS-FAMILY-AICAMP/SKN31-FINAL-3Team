"""실시간 견적 순위(그래프 밖 읽기 모델) 갱신 규칙."""

from __future__ import annotations

import pytest

from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services import quotation_service


def _case(**overrides):
    case = {
        "case_id": "CASE-1",
        "mr_name": "MAT-MR-1",
        "stage": "QUOTATION_COLLECTION",
        "assigned_user_id": "buyer@example.com",
        "workflow_snapshot": {
            "values": {
                "rfq_name": "RFQ-2",
                "rfq_rounds": [{"rfq_name": "RFQ-1"}],
            }
        },
        "quotation_snapshot": {"recipient_count": 3, "responded_count": 1},
        "live_quotation_ranking": None,
    }
    case.update(overrides)
    return case


def _result(**overrides):
    result = {
        "ranking": [
            {"quotation_id": "SQ-1", "supplier_name": "가나상사", "overall_score": 87.5, "rank": 1},
            {"quotation_id": "SQ-2", "supplier_name": "다라상사", "overall_score": 70.0, "rank": 2},
        ],
        "excluded": [],
        "parse_failed": [],
        "competition_count": 2,
        "single_bid": False,
        "specification_evaluation": {"status": "completed"},
    }
    result.update(overrides)
    return result


@pytest.fixture
def harness(monkeypatch):
    state = {"case": _case(), "saved": [], "notices": [], "evaluate_calls": [], "result": _result()}

    monkeypatch.setattr(quotation_service.case_repository, "get_case", lambda case_id: state["case"])

    def save(case_id, payload):
        state["saved"].append(payload)
        state["case"] = {**state["case"], "live_quotation_ranking": payload}
        return True

    monkeypatch.setattr(quotation_service.case_repository, "save_live_quotation_ranking", save)
    monkeypatch.setattr(
        quotation_service.notification_repository,
        "create_notification",
        lambda **kwargs: state["notices"].append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        "backend_logic2.policies.repository.for_case",
        lambda case_id: {"version": 1, "policy": CompanyPolicy().model_dump()},
    )

    def evaluate(rfq_names, *, current_rfq_name, round_by_rfq=None, spec_evaluator=None):
        state["evaluate_calls"].append((list(rfq_names), current_rfq_name, dict(round_by_rfq or {})))
        return state["result"]

    monkeypatch.setattr(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs",
        evaluate,
    )
    return state


def test_webhook_refresh_ranks_all_rounds_and_saves_without_notice(harness) -> None:
    payload = quotation_service.refresh_live_ranking("CASE-1", "RFQ-2")

    assert harness["evaluate_calls"] == [(["RFQ-1", "RFQ-2"], "RFQ-2", {"RFQ-1": 0, "RFQ-2": 1})]
    assert payload is not None
    assert payload["rfq_names"] == ["RFQ-1", "RFQ-2"]
    assert payload["ranking"][0]["supplier_name"] == "가나상사"
    assert payload["competition_count"] == 2
    assert harness["saved"] and harness["notices"] == []


def test_all_responded_notifies_once_per_round(harness) -> None:
    harness["case"] = _case(quotation_snapshot={"recipient_count": 2, "responded_count": 2})

    quotation_service.refresh_live_ranking("CASE-1", "RFQ-2")
    quotation_service.refresh_live_ranking("CASE-1", "RFQ-2")

    assert len(harness["notices"]) == 1
    notice = harness["notices"][0]
    assert notice["notification_type"] == "QUOTATION_RANKING_READY"
    assert "가나상사" in notice["message"] and "87.5" in notice["message"]
    assert harness["saved"][-1]["notices"] == {"all_responded": "RFQ-2"}


def test_single_bid_and_parse_failure_notices(harness) -> None:
    harness["result"] = _result(
        ranking=[{"quotation_id": "SQ-1", "supplier_name": "가나상사", "overall_score": 60.0}],
        competition_count=1,
        single_bid=True,
    )
    quotation_service.refresh_live_ranking("CASE-1", notify_reason="deadline")
    assert "수용 또는 재비딩" in harness["notices"][-1]["message"]

    harness["case"] = _case()
    harness["result"] = _result(ranking=[], parse_failed=[{"quotation_id": "SQ-9"}], competition_count=0)
    quotation_service.refresh_live_ranking("CASE-1", notify_reason="deadline")
    assert harness["notices"][-1]["title"] == "견적서를 읽지 못했습니다"


def test_refresh_skips_closed_stages_and_never_raises(harness, monkeypatch) -> None:
    harness["case"] = _case(stage="ORDER_START")
    assert quotation_service.refresh_live_ranking("CASE-1") is None
    assert harness["evaluate_calls"] == []

    harness["case"] = _case()

    def boom(*args, **kwargs):
        raise RuntimeError("erp down")

    monkeypatch.setattr(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs",
        boom,
    )
    assert quotation_service.refresh_live_ranking("CASE-1") is None
