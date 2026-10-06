"""Isolated assistant graph: context, scope, bounded calls, no purchase writes."""
from unittest.mock import patch

import pytest

from backend_logic2.assistant.models import AssistantMessageRequest, AssistantPlan, CaseQueryFilters, DialogueContext
from backend_logic2.assistant.service import AssistantService
from backend_logic2.assistant.adapters.postgres_procurement_query import PostgresProcurementQuery
from test_assistant import FakeCatalog, FakeHelp, FakeModel, FakeFreshness
from test_assistant_waiting_queries import row


PATH = 'backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases'


class TrackedModel(FakeModel):
    def __init__(self):
        self.plans, self.composes = [], []

    def plan(self, **kwargs):
        self.plans.append(kwargs)
        return None

    def compose(self, **kwargs):
        assert not kwargs['records'], 'Private query rows must not reach a model'
        self.composes.append(kwargs)
        return None


def service(model=None):
    return AssistantService(feature_catalog=FakeCatalog(), help_knowledge=FakeHelp(),
        procurement_query=PostgresProcurementQuery(), freshness=FakeFreshness(), model=model or TrackedModel())


def turn(svc, message, previous=None, actor='buyer'):
    return svc.answer(AssistantMessageRequest(message=message, dialogue=previous), current_user={'erp_user_id': actor})


@pytest.mark.parametrize('question', ['그중 승인 대기만 보여줘', '그 중 승인 대기 항목은 몇 건이야?'])
def test_subset_preserves_item_and_changes_waiting_group(question):
    svc = service()
    with patch(PATH, return_value=[]):
        first = turn(svc, '무선 마우스 구매 작업이 몇 건이야?')
        second = turn(svc, question, first.dialogue)
    assert second.dialogue.filters.keyword == '무선 마우스'
    assert second.dialogue.filters.waiting_for == 'approval'
    assert second.dialogue.filters.count_requested == ('몇 건' in question)


def test_new_topic_does_not_inherit_item_or_offset():
    ctx = DialogueContext(filters=CaseQueryFilters(keyword='마우스', offset=20))
    with patch(PATH, return_value=[]):
        response = turn(service(), '외부 응답 대기 작업 보여줘', ctx)
    assert response.dialogue.filters.keyword is None
    assert response.dialogue.filters.offset == 0
    assert response.dialogue.filters.waiting_for == 'external'


@pytest.mark.parametrize('question', ['첫 번째 건 알려줘', '1번 현재 단계는?', '이 단계에서 무엇을 확인해야 해?', '관련 화면으로 이동'])
def test_selected_reference_is_reauthorized_on_every_turn(question):
    ctx = DialogueContext(references=['MAT-MR-2026-00196'])
    with patch(PATH, return_value=[row()]) as listing:
        response = turn(service(), question, ctx, actor='different-user')
    assert response.dialogue.filters.exact_reference == 'MAT-MR-2026-00196'
    assert listing.call_args.kwargs['assigned_user_id'] == 'different-user'
    assert response.actions[0].highlight_reference == 'MAT-MR-2026-00196'


@pytest.mark.parametrize('question', ['그중 승인 대기만', '다음 10건 보여줘', '첫 번째 건 알려줘', '그거 알려줘'])
def test_missing_context_clarifies_without_db_or_model(question):
    model = TrackedModel()
    with patch(PATH) as listing:
        response = turn(service(model), question)
    assert response.intent == 'clarification'
    listing.assert_not_called()
    assert not model.plans and not model.composes


def test_ambiguous_reference_does_not_pick_random_record():
    ctx = DialogueContext(references=['MAT-MR-2026-00196', 'MAT-MR-2026-00217'])
    with patch(PATH) as listing:
        result = turn(service(), '이거 왜 기다려?', ctx)
    assert result.intent == 'clarification'
    listing.assert_not_called()


def test_next_page_keeps_filters_and_all_ten_cards_navigate():
    rows = [row(reference=f'MAT-MR-2026-{n:05}', item_name='마우스') for n in range(25)]
    svc = service()
    with patch(PATH, return_value=rows):
        first = turn(svc, '마우스 구매 작업 보여줘')
        second = turn(svc, '다음 10건 보여줘', first.dialogue)
        third = turn(svc, '다음 10건 보여줘', second.dialogue)
    assert len(first.actions) == len(first.records) == 10
    assert first.meta['has_more'] and second.meta['has_more']
    assert not third.meta['has_more'] and len(third.records) == 5
    assert not set(r.reference for r in first.records) & set(r.reference for r in second.records)
    assert second.dialogue.filters.offset == 10


def test_due_refinement_uses_previous_item():
    ctx = DialogueContext(filters=CaseQueryFilters(keyword='무선 마우스', waiting_for='external'))
    with patch(PATH, return_value=[]):
        result = turn(service(), '그중 납기 3일 이내만', ctx)
    assert result.dialogue.filters.keyword == '무선 마우스'
    assert result.dialogue.filters.waiting_for == 'external'
    assert result.dialogue.filters.due_within_days == 3


def test_replayed_context_cannot_bypass_assignee():
    ctx = DialogueContext(filters=CaseQueryFilters(include_closed=True), references=['MAT-MR-2026-00196'])
    with patch(PATH, return_value=[]) as listing:
        response = turn(service(), '첫 번째 건 알려줘', ctx, actor='unauthorized-user')
    assert not response.records
    assert listing.call_args.kwargs['assigned_user_id'] == 'unauthorized-user'


def test_private_answers_never_echo_into_planner():
    model = TrackedModel()
    with patch(PATH, return_value=[]):
        service(model).answer(AssistantMessageRequest(message='승인 대기 작업 보여줘', conversation=[
            {'role': 'assistant', 'content': 'PRIVATE CONTRACT PRICE'},
            {'role': 'user', 'content': '승인 대기 작업 보여줘'},
        ]), current_user={'id': 'buyer'})
    assert len(model.plans) == 1 and not model.composes
    assert 'PRIVATE' not in str(model.plans)


def test_unknown_model_filter_clarifies_without_query():
    class BadModel(FakeModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', filters=CaseQueryFilters(stage='MADE_UP'))
    with patch(PATH) as listing:
        response = turn(service(BadModel()), '테스트 작업 알려줘')
    assert response.intent == 'clarification'
    listing.assert_not_called()


@pytest.mark.parametrize('question', ['PO 승인해줘', 'RFQ 발송해줘', '구매 요청 삭제해줘'])
def test_mutation_only_guides_without_db(question):
    with patch(PATH) as listing:
        response = turn(service(), question)
    listing.assert_not_called()
    assert response.intent == 'feature_guide'
    assert all(action.type in {'navigate', 'navigate_with_filters'} for action in response.actions)


def test_slots_reject_excess_work_without_queueing_on_purchase_graph():
    svc = service()
    svc._slots.acquire(); svc._slots.acquire()
    try:
        response = turn(svc, '승인 대기 작업 보여줘')
    finally:
        svc._slots.release(); svc._slots.release()
    assert response.meta['busy']


def test_database_failure_preserves_context_and_does_not_say_zero():
    ctx = DialogueContext(filters=CaseQueryFilters(keyword='마우스'))
    with patch(PATH, side_effect=RuntimeError('offline')):
        response = turn(service(), '그중 승인 대기만 보여줘', ctx)
    assert not response.meta['query_available']
    assert response.dialogue.filters == ctx.filters
    assert response.dialogue.references == ctx.references
    assert response.dialogue.memory.recent[-1].question == '그중 승인 대기만 보여줘'
    assert '찾지 못했습니다' not in response.answer


def test_graph_has_no_cycles_checkpointer_or_purchase_nodes():
    graph = service().graph
    assert graph.checkpointer is None
    assert set(graph.get_graph().nodes) == {'__start__', '__end__', 'resolve_context', 'plan_and_validate', 'authorized_read', 'grounded_response', 'clarify', 'guide_lookup', 'remember_context'}
