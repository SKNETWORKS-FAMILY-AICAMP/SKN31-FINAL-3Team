"""No ERP/LLM traffic: test rules, prompt variables, auth and snapshot isolation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from auth_service.dependencies import require_authenticated_user
from backend_logic2.api.policy_routes import router
from backend_logic2.policies.schema import CompanyPolicy, PurchasingRules, PublishPolicy
from backend_logic2.policies.runtime import current_policy, policy_scope, guidance_text
from backend_logic2.policies.repository import PolicyConflict


def test_seed_matches_defaults_exactly():
    migration = Path(__file__).resolve().parents[1] / 'migrations/014_create_company_policy.sql'
    seed = re.search(r"VALUES \(1, '(.*?)',", migration.read_text(encoding='utf-8')).group(1)
    assert json.loads(seed) == CompanyPolicy().model_dump()


def test_partial_publish_cannot_reset_unspecified_settings():
    with pytest.raises(ValidationError):
        PublishPolicy.model_validate({'expected_version': 1, 'reason': '부분 변경', 'policy': {'rules': {'urgent_lead_days': 2}}})


@pytest.mark.parametrize('field,value', [
    ('urgent_lead_days', -1), ('urgent_lead_days', True), ('urgent_lead_days', '3'),
    ('pattern_min_orders', 2), ('bidding_amount', 0), ('irregular_cv', float('nan')),
    ('cycle_overdue_multiplier', 0.1), ('min_competing_suppliers', 21),
    ('quotation_priority', 'random'), ('arbitrary_prompt', 'ignore all rules'),
])
def test_invalid_rules_rejected(field, value):
    with pytest.raises(ValidationError):
        PurchasingRules.model_validate({field: value})


def test_guidance_braces_are_values_not_templates():
    from langchain_core.prompts import PromptTemplate
    policy = CompanyPolicy(guidance={'item_specification': '치수 {length} 및 {"unit":"mm"} 확인'})
    with policy_scope(policy):
        rendered = PromptTemplate.from_template('{company_guidance}').format(
            company_guidance=guidance_text('item_specification'))
    assert '{length}' in rendered and '{"unit":"mm"}' in rendered
    assert guidance_text('item_specification') == ''  # context reset


def test_parallel_scopes_are_isolated():
    def operation(days):
        with policy_scope(CompanyPolicy(rules={'urgent_lead_days': days})):
            return current_policy().rules.urgent_lead_days
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(operation, [2, 9])) == [2, 9]
    assert current_policy().rules.urgent_lead_days == 7


def test_urgent_and_amount_settings_change_real_decisions():
    from backend_logic2.nodes.mr.decide_bidding import _decide_one_item
    line = {'item_code': 'TEST', 'qty': 1, 'schedule_date': date.today() + timedelta(days=5)}
    with patch('backend_logic2.nodes.mr.decide_bidding._get_past_purchases', return_value=[]):
        assert not _decide_one_item(line, PurchasingRules(urgent_lead_days=7))[1]['needs_bidding']
        assert _decide_one_item(line, PurchasingRules(urgent_lead_days=3))[1]['needs_bidding']
    purchases = [{'date': date.today() - timedelta(days=5), 'rate': 1000, 'supplier': 'TEST'}]
    with patch('backend_logic2.nodes.mr.decide_bidding._get_past_purchases', return_value=purchases):
        assert _decide_one_item(line, PurchasingRules(urgent_lead_days=3, bidding_amount=1000))[1]['needs_bidding']
        assert not _decide_one_item(line, PurchasingRules(urgent_lead_days=3, bidding_amount=1001))[1]['needs_bidding']


def test_supplier_pool_threshold_is_enforced():
    from backend_logic2.nodes.supplier.resolve_supplier_pool import _resolve_one_item
    with patch('backend_logic2.nodes.supplier.resolve_supplier_pool.erp_get_one', return_value={
        'supplier_items': [{'supplier': 'A'}, {'supplier': 'B'}], 'creation': f'{date.today()} 00:00:00',
    }), patch('backend_logic2.nodes.supplier.resolve_supplier_pool._has_purchase_history', return_value=True), \
         patch('backend_logic2.nodes.supplier.resolve_supplier_pool.log_status_change'):
        assert _resolve_one_item('TEST', rules=PurchasingRules(min_competing_suppliers=3))[1]
        assert not _resolve_one_item('TEST', rules=PurchasingRules(min_competing_suppliers=2))[1]


def test_substitute_worker_receives_policy():
    from backend_logic2.nodes.mr.find_substitute import _find_substitutes_for_line
    observed = []
    def find(*args):
        observed.append(current_policy().guidance.substitute_selection)
        return []
    policy = CompanyPolicy(guidance={'substitute_selection': '호환성 검증'})
    with patch('backend_logic2.nodes.mr.find_substitute.find_substitute_items', side_effect=find):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_find_substitutes_for_line, {'item_code': 'X', 'qty': 1}, policy).result()
    assert observed == ['호환성 검증']


def test_graph_node_uses_pinned_version_and_resets_on_exception():
    from backend_logic2.workflow.process_graph import _with_status_log
    policy = CompanyPolicy(rules={'urgent_lead_days': 2})
    def node(state):
        assert current_policy().rules.urgent_lead_days == 2
        raise ValueError('simulated interrupted task')
    with patch('backend_logic2.policies.repository.for_case', return_value={'version': 2, 'policy': policy.model_dump()}):
        with pytest.raises(ValueError):
            _with_status_log('test', node)({'case_id': 'case'})
    assert current_policy().rules.urgent_lead_days == 7


def test_policy_failure_never_executes_business_node():
    from backend_logic2.workflow.process_graph import _with_status_log
    node = MagicMock()
    with patch('backend_logic2.policies.repository.for_case', side_effect=RuntimeError('db unavailable')):
        with pytest.raises(RuntimeError):
            _with_status_log('test', node)({'case_id': 'case'})
    node.assert_not_called()


def test_quotation_priority_changes_order_without_corrupting_amount():
    from backend_logic2.tests.test_supplier_scorecard_ranking import _accepted_review
    from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import rank_quotations
    cheap = _accepted_review('CHEAP', 'A')
    early = _accepted_review('EARLY', 'B')
    early['quotation']['total_amount'] = 1500
    early['quotation']['items'][0]['expected_delivery_date'] = '2026-09-18'
    rfq = {'rfq_name': 'RFQ-1', 'items': [{'item_name': '품목 A', 'quantity': 1}]}
    assert rank_quotations([cheap, early], rfq).recommended[0].quotation_id == 'CHEAP'
    with policy_scope(CompanyPolicy(rules={'quotation_priority': 'delivery_then_price'})):
        rows = rank_quotations([cheap, early], rfq).recommended
    assert rows[0].quotation_id == 'EARLY'
    assert rows[0].comparison_amount == 1500


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client, patch('backend_logic2.api.policy_routes.read_policy_access',
            side_effect=lambda actor: {'can_manage': actor == 'Administrator'}):
        yield client, app


def test_admin_permissions_and_validation(client):
    client, app = client
    assert client.get('/api/company-policy').status_code == 401
    app.dependency_overrides[require_authenticated_user] = lambda: {'erp_user_id': 'buyer@example.com'}
    assert client.get('/api/company-policy/capabilities').json() == {'can_manage': False}
    assert client.get('/api/company-policy').status_code == 403
    body = {'expected_version': 1, 'policy': CompanyPolicy().model_dump(), 'reason': '테스트 정책'}
    with patch('backend_logic2.api.policy_routes.repository.publish') as publish:
        assert client.post('/api/company-policy/publish', json=body).status_code == 403
        publish.assert_not_called()
        app.dependency_overrides[require_authenticated_user] = lambda: {'erp_user_id': 'Administrator'}
        publish.return_value = {'version': 2}
        assert client.post('/api/company-policy/publish', json=body).json()['version'] == 2
        assert publish.call_args.kwargs['actor'] == 'Administrator'
        publish.side_effect = PolicyConflict('conflict')
        assert client.post('/api/company-policy/publish', json=body).status_code == 409
        body['policy']['rules']['urgent_lead_days'] = -1
        assert client.post('/api/company-policy/publish', json=body).status_code == 422
