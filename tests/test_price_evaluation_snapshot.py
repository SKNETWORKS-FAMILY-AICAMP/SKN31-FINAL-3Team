from unittest.mock import patch

from backend_logic2.services.price_evaluation import build_price_evaluations
from backend_logic2.services.scorecard_service import automatic_scores, completed_scores
from backend_logic2.services.quotation_service import build_quotation_snapshot, refresh_case_quotations


def quotations():
    return [
        {"name": "SQ-A", "supplier": "A", "currency": "KRW", "docstatus": 1,
         "items": [{"item_code": "OTHER", "rate": 99999, "uom": "EA"},
                   {"item_code": "ITEM", "rate": 100, "uom": "EA", "request_for_quotation": "RFQ"}]},
        {"name": "SQ-B", "supplier": "B", "currency": "KRW", "docstatus": 1,
         "items": [{"item_code": "ITEM", "rate": 200, "uom": "EA", "request_for_quotation": "RFQ"}]},
    ]


def test_nested_item_prices_calculated_when_received_and_persisted():
    case = {"case_id": "case", "item_code": "ITEM", "workflow_snapshot": {"values": {"rfq_name": "RFQ"}}}
    with patch("backend_logic2.services.quotation_service.get_quotations_for_rfq", return_value=quotations()):
        snapshot = build_quotation_snapshot(case, "RFQ")
        with patch("backend_logic2.services.quotation_service.case_repository.update_quotation_snapshot", return_value=(case, True)) as save:
            refresh_case_quotations(case, notify=False)
    assert save.call_args.args[1]["price_evaluations"] == snapshot["price_evaluations"]
    assert snapshot["price_evaluations"]["A"]["score"] == 2.5
    assert snapshot["price_evaluations"]["B"]["score"] == 5
    assert snapshot["price_evaluations"]["A"]["supplier_count"] == 2


def test_old_case_with_nested_quotations_is_rated_without_migration():
    case = {"item_code": "ITEM", "quotation_snapshot": {"quotations": quotations(), "rfq_name": "RFQ"}}
    delivery = {"supplier": "A", "delivery_status": "FULL", "promised_delivery_date": "2026-09-17", "full_receipt_date": "2026-09-16"}
    result = completed_scores(case, delivery, {"quality": 4, "service": 5, "communication": 5})
    assert result["price"] == 2.5
    assert result["calculation"]["price_basis"]["highest_rate"] == 200


def test_saved_price_score_is_used_at_evaluation():
    evaluations = build_price_evaluations(quotations(), "ITEM", "RFQ")
    case = {"item_code": "ITEM", "quotation_snapshot": {"price_evaluations": evaluations}}
    result = automatic_scores(case, {"supplier": "B"})
    assert result["scores"]["price"] == 5


def test_duplicate_lines_do_not_hide_price_and_other_round_is_excluded():
    rows = quotations()
    rows[0]["items"].append(dict(rows[0]["items"][1]))
    rows.append({"supplier": "C", "items": [{"item_code": "ITEM", "rate": 99999, "request_for_quotation": "OLD"}]})
    result = build_price_evaluations(rows, "ITEM", "RFQ")
    assert result["A"]["score"] == 2.5
    assert "C" not in result


def test_ambiguous_same_supplier_prices_are_not_arbitrarily_chosen():
    rows = quotations()
    rows[0]["items"].append({"item_code": "ITEM", "rate": 150, "uom": "EA"})
    result = build_price_evaluations(rows, "ITEM", "RFQ")
    assert "score" not in result["A"]
