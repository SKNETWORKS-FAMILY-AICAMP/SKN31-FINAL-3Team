"""Assistant-only regressions. All operational storage and model calls are fake."""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from backend_logic2.assistant.models import AssistantMessageRequest, AssistantPlan, CaseQueryFilters, ModelAnswer
from backend_logic2.assistant.service import AssistantService, _heuristic_plan
from backend_logic2.assistant.adapters.postgres_procurement_query import PostgresProcurementQuery
from backend_logic2.assistant.adapters.json_feature_catalog import JsonFeatureCatalog
from backend_logic2.assistant.adapters.sqlite_help_knowledge import SQLiteHelpKnowledge
from backend_logic2.assistant.query_routing import WAITING_STAGES, item_keyword
from test_assistant import FakeModel, FakeFreshness, FakeCatalog, FakeHelp

DATA=Path(__file__).resolve().parents[1]/'backend_logic2/assistant/data'


def row(stage='PR_RESPONSE_WAITING', status='WAITING_INPUT', reference='MAT-MR-2026-00196', **summary):
    return dict(case_id=reference, mr_name=reference, stage=stage, status=status,
                summary={'item_name':'무선 마우스', **summary})


class WrongModel(FakeModel):
    available=True
    def plan(self, **kwargs):
        return AssistantPlan(intent='case_query',query=kwargs['message'],filters=CaseQueryFilters(keyword='외부 진행 대기',status='WAITING_INPUT',stage='MR_REVIEW'))
    def compose(self, **kwargs):
        raise AssertionError('An LLM must not rewrite grounded case counts')


@pytest.mark.parametrize('message,group', [
    ('외부 진행 대기중인 작업이 있어?', 'external'),
    ('외부 승인 대기중인 게 있나요?', 'external'),
    ('외부 응답 대기 중인 작업을 보여줘', 'external'),
    ('외부대기 MR 있어?', 'external'),
    ('공급사 수주 확인 응답 대기 중인 MR 있어?', 'supplier_confirmation'),
    ('PR 응답 대기중인 건 있어?', 'supplier_confirmation'),
    ('견적 회신 대기 작업 보여줘', 'quotation'),
    ('요청자 대체품 선택 대기 있어?', 'requester'),
    ('입고 대기 중인 작업 보여줘', 'delivery'),
    ('PO 승인 대기중인 작업이 있어?', 'po_approval'),
])
@pytest.mark.parametrize('model', [FakeModel,WrongModel])
def test_waiting_synonyms_override_wrong_keyword_and_model_outage(message,group,model):
    stage=sorted(WAITING_STAGES[group])[0]
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=[row(stage)]):
        service=AssistantService(feature_catalog=FakeCatalog(),help_knowledge=FakeHelp(),procurement_query=PostgresProcurementQuery(),freshness=FakeFreshness(),model=model())
        response=service.answer(AssistantMessageRequest(message=message),current_user={'erp_user_id':'buyer@example.com'})
    assert response.records and response.records[0].reference=='MAT-MR-2026-00196'
    assert response.meta['applied_filters']['waiting_for']==group
    assert response.meta['applied_filters']['keyword'] is None
    assert response.meta['query_executed'] and response.meta['query_available']
    assert response.source=='deterministic'
    assert '없습니다' not in response.answer


@pytest.mark.parametrize('stage,status,expected', [
    ('SUBSTITUTE_DECISION','WAITING_INPUT',True),('QUOTATION_COLLECTION','WAITING_INPUT',True),
    ('PR_RESPONSE_WAITING','WAITING_INPUT',True),('DELIVERY','RUNNING',True),
    ('DELIVERY','PENDING',True),('PRE_PO_APPROVAL','WAITING_INPUT',False),
    ('RFQ_TARGET_SELECTION','WAITING_INPUT',False),('SUPPLIER_SELECTION','WAITING_INPUT',False),
    ('PR_RESPONSE_WAITING','FAILED',False),('PR_RESPONSE_WAITING','COMPLETED',False),
])
def test_external_is_not_every_waiting_input(stage,status,expected):
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=[row(stage,status)]):
        records=PostgresProcurementQuery().query_cases(CaseQueryFilters(waiting_for='external'),actor='buyer@example.com')
    assert bool(records)==expected


@pytest.mark.parametrize('actor,assigned', [('buyer@example.com','buyer@example.com'),('Administrator',None)])
def test_scope_preserved_on_each_page(actor,assigned):
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',side_effect=[[row('MR_REVIEW')]*200,[row()]]) as listing:
        records=PostgresProcurementQuery().query_cases(CaseQueryFilters(waiting_for='external'),actor=actor)
    assert len(records)==1
    assert [c.kwargs['offset'] for c in listing.call_args_list]==[0,200]
    assert all(c.kwargs['assigned_user_id']==assigned for c in listing.call_args_list)


def test_bounded_search_reports_incomplete_instead_of_false_zero():
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=[row('MR_REVIEW')]*200):
        with pytest.raises(RuntimeError,match='coverage limit'):
            PostgresProcurementQuery().query_cases(CaseQueryFilters(waiting_for='external'),actor='buyer@example.com')


def test_product_keyword_and_other_filters_still_apply():
    assert item_keyword('무선 마우스 외부 진행 대기','무선 마우스 외부 진행 대기 있어?')=='무선 마우스'
    assert item_keyword('건전지','건전지 외부 대기 있어?')=='건전지'
    today=date.today()
    rows=[row(schedule_date=str(today+timedelta(days=2)),attachments=['quote.pdf']),row(reference='later',schedule_date=str(today+timedelta(days=9)),attachments=['quote.pdf']),row(reference='no-file',schedule_date=str(today))]
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=rows):
        records=PostgresProcurementQuery().query_cases(CaseQueryFilters(waiting_for='external',keyword='마우스',due_within_days=3,has_attachments=True),actor='buyer@example.com')
    assert [r.reference for r in records]==['MAT-MR-2026-00196']


def test_exact_reference_ignores_llm_invented_filters_and_includes_terminal():
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=[row('COMPLETED','COMPLETED')]) as listing:
        service=AssistantService(feature_catalog=FakeCatalog(),help_knowledge=FakeHelp(),procurement_query=PostgresProcurementQuery(),freshness=FakeFreshness(),model=WrongModel())
        response=service.answer(AssistantMessageRequest(message='MAT-MR-2026-00196 현재 상태'),current_user={'erp_user_id':'buyer@example.com'})
    assert response.records[0].status=='COMPLETED'
    assert listing.call_args.kwargs['include_closed']
    assert response.actions[0].search_query=='MAT-MR-2026-00196'


@pytest.mark.parametrize('failure', [False,True])
def test_empty_and_database_error_have_different_answers(failure):
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',side_effect=RuntimeError('offline') if failure else None,return_value=[]):
        service=AssistantService(feature_catalog=FakeCatalog(),help_knowledge=FakeHelp(),procurement_query=PostgresProcurementQuery(),freshness=FakeFreshness(),model=WrongModel())
        response=service.answer(AssistantMessageRequest(message='외부 진행 대기중인 작업이 있어?'),current_user={'erp_user_id':'buyer@example.com'})
    assert response.meta['query_available'] is (not failure)
    assert ('연결할 수 없습니다' in response.answer) is failure
    assert ('담당 범위' in response.answer) is (not failure)


@pytest.mark.parametrize('message', ['외부 응답 대기 사용법','대체품 대기 중일 때 어떻게 해?','PO 승인 대기 화면은 어디?'])
def test_waiting_help_does_not_turn_into_live_query(message):
    assert _heuristic_plan(message).intent=='help'


FEATURES=json.loads((DATA/'features.json').read_text(encoding='utf-8'))
HELP=json.loads((DATA/'help_articles.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('entry',FEATURES,ids=lambda e:e['id'])
def test_every_feature_is_retrievable(entry):
    matches=JsonFeatureCatalog(DATA/'features.json').search(entry['title'],limit=3)
    assert any(m.id==entry['id'] and m.target==entry['target'] for m in matches)


@pytest.mark.parametrize('entry',HELP,ids=lambda e:e['id'])
def test_every_help_article_is_retrievable(entry,tmp_path):
    matches=SQLiteHelpKnowledge(DATA/'help_articles.json',tmp_path/'help.sqlite3').search(entry['title'],limit=4)
    assert any(m.id==entry['id'] and m.target==entry['target'] for m in matches)


def test_authentication_and_disabled_api_remain_enforced():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth_service.dependencies import require_authenticated_user
    from backend_logic2.assistant import api
    app=FastAPI();app.include_router(api.router)
    with TestClient(app) as client:
        assert client.post('/api/assistant/messages',json={'message':'외부 진행 대기 있어?'}).status_code==401
        assert client.get('/api/assistant/capabilities').status_code==401
        app.dependency_overrides[require_authenticated_user]=lambda:{'erp_user_id':'buyer@example.com'}
        with patch.object(api,'assistant_enabled',return_value=False):
            assert client.post('/api/assistant/messages',json={'message':'외부 진행 대기 있어?'}).status_code==503
