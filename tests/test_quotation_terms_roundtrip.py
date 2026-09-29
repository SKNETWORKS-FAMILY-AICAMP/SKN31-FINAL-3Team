"""Keep the new classified representation safe, reversible, and item-scoped."""
from test_quotation_notes_registration import _quotation, _get_one
from backend_logic2.nodes.quotation.quotation_filter.quotation_models import Quotation
from backend_logic2.nodes.quotation.quotation_filter.quotation_terms import render_separated_terms, parse_separated_terms
from backend_logic2.nodes.quotation.quotation_filter.get_supplier_quotations import _quotation_from_document
from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import build_supplier_quotation_payload


def test_multi_item_specs_and_commercial_terms_roundtrip_without_html_execution():
    data = _quotation('보증 12개월\n운송비 별도 & <script>not code</script>')
    data['items'][0]['specifications'] = {'재질': 'SUS316', '압력': '10 < 20\nbar'}
    data['items'].append({**data['items'][0], 'item_code': 'ITEM-2', 'specifications': {'전원': '220V'}})
    rendered = render_separated_terms(Quotation.model_validate(data))
    assert '<script>' not in rendered
    assert parse_separated_terms(rendered) == (
        {1: {'재질': 'SUS316', '압력': '10 < 20\nbar'}, 2: {'전원': '220V'}}, data['notes'],
    )


def test_erp_reload_uses_supplier_specs_not_requested_description():
    data = _quotation('선결제 50%')
    data['items'][0]['specifications'] = {'재질': 'PVC'}
    saved = build_supplier_quotation_payload(data, get_one=_get_one, get_many=lambda *a, **k: [])
    detail = {**saved, 'name': 'SQ-ERP', 'net_total': 1000, 'grand_total': 1000,
              'items': [{**saved['items'][0], 'amount': 1000, 'description': '재질: SUS316 (요청 규격)'}]}
    loaded = _quotation_from_document(detail, 'RFQ-1')
    assert loaded.items[0].specifications == {'재질': 'PVC'}
    assert loaded.notes == '선결제 50%'
    assert loaded.content_sections_separated is True


def test_existing_portal_terms_keep_legacy_path():
    assert parse_separated_terms('<p>규격: SUS316<br>보증: 12개월</p>') is None


def test_empty_supplier_evidence_cannot_borrow_rfq_specs_on_reload():
    saved = build_supplier_quotation_payload(_quotation(None), get_one=_get_one, get_many=lambda *a, **k: [])
    loaded = _quotation_from_document({**saved, 'name': 'SQ', 'net_total': 1000, 'grand_total': 1000,
        'items': [{**saved['items'][0], 'amount': 1000, 'description': '재질: SUS316'}]}, 'RFQ-1')
    assert loaded.items[0].specifications == {}
    assert loaded.notes is None
