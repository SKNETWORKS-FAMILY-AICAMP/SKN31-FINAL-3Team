"""Read-only presentation matches the dashboard's stored auto-progress verdict."""
from unittest.mock import patch

import pytest

from backend_logic2.assistant.adapters.postgres_procurement_query import PostgresProcurementQuery
from backend_logic2.assistant.models import CaseQueryFilters
from backend_logic2.assistant.query_routing import auto_progress_blockers, matches_waiting


def case(checks):
    return {
        'mr_name': 'MAT-MR-2026-00211', 'stage': 'QUOTATION_COLLECTION',
        'status': 'WAITING_INPUT', 'summary': {'item_name': '테스트 마우스'},
        'workflow_snapshot': {'values': {'quotation_ranking_meta': {
            'auto_progress': {'checks': checks},
        }}},
    }


def test_buyer_decision_is_not_an_external_reply_wait():
    held = case([{'status': 'blocked', 'detail': '비교 가능한 견적이 부족합니다.'}])
    external = dict(case([]), stage='PR_RESPONSE_WAITING', mr_name='MAT-MR-2026-00196')
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases', return_value=[held, external]):
        records = PostgresProcurementQuery().query_cases(CaseQueryFilters(waiting_for='external', count_requested=True), actor='buyer@example.com')
        assert records.total_count == 1
        assert [r.reference for r in records] == ['MAT-MR-2026-00196']
        # Exact/stage lookup still returns the held case, with a useful reason.
        details = PostgresProcurementQuery().query_cases(CaseQueryFilters(exact_reference=held['mr_name']), actor='buyer@example.com')
    assert details[0].waiting_on == '구매 담당자'
    assert '견적이 부족' in details[0].next_action
    assert details[0].target == 'vendor-select'


@pytest.mark.parametrize('checks', [None, {}, 'blocked', [], [None, 'blocked'], [{'status': 'passed'}]])
def test_missing_malformed_or_passed_verdict_does_not_hide_external_wait(checks):
    assert matches_waiting(case(checks), 'external')


def test_failure_does_not_become_normal_human_decision():
    failed = dict(case([{'status': 'blocked'}]), status='FAILED')
    assert auto_progress_blockers(failed) == []
    assert not matches_waiting(failed, 'external')
