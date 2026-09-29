"""Offline regressions: no ERP writes, RunPod jobs or supplier emails."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from test_quotation_luna_ranking import _rfq, _review, _assessment
from test_auto_final_selection import _evaluate, _result
from backend_logic2.nodes.quotation.quotation_filter.quotation_spec_evaluator import specification_evaluation_fingerprint
from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import rank_quotations_with_spec_scores
from backend_logic2.services import quotation_attachments
from backend_logic2.repositories import quotation_specification_cache
from backend_logic2.policies import repository as policies
from backend_logic2.workflow import process_commands as commands


def test_rebid_reuses_only_identical_semantic_inputs():
    rfq, quotation, evaluator = _rfq(), _review('SQ-1', 'Supplier', '100').quotation, SimpleNamespace(model_name='qwen')
    fingerprint = lambda r, q=quotation: specification_evaluation_fingerprint(r, q, evaluator)
    original = fingerprint(rfq)
    assert original == fingerprint(rfq.model_copy(update={'rfq_name': 'RFQ-NEW'}))
    assert original != fingerprint(rfq, quotation.model_copy(update={'notes': '선결제 필수'}))
    changed = rfq.model_copy(deep=True)
    changed.items[0].specifications = {'재질': 'PVC'}
    assert original != fingerprint(changed)
    evaluator.model_name = 'new-weights'
    assert original != fingerprint(rfq)


def test_cache_lookup_is_not_bound_to_current_rfq_and_rechecks_each_hash():
    connection = MagicMock()
    connection.__enter__.return_value.execute.return_value.fetchall.return_value = [
        {'quotation_id': 'SQ-OLD', 'input_hash': 'same', 'assessment_json': {'score': 80}},
        {'quotation_id': 'SQ-OTHER', 'input_hash': 'same', 'assessment_json': {'score': 99}},
    ]
    with patch.object(quotation_specification_cache, 'get_connection', return_value=connection):
        result = quotation_specification_cache.load_matching('RFQ-NEW', {'SQ-OLD': 'same', 'SQ-OTHER': 'different'}, 'qwen')
    assert result == {'SQ-OLD': {'score': 80}}
    sql, params = connection.__enter__.return_value.execute.call_args.args
    assert 'rfq_name = ' not in sql
    assert 'RFQ-NEW' not in params


@pytest.mark.parametrize('status', ['review_required', 'unknown', 'not_evaluated', 'clear'])
def test_terms_never_block_automatic_selection(status):
    """특약은 보여 주기만 한다.

    있다는 이유만으로 멈추면 인사말 한 줄에도 사람이 붙어 자동화가 사실상
    꺼진다. 점수와 진행은 가격·납기·규격·평가이력 기준대로 간다.
    """
    result = _result()
    result['ranking'][0].update(terms_review=status, terms_reason='선결제 확인 필요')
    decision = _evaluate(result=result)
    assert decision.should_proceed
    assert 'SUPPLIER_TERMS' not in {r['code'] for r in decision.blockers}


@pytest.mark.parametrize(('notes', 'review', 'reason', 'expected'), [
    ('', 'unknown', '', 'clear'),
    ('감사합니다', 'clear', '인사 문구', 'clear'),
    ('선결제 필수', 'review_required', '결제 조건 확인', 'review_required'),
    ('선결제 필수', 'unknown', '', 'unknown'),
    ('선결제 필수', 'clear', '', 'clear'),
])
def test_terms_are_carried_through_without_touching_score_or_gates(notes, review, reason, expected):
    """판정값은 화면에 보여 주려고 그대로 들고 간다. 점수도 게이트도 안 건드린다."""
    quote = _review('SQ-1', 'Supplier', '100')
    quote.quotation.notes = notes
    assessment = _assessment('SQ-1', 80).model_copy(update={'terms_review': review, 'terms_reason': reason})
    ranked = rank_quotations_with_spec_scores([quote], _rfq(), {'SQ-1': assessment}).recommended[0]
    assert ranked.terms_review == expected
    assert ranked.specification_score == 80
    assert not ranked.requires_confirmation


def test_separated_terms_are_shown_rather_than_judged():
    """분리 저장된 특약은 규격 AI 입력에서 뺀 조항이라 판정 대상이 아니다."""
    quote = _review('SQ-1', 'Supplier', '100')
    quote.quotation.notes = '설치공사 별도'
    quote.quotation.content_sections_separated = True
    assessment = _assessment('SQ-1', 80).model_copy(update={'terms_review': 'clear', 'terms_reason': ''})
    ranked = rank_quotations_with_spec_scores([quote], _rfq(), {'SQ-1': assessment}).recommended[0]
    assert ranked.terms_review == 'not_evaluated'
    assert ranked.specification_score == 80
    assert not ranked.requires_confirmation


def test_paused_case_does_not_mutate_shared_company_policy():
    row = {'policy': {'rules': {'automation_mode': 'on'}}, 'automation_paused': True}
    conn = MagicMock()
    conn.__enter__.return_value.execute.return_value.fetchone.return_value = row
    with patch.object(policies, 'get_connection', return_value=conn):
        result = policies.for_case('CASE-1')
    assert result['policy']['rules']['automation_mode'] == 'off'
    assert row['policy']['rules']['automation_mode'] == 'on'


@pytest.mark.parametrize('cancelled', [True, False])
def test_urgent_decisions_write_visible_logs(cancelled):
    info = {'ITEM': {'needs_bidding': False, 'direct_supplier': 'Supplier', 'last_rate': 100,
                     'reference_po': 'PO-1', 'reasons': ['긴급발주']}}
    with patch('backend_logic2.nodes.mr.decide_bidding.decide_bidding', return_value=info), \
         patch.object(commands, '_cancel_urgent_mr_without_supplier', return_value='긴급 구매 불가' if cancelled else None), \
         patch('backend_logic2.nodes.supplier.tools.case_logging.log_ai_decision') as log:
        result = commands.decide_bidding_choice_command({'case_id': 'CASE', 'mr_name': 'MR'})
    assert log.call_args.args[1] == ('urgent_purchase_cancelled' if cancelled else 'direct_purchase_decision')
    assert result.update['status'] == ('urgent_no_supplier_cancelled' if cancelled else 'supplier_selected')


def test_email_originals_require_same_case_rfq_and_received_communication():
    case = {'workflow_snapshot': {'values': {'rfq_name': 'RFQ-1'}}}
    conn = MagicMock()
    conn.__enter__.return_value.execute.return_value.fetchone.return_value = {'communication_name': 'MAIL', 'rfq_name': 'RFQ-1'}
    documents = {'Supplier Quotation': {'items': [{'request_for_quotation': 'RFQ-1'}]},
                 'Communication': {'reference_doctype': 'Request for Quotation', 'reference_name': 'RFQ-1', 'sent_or_received': 'Received'}}
    with patch.object(quotation_attachments, 'get_connection', return_value=conn), \
         patch.object(quotation_attachments, 'erp_get_one', side_effect=lambda doctype, name: documents[doctype]), \
         patch.object(quotation_attachments, 'erp_get', return_value=[]), \
         patch.object(quotation_attachments, 'erp_get_communication_attachments', return_value=[{'name': 'FILE', 'file_name': 'quote.pdf'}]):
        assert quotation_attachments.originals(case, 'SQ')[0]['attached_to_name'] == 'MAIL'
        documents['Communication']['reference_name'] = 'OTHER'
        assert quotation_attachments.originals(case, 'SQ') == []
        documents['Supplier Quotation']['items'][0]['request_for_quotation'] = 'OTHER'
        with pytest.raises(PermissionError):
            quotation_attachments.originals(case, 'SQ')


def test_attachment_download_rejects_unrelated_file_before_download():
    from backend_logic2.api import procurement_routes as routes
    with patch.object(routes, '_require_case_access', return_value={}), \
         patch.object(quotation_attachments, 'originals', return_value=[]), \
         patch.object(routes, 'erp_download_file') as download:
        with pytest.raises(HTTPException) as exc:
            routes.download_quotation_attachment('CASE', {}, 'SQ', 'FOREIGN-FILE')
    assert exc.value.status_code == 403
    download.assert_not_called()


def test_authorized_original_download_returns_bytes_not_private_erp_url():
    from backend_logic2.api import procurement_routes as routes
    file = {'name': 'FILE', 'attached_to_doctype': 'Communication', 'attached_to_name': 'MAIL'}
    with patch.object(routes, '_require_case_access', return_value={}), \
         patch.object(quotation_attachments, 'originals', return_value=[file]), \
         patch.object(routes, 'erp_download_file', return_value={
             'document': {**file, 'file_name': '견적.pdf'}, 'content': b'original-pdf', 'content_type': 'application/pdf',
         }):
        response = routes.download_quotation_attachment('CASE', {}, 'SQ', 'FILE')
    assert response.body == b'original-pdf'
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['content-disposition'].startswith('attachment;')


def test_stop_api_cannot_claim_success_when_graph_is_already_running():
    from backend_logic2.api import procurement_routes as routes
    from backend_logic2.services import graph_worker
    with patch.object(routes, '_require_case_access', return_value={}), \
         patch.object(graph_worker, 'case_lock', side_effect=graph_worker.CaseBusy('CASE')), \
         patch.object(routes.case_repository, 'pause_automation') as pause:
        with pytest.raises(HTTPException) as exc:
            routes.pause_case_automation('CASE', {})
    assert exc.value.status_code == 409
    pause.assert_not_called()


def test_stop_api_checks_access_before_changing_anything():
    from backend_logic2.api import procurement_routes as routes
    with patch.object(routes, '_require_case_access', side_effect=HTTPException(403, 'denied')), \
         patch.object(routes.case_repository, 'pause_automation') as pause:
        with pytest.raises(HTTPException) as exc:
            routes.pause_case_automation('CASE', {})
    assert exc.value.status_code == 403
    pause.assert_not_called()


def test_pr_log_records_actual_returned_status_without_sending_mail():
    with patch('backend_logic2.integrations.erp_client.erp_get_one', return_value={'email_id': 'supplier@example.test'}), \
         patch('backend_logic2.pr.service.create_and_send_pr', return_value={'pr_id': 'PR', 'status': 'SENT'}), \
         patch('backend_logic2.nodes.supplier.tools.case_logging.log_ai_decision') as log:
        result = commands.create_pr_command({'case_id': 'CASE', 'mr_name': 'MR', 'selected_supplier': 'Supplier'})
    assert log.call_args.args[1] == 'pr_request_created'
    assert 'SENT' in log.call_args.args[2]
    assert result.goto == 'await_supplier_pr_response'
