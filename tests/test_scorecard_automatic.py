from copy import deepcopy
from unittest.mock import patch

import pytest

from backend_logic2.services.scorecard_service import automatic_scores, completed_scores


@pytest.fixture
def basis():
    return ({"item_code": "ITEM-1", "quotation_snapshot": {"quotations": [
        {"item_code": "ITEM-1", "supplier": "A", "rate": 100, "currency": "KRW", "uom": "EA"},
        {"item_code": "ITEM-1", "supplier": "B", "rate": 200, "currency": "KRW", "uom": "EA"},
    ]}}, {"supplier": "A", "promised_delivery_date": "2026-09-17",
           "full_receipt_date": "2026-09-17", "delivery_status": "FULL"})


@pytest.mark.parametrize("received,score", [("2026-09-14", 5), ("2026-09-15", 5), ("2026-09-16", 4),
    ("2026-09-17", 3), ("2026-09-18", 2), ("2026-09-19", 1), ("2026-09-30", 1)])
def test_delivery_boundaries(basis, received, score):
    case, delivery = basis
    delivery["full_receipt_date"] = received
    assert automatic_scores(case, delivery)["scores"]["leadTime"] == score


def test_price_highest_is_five_and_other_is_proportional(basis):
    case, delivery = basis
    assert automatic_scores(case, delivery)["scores"]["price"] == 2.5
    delivery["supplier"] = "B"
    assert automatic_scores(case, delivery)["scores"]["price"] == 5


def test_unrelated_and_cancelled_quotes_do_not_change_price(basis):
    case, delivery = basis
    case["quotation_snapshot"]["quotations"] += [
        {"item_code": "OTHER", "supplier": "C", "rate": 9999},
        {"item_code": "ITEM-1", "supplier": "C", "rate": 9999, "docstatus": 2},
    ]
    assert automatic_scores(case, delivery)["scores"]["price"] == 2.5


@pytest.mark.parametrize("key,value", [("rate", 0), ("rate", "NaN"), ("rate", "Infinity"),
    ("currency", "USD"), ("uom", "BOX")])
def test_invalid_or_incomparable_quotes_exclude_price(basis, key, value):
    case, delivery = basis
    case["quotation_snapshot"]["quotations"][0][key] = value
    result = completed_scores(case, delivery, {"quality": 4, "service": 3, "communication": 5, "price": 5})
    assert "price" not in result
    assert result["calculation"]["excluded_fields"] == ["price"]
    assert result["leadTime"] == 3


def test_server_ignores_client_automatic_values(basis):
    case, delivery = basis
    result = completed_scores(case, delivery, {"quality": 4, "service": 3, "communication": 5,
                                              "leadTime": 5, "price": 5})
    assert result["leadTime"] == 3
    assert result["price"] == 2.5
    assert result["quality"] == 4
    assert result["calculation"]["version"] == 1


@pytest.mark.parametrize("value", [True, 0, 6, 2.5, "5", float("nan")])
def test_invalid_manual_scores_rejected(basis, value):
    with pytest.raises(ValueError):
        completed_scores(*basis, {"quality": value, "service": 3, "communication": 5})


def test_missing_receipt_and_missing_quotes_are_not_fabricated(basis):
    case, delivery = deepcopy(basis)
    delivery["full_receipt_date"] = None
    case["quotation_snapshot"] = {}
    assert automatic_scores(case, delivery)["scores"] == {}
    with pytest.raises(ValueError):
        completed_scores(case, delivery, {"quality": 4, "service": 3, "communication": 5})


def test_partial_receipt_not_rated(basis):
    case, delivery = basis
    delivery["delivery_status"] = "PARTIAL"
    assert "leadTime" not in automatic_scores(case, delivery)["scores"]


@pytest.mark.parametrize("has_price", [True, False])
def test_submission_persists_server_scores_and_completes_workflow(basis, has_price):
    from backend_logic2.services import workflow_service
    case, delivery = basis
    if not has_price:
        case["quotation_snapshot"] = {}
    case.update(case_id="case-1", status="WAITING_INPUT", stage="SCORECARD")
    task = {"case_id": "case-1", "status": "PENDING", "task_type": "supplier_scorecard"}
    with (
        patch.object(workflow_service.task_repository, "get_task", return_value=task),
        patch.object(workflow_service.case_repository, "get_case", return_value=case),
        patch("backend_logic2.repositories.deliveries.get_delivery_by_case", return_value=delivery),
        patch("backend_logic2.repositories.deliveries.complete_scorecard") as save,
        patch.object(workflow_service.task_repository, "claim_task", return_value={"version": 2}),
        patch.object(workflow_service.task_repository, "complete_claimed_task") as complete,
        patch.object(workflow_service, "_delete_case_notifications_safely"),
        patch.object(workflow_service.case_repository, "transition_case") as transition,
    ):
        workflow_service.resume_task("task-1", answer={"quality": 4, "service": 3, "communication": 5},
                                     answered_by="buyer", expected_version=1)
    assert save.call_args.args[1]["leadTime"] == 3
    if has_price:
        assert save.call_args.args[1]["price"] == 2.5
    else:
        assert "price" not in save.call_args.args[1]
        assert save.call_args.args[1]["calculation"]["excluded_fields"] == ["price"]
    complete.assert_called_once()
    assert transition.call_args.kwargs["status"] == "COMPLETED"


def test_four_scores_average_uses_only_available_weights():
    from backend_logic2.repositories.deliveries import calculate_scorecard_weighted_score
    assert calculate_scorecard_weighted_score({"leadTime": 5, "quality": 5, "service": 5, "communication": 5}) == 5
    assert calculate_scorecard_weighted_score({"leadTime": 5, "quality": 3, "service": 5, "communication": 5}) == 4.25
