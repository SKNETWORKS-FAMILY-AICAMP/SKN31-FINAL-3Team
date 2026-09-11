from unittest.mock import MagicMock, patch

from backend_logic2.nodes.quotation.sq_evaluation import _attach_supplier_scorecards
from backend_logic2.repositories.deliveries import get_supplier_latest_scorecards
from backend_logic2.repositories.deliveries import calculate_scorecard_weighted_score


def test_scorecard_weighted_score_uses_procurement_weights():
    score = calculate_scorecard_weighted_score({
        "leadTime": 5.0,
        "quality": 4.0,
        "price": 3.0,
        "service": 4.0,
        "communication": 5.0,
    })

    assert score == 4.15


def test_scorecard_weighted_score_requires_all_five_fields():
    assert calculate_scorecard_weighted_score({"quality": 5.0}) is None


def test_supplier_latest_scorecard_returns_raw_scores():
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [
        {
            "supplier": "공급사 A",
            "scorecard": {
                "leadTime": 5,
                "quality": 4,
                "price": 3,
                "service": 4,
                "communication": 5,
            },
        },
    ]
    context = MagicMock()
    context.__enter__.return_value = connection

    with patch(
        "backend_logic2.repositories.deliveries.get_connection",
        return_value=context,
    ):
        scorecards = get_supplier_latest_scorecards(["공급사 A", "공급사 A"])

    assert scorecards["공급사 A"] == {
        "leadTime": 5.0,
        "quality": 4.0,
        "price": 3.0,
        "service": 4.0,
        "communication": 5.0,
        "weighted_score": 4.15,
    }


def test_quotation_scorecard_enrichment_keeps_unreviewed_supplier_neutral():
    quotations = [
        {"name": "SQ-1", "supplier": "공급사 A"},
        {"name": "SQ-2", "supplier": "신규 공급사"},
    ]
    scorecards = {
        "공급사 A": {
            "leadTime": 5.0,
            "quality": 4.0,
            "price": 3.0,
            "service": 4.0,
            "communication": 5.0,
            "weighted_score": 4.15,
        }
    }

    with patch(
        "backend_logic2.nodes.quotation.sq_evaluation.get_supplier_latest_scorecards",
        return_value=scorecards,
    ):
        enriched = _attach_supplier_scorecards(quotations)

    assert enriched[0]["supplier_scorecard"] == scorecards["공급사 A"]
    assert enriched[1]["supplier_scorecard"] is None
