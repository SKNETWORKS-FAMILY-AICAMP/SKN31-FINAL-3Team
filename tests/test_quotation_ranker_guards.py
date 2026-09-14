from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
    _enrich_ranking_with_prices,
)


def test_erp_enrichment_does_not_overwrite_verified_amount_or_delivery() -> None:
    enriched = _enrich_ranking_with_prices(
        [{
            "name": "SQ-1",
            "supplier": "A",
            "currency": "KRW",
            "total_amount": 1100,
            "grand_total": 1100,
            "expected_delivery_date": "2026-09-30",
        }],
        [{
            "name": "SQ-1",
            "supplier": "A",
            "currency": "KRW",
            "grand_total": 9999,
            "items": [{
                "rate": 1000,
                "amount": 1000,
                "expected_delivery_date": "2026-09-10",
            }],
        }],
    )

    assert enriched[0]["total_amount"] == 1100
    assert enriched[0]["grand_total"] == 1100
    assert enriched[0]["expected_delivery_date"] == "2026-09-30"
