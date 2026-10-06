"""Real user phrasing, real canonical statuses, and displayed count semantics."""
from unittest.mock import patch
from datetime import date, timedelta, datetime, timezone
import pytest

from backend_logic2.assistant.adapters.postgres_procurement_query import PostgresProcurementQuery
from backend_logic2.assistant.models import AssistantMessageRequest, AssistantPlan, CaseQueryFilters, FeatureMatch
from backend_logic2.assistant.service import AssistantService
from backend_logic2.assistant.query_routing import business_today
from test_assistant import FakeModel, FakeFreshness, FakeCatalog, FakeHelp
from test_assistant_waiting_queries import row


def service(model=None, catalog=None):
    return AssistantService(feature_catalog=catalog or FakeCatalog(), help_knowledge=FakeHelp(),
                            procurement_query=PostgresProcurementQuery(), freshness=FakeFreshness(), model=model or FakeModel())


@pytest.mark.parametrize('stored', ['무선마우스','무선 마우스','무선  마우스','무선\t마우스'])
@pytest.mark.parametrize('keyword', ['무선 마우스','무선마우스'])
def test_spacing_is_presentation_not_a_different_item(stored,keyword):
    rows=[row(item_name=stored),row(reference='wired',item_name='유선마우스')]
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=rows):
        results=PostgresProcurementQuery().query_cases(CaseQueryFilters(keyword=keyword),actor='buyer')
    assert [r.reference for r in results]==['MAT-MR-2026-00196']


def test_search_reads_canonical_fields_without_joining_field_boundaries():
    rows=[dict(row(reference='canonical'),summary={},item_name='무선마우스'),row(reference='split',item_name='무선',description='마우스')]
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=rows):
        result=PostgresProcurementQuery().query_cases(CaseQueryFilters(keyword='무선 마우스'),actor='buyer')
    assert [r.reference for r in result]==['canonical']


@pytest.mark.parametrize('message', ['무선 마우스 구매 작업이 몇개나 있지?', '무선마우스 구매 작업이 몇 건 있어?', '무선 마우스 관련 MR 개수 알려줘'])
def test_user_count_question_scans_all_pages_and_labels_only_open_count(message):
    rows=[row(reference=f'MAT-MR-2026-{n:05}',item_name='무선마우스') for n in range(201)]
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',side_effect=[rows[:200],rows[200:]]) as listing:
        response=service().answer(AssistantMessageRequest(message=message),current_user={'erp_user_id':'buyer'})
    assert response.meta['total_count']==201
    assert len(response.records)==10
    assert '총 201건' in response.answer and '제외한' in response.answer
    assert [call.kwargs['offset'] for call in listing.call_args_list]==[0,200]
    assert all(call.kwargs['assigned_user_id']=='buyer' for call in listing.call_args_list)


def test_truncated_count_fails_explicitly_instead_of_claiming_ten():
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=[row(item_name='무선마우스')]*200):
        response=service().answer(AssistantMessageRequest(message='무선 마우스 구매 작업이 몇개나 있지?'),current_user={'erp_user_id':'buyer'})
    assert not response.meta['query_available']
    assert response.meta['total_count'] is None and not response.records
    assert '찾지 못했습니다' not in response.answer


class LeakedPreviousFilter(FakeModel):
    available=True
    def plan(self, **kwargs):
        return AssistantPlan(intent='case_query', query='승인 대기', filters=CaseQueryFilters(keyword='무선 마우스',status='WAITING_INPUT',stage='MR_REVIEW'))
    def compose(self, **kwargs):
        raise AssertionError('Do not rewrite query evidence with a model')


@pytest.mark.parametrize('model',[FakeModel,LeakedPreviousFilter])
@pytest.mark.parametrize('question,expected',[
    ('승인 대기중인 MR 보여줄래?', {'review','po'}),
    ('승인대기중인 MR 보여줄래?', {'review','po'}),
    ('MR 승인 대기 보여줘', {'review'}),
    ('PO 승인 대기 보여줘', {'po'}),
])
def test_approval_is_not_every_human_or_supplier_wait(question,expected,model):
    rows=[row('MR_REVIEW','AWAITING_MR_REVIEW','review',item_name='모니터'),
          row('PRE_PO_APPROVAL','WAITING_INPUT','po',item_name='모니터'),
          row('QUOTATION_COLLECTION','WAITING_INPUT','quote'),
          row('RFQ_TARGET_SELECTION','WAITING_INPUT','rfq'),
          row('MR_REVIEW','REJECTED','rejected')]
    request=AssistantMessageRequest(message=question,conversation=[
        {'role':'user','content':'무선 마우스 구매 작업이 몇개나 있지?'},
        {'role':'assistant','content':'무선마우스 구매 작업은 5건입니다.'}])
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=rows):
        response=service(model()).answer(request,current_user={'erp_user_id':'buyer'})
    assert {r.reference for r in response.records}==expected
    assert response.meta['applied_filters']['keyword'] is None
    if 'review' in expected:
        assert next(r for r in response.records if r.reference=='review').status_label=='구매 요청 승인 대기'


class IrrelevantCatalog:
    def search(self,*args,**kwargs):
        return [FeatureMatch(id='runpod-worker',title='RunPod 워커 관리',summary='비용이 발생합니다.',target='company-policy',keywords=[],steps=[])]


@pytest.mark.parametrize('message,target',[
    ('무선 마우스 구매 작업이 몇개나 있지?','mr-list'),
    ('승인 대기중인 MR 보여줄래?','dashboard'),
    ('견적 회신 대기 작업 보여줘','vendor-select'),
    ('PO 승인 대기 보여줘','po-manage'),
])
def test_empty_result_never_uses_unrelated_feature_navigation(message,target):
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=[]):
        response=service(catalog=IrrelevantCatalog()).answer(AssistantMessageRequest(message=message),current_user={'erp_user_id':'buyer'})
    assert response.actions[0].target==target
    assert 'RunPod' not in str(response.model_dump())
    if '승인 대기중인' in message:
        assert 'RFQ 대상 선택이나 외부 회신 대기는 별도' in response.answer


class MissingDateFilter(FakeModel):
    available=True
    def plan(self, **kwargs):
        return AssistantPlan(intent='case_query',query=kwargs['message'],filters=CaseQueryFilters(keyword='납기가 가까운 항목'))
    def compose(self, **kwargs):
        raise AssertionError('Query answers cannot be rewritten')


@pytest.mark.parametrize('model',[FakeModel,MissingDateFilter])
@pytest.mark.parametrize('question,days',[
    ('납기가 가까운 항목 찾아줘',7),('납기 임박 MR 보여줘',7),
    ('3일 이내 납기인 MR 보여줘',3),('0일 이내 납기인 MR 보여줘',0),
])
def test_near_deadline_has_explicit_window_and_dates_not_all_open_jobs(question,days,model):
    today=business_today()
    rows=[row(reference=str(n),schedule_date=(today+timedelta(days=n)).isoformat()) for n in (-1,0,2,7,30)]
    rows.append(row(reference='unknown-date'))
    with patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases',return_value=rows):
        response=service(model()).answer(AssistantMessageRequest(message=question),current_user={'erp_user_id':'buyer'})
    assert {r.reference for r in response.records}=={str(n) for n in (0,2,7) if n<=days}
    assert response.meta['applied_filters']['due_within_days']==days
    assert f'{days}일 이내' in response.answer and '납기순' in response.answer
    assert str(today) in response.answer
    assert all(r.schedule_date for r in response.records)


def test_business_date_does_not_follow_servers_utc_midnight():
    with patch('backend_logic2.assistant.query_routing.datetime') as clock:
        clock.now.side_effect=lambda tz:datetime(2026,10,5,16,0,tzinfo=timezone.utc).astimezone(tz)
        assert business_today()==date(2026,10,6)
