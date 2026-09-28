from datetime import date

from test_quotation_luna_ranking import _review, _rfq, _assessment
from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import rank_quotations_with_spec_scores
from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import load_rfq_requirements_from_erp


def score(review, rfq, **kwargs):
    return rank_quotations_with_spec_scores([review], rfq, {review.quotation_id: _assessment(review.quotation_id, 100)}, **kwargs).recommended[0]


def test_past_delivery_is_zero_and_visible_not_excluded():
    rfq = _rfq().model_copy(update={"delivery_not_before": date(2026, 9, 20)})
    review = _review("Q1", "S1", "100")
    review.quotation.items[0].expected_delivery_date = date(2026, 9, 19)
    result = score(review, rfq)
    assert result.delivery_score == 0
    assert result.requires_confirmation
    assert any(w.startswith("[납기 확인]") for w in result.warnings)


def test_same_day_delivery_allowed_and_any_bad_item_detected():
    rfq = _rfq().model_copy(update={"delivery_not_before": date(2026, 9, 20)})
    review = _review("Q1", "S1", "100")
    review.quotation.items[0].expected_delivery_date = date(2026, 9, 20)
    assert score(review, rfq).delivery_score == 100
    review.quotation.items.append(review.quotation.items[0].model_copy(update={"expected_delivery_date": date(2026, 9, 19)}))
    assert score(review, rfq).delivery_score == 0


def test_lead_time_date_cannot_precede_rfq():
    review = _review("Q1", "S1", "100")
    review.quotation.quotation_date = date(2026, 9, 1)
    review.quotation.items[0].lead_time_days = 2
    assert score(review, _rfq().model_copy(update={"delivery_not_before": date(2026, 9, 20)})).delivery_score == 0


def test_rebid_uses_own_round_date():
    review = _review("Q1", "S1", "100")
    review.quotation.items[0].expected_delivery_date = date(2026, 9, 15)
    current = _rfq().model_copy(update={"rfq_name": "RFQ-2", "delivery_not_before": date(2026, 9, 20)})
    old = _rfq().model_copy(update={"delivery_not_before": date(2026, 9, 10)})
    assert score(review, current, rfq_contexts={"RFQ-1": old}).delivery_score == 100
    old.delivery_not_before = date(2026, 9, 16)
    assert score(review, current, rfq_contexts={"RFQ-1": old}).delivery_score == 0


def test_empty_submission_overrides_cached_full_score():
    review = _review("Q1", "S1", "100")
    review.quotation.items[0].specifications = {}
    result = score(review, _rfq())
    assert result.specification_score == 0
    assert result.specification_items == []
    assert "모든 필수 규격을 충족" not in result.reason
    assert any(w.startswith("[규격 확인]") for w in result.warnings)
    assert result.requires_confirmation


def test_no_buyer_criteria_is_unassessable_not_full_score():
    rfq = _rfq()
    rfq.items[0].specifications = {}
    result = score(_review("Q1", "S1", "100"), rfq)
    assert result.specification_score is None
    assert "specification" not in result.applied_weights
    assert result.requires_confirmation


def test_notes_with_specs_remain_evaluable():
    review = _review("Q1", "S1", "100")
    review.quotation.items[0].specifications = {}
    review.quotation.notes = "재질: SUS316"
    assert score(review, _rfq()).specification_score == 100


def test_rfq_start_ignores_copied_mr_transaction_date():
    calls = []
    def get_many(*args, **kwargs):
        calls.append((args, kwargs))
        return [{"creation": "2026-09-22 10:20:00"}]
    def get_one(*_):
        return {"name": "RFQ-1", "creation": "2026-09-20 12:00:00", "transaction_date": "2026-01-01", "items": [{"item_code": "I1", "qty": 1}]}
    rfq = load_rfq_requirements_from_erp("RFQ-1", get_one=get_one, get_many=get_many)
    assert rfq.delivery_not_before == date(2026, 9, 22)
    assert len(calls) == 1 and calls[0][1]["limit"] == 1
    fallback = load_rfq_requirements_from_erp("RFQ-1", get_one=get_one, get_many=lambda *a, **kw: [])
    assert fallback.delivery_not_before == date(2026, 9, 20)
