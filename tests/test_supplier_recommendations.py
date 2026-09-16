from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from backend_logic2.services.supplier_recommendations import aggregate_evaluations, attach_supplier_recommendations


def test_average_of_completed_evaluations_excludes_missing_price():
    result = aggregate_evaluations([
        {"supplier": "A", "scorecard": {"leadTime": 5, "quality": 3, "service": 5, "communication": 5}},
        {"supplier": "A", "scorecard": {"leadTime": 3, "quality": 4, "price": 2, "service": 4, "communication": 4}},
    ])["A"]
    assert result["average_score"] == pytest.approx((4.5 + 3.4) / 2)
    assert result["evaluation_count"] == 2
    assert result["scores"] == {"leadTime": 4, "quality": 3.5, "price": 2, "service": 4.5, "communication": 4.5}


def test_unrated_supplier_and_invalid_evaluations_have_no_fake_scores():
    assert aggregate_evaluations([{"supplier": "A", "scorecard": {"quality": 5}},
                                  {"supplier": "B", "scorecard": None}]) == {}
    result = aggregate_evaluations([{"supplier": "A", "scorecard": {
        "leadTime": 5, "quality": 3, "price": None, "service": 5, "communication": 5}}])["A"]
    assert result["average_score"] == 4.5
    assert "price" not in result["scores"]


def test_refresh_batches_suppliers_and_uses_current_history_not_snapshot():
    cases = [{"workflow_snapshot": {"values": {"supplier_candidates": [
        {"name": "A", "supplier_scorecard": {"quality": 1}}, {"name": "Unrated"}]}}},
        {"quotation_snapshot": {"quotations": [{"supplier": "A"}]}}]
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [{"supplier": "A", "scorecard": {
        "leadTime": 5, "quality": 3, "service": 5, "communication": 5}}]
    with patch("backend_logic2.services.supplier_recommendations.get_connection", return_value=nullcontext(connection)):
        attach_supplier_recommendations(cases)
    connection.execute.assert_called_once()
    assert "scorecard_status = 'COMPLETED'" in connection.execute.call_args.args[0]
    assert cases[0]["supplier_recommendations"]["A"]["average_score"] == 4.5
    assert "Unrated" not in cases[0]["supplier_recommendations"]
    assert cases[1]["supplier_recommendations"] == cases[0]["supplier_recommendations"]


def test_manual_supplier_evaluations_can_be_loaded_without_candidate_case():
    from backend_logic2.api.procurement_routes import get_supplier_evaluations, SupplierEvaluationRequest
    expected = {"SUP-0001": {"average_score": 4.5, "evaluation_count": 1}}
    with patch("backend_logic2.services.supplier_recommendations.get_supplier_recommendations", return_value=expected) as query:
        result = get_supplier_evaluations(SupplierEvaluationRequest(names=["SUP-0001"]), {"id": "buyer"})
    assert result["items"] == expected
    query.assert_called_once_with(["SUP-0001"])


def test_search_includes_recommendation_using_supplier_id():
    from backend_logic2.api.procurement_routes import search_suppliers_for_frontend
    with (
        patch("backend_logic2.api.procurement_routes.erp_get", return_value=[{"name": "SUP-1", "supplier_name": "표시 이름"}]),
        patch("backend_logic2.services.supplier_recommendations.get_supplier_recommendations", return_value={"SUP-1": {"average_score": 4.5}}) as query,
    ):
        result = search_suppliers_for_frontend({"id": "buyer"}, q="표시", field="name")
    query.assert_called_once_with(["SUP-1"])
    assert result["items"][0]["recommendation"]["average_score"] == 4.5
