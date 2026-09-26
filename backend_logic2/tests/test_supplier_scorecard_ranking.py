from unittest.mock import MagicMock, patch

from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
    _attach_supplier_scorecards,
    rank_quotations,
)
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
            "evaluation_count": 2,
        }
    }

    with patch(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.get_supplier_scorecard_history",
        return_value=scorecards,
    ):
        enriched = _attach_supplier_scorecards(quotations)

    assert enriched[0]["supplier_scorecard"] == scorecards["공급사 A"]
    assert enriched[1]["supplier_scorecard"] is None


def _accepted_review(quotation_id: str, supplier: str) -> dict:
    return {
        "quotation": {
            "quotation_id": quotation_id,
            "rfq_name": "RFQ-1",
            "supplier_id": supplier,
            "supplier_name": supplier,
            "currency": "KRW",
            "subtotal": 1000,
            "tax_amount": 100,
            "total_amount": 1100,
            "items": [{
                "item_name": "품목 A",
                "quantity": 1,
                "unit_price": 1000,
                "amount": 1000,
                "expected_delivery_date": "2026-09-20",
            }],
            "source": {"kind": "portal", "filename": quotation_id},
        },
        "quotation_id": quotation_id,
        "supplier_name": supplier,
        "source_kind": "portal",
        "status": "accepted",
        "valid": True,
        "specification_compliant": True,
    }


def test_scorecard_breaks_tie_only_when_every_tied_supplier_has_score():
    rfq = {
        "rfq_name": "RFQ-1",
        "items": [{"item_name": "품목 A", "quantity": 1}],
    }
    reviews = [_accepted_review("SQ-A", "A"), _accepted_review("SQ-B", "B")]

    ranked = rank_quotations(
        reviews,
        rfq,
        supplier_scorecards={
            "A": {"weighted_score": 3.5},
            "B": {"weighted_score": 4.5},
        },
    )

    assert [(row.quotation_id, row.rank) for row in ranked.recommended] == [
        ("SQ-B", 1),
        ("SQ-A", 2),
    ]


def test_missing_scorecard_keeps_new_supplier_neutral_and_tied():
    rfq = {
        "rfq_name": "RFQ-1",
        "items": [{"item_name": "품목 A", "quantity": 1}],
    }
    reviews = [_accepted_review("SQ-A", "A"), _accepted_review("SQ-B", "B")]

    ranked = rank_quotations(
        reviews,
        rfq,
        supplier_scorecards={"A": {"weighted_score": 4.5}},
    )

    assert [(row.quotation_id, row.rank, row.tied) for row in ranked.recommended] == [
        ("SQ-A", 1, True),
        ("SQ-B", 1, True),
    ]
