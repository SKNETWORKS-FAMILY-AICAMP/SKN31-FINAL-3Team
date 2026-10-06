"""User-goal regressions: not merely testing a SQL filter we chose ourselves."""
from unittest.mock import patch
import pytest
from backend_logic2.assistant.models import AssistantPlan, CaseQueryFilters, AssistantMessageRequest, DialogueContext
from backend_logic2.assistant.memory import planner_memory, restore_memory
from test_assistant_graph import service, turn, PATH, TrackedModel
from test_assistant_waiting_queries import row
from test_assistant_sessions import client_store


def test_attachment_condition_is_not_a_literal_item_keyword():
    class AttachmentModel(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', filters=CaseQueryFilters(has_attachments=True))
    entry = row('MR_REVIEW', 'AWAITING_MR_REVIEW')
    entry['summary'] = {'item_name':'무선 마우스', 'attachments':[{'file_name':'test.txt'}]}
    with patch(PATH, return_value=[entry]):
        result = turn(service(AttachmentModel()), '첨부파일 있는 구매 요청만 보여줘')
    assert result.dialogue.filters.keyword is None
    assert len(result.records) == 1


def test_unsupported_question_does_not_offer_unrelated_keyword_shortcut():
    class UnsupportedModel(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='unsupported', capability='unsupported', query='회사 구매 정책')
    result = turn(service(UnsupportedModel()), '회사 통장 잔액 얼마야?')
    assert result.actions == []


@pytest.mark.parametrize('question', ['내 승인을 기다리는 작업이 몇 개야?', '내가 승인할 건 있어?', '제가 승인해야 하는 일 좀 보여줘'])
def test_personal_approval_asks_meaning_before_claiming_zero(question):
    with patch(PATH) as listing:
        response = turn(service(), question)
    assert response.intent == 'clarification'
    assert response.dialogue.pending.kind == 'approval_meaning'
    assert response.followups == ['결재·승인만', '결정할 작업 전체']
    assert '0건' not in response.answer
    listing.assert_not_called()


@pytest.mark.parametrize('choice', ['2번', '결정할 작업 전체', '둘 다', '선택 업무도 포함'])
def test_clarification_choice_preserves_original_count_and_scope(choice):
    svc = service()
    first = turn(svc, '내 승인을 기다리는 작업이 몇 개야?')
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION'), row('PR_RESPONSE_WAITING', reference='external')]) as listing:
        result = turn(svc, choice, first.dialogue)
    assert result.meta['total_count'] == 1
    assert result.meta['applied_filters']['task_scope'] == 'actionable'
    assert result.dialogue.pending is None
    assert listing.call_args.kwargs['assigned_user_id'] == 'buyer'
    assert len(result.dialogue.memory.recent) == 2


def test_formal_approval_choice_checks_live_po_role_once():
    svc = service()
    first = turn(svc, '내 승인을 기다리는 작업이 몇 개야?')
    rows = [row('MR_REVIEW', 'AWAITING_MR_REVIEW'), row('PRE_PO_APPROVAL', reference='po1'), row('PRE_PO_APPROVAL', reference='po2')]
    with patch(PATH, return_value=rows), patch('backend_logic2.assistant.adapters.user_access.may_approve_po', return_value=False) as access:
        result = turn(svc, '결재·승인만', first.dialogue)
    assert result.meta['total_count'] == 1
    access.assert_called_once_with('buyer')


def test_permission_failure_is_not_a_false_zero():
    with patch(PATH, return_value=[row('PRE_PO_APPROVAL')]), patch('backend_logic2.assistant.adapters.user_access.may_approve_po', side_effect=RuntimeError('unavailable')):
        result = turn(service(), '내가 결정할 작업은 몇 건이야?')
    assert result.meta['query_available'] is False
    assert '0건' not in result.answer


@pytest.mark.parametrize('question', ['내가 처리할 일 있어?', '내 할 일 몇 개야?', '제가 확인할 작업 보여줘', '내가 결정할 작업 보여줘'])
def test_personal_tasks_exclude_external_waits(question):
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION'), row('QUOTATION_COLLECTION', reference='external'), row('DELIVERY', 'RUNNING', reference='delivery')]):
        response = turn(service(), question)
    assert len(response.records) == 1
    assert response.dialogue.filters.task_scope == 'actionable'


def test_assigned_admin_does_not_mean_all_visible_cases():
    with patch(PATH, return_value=[]) as listing:
        response = turn(service(), '내 담당 구매 작업은 몇 건이야?', actor='Administrator')
    assert response.dialogue.filters.task_scope == 'assigned'
    assert listing.call_args.kwargs['assigned_user_id'] == 'Administrator'


def test_semantic_paraphrase_selects_registered_capability():
    class Semantic(TrackedModel):
        def plan(self, **kwargs):
            assert kwargs['message'] == '나한테 공 넘어온 거 있어?'
            return AssistantPlan(intent='case_query', capability='my_tasks', filters=CaseQueryFilters(waiting_for='decision'))
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION')]):
        result = turn(service(Semantic()), '나한테 공 넘어온 거 있어?')
    assert result.meta['capability'] == 'my_tasks'
    assert len(result.records) == 1


def test_low_confidence_never_broadly_queries():
    class Uncertain(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', confidence='low', clarification_question='어떤 기한을 말씀하시나요?', choices=['납기', '견적 마감'])
    with patch(PATH) as listing:
        result = turn(service(Uncertain()), '급한 것만')
    listing.assert_not_called()
    assert result.intent == 'clarification'
    assert result.followups == ['납기', '견적 마감']


def test_semantic_refinement_keeps_prior_item_and_counts():
    class Semantic(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', capability='count_cases', context_mode='refine', filters=CaseQueryFilters(waiting_for='decision', task_scope='actionable'))
    prior = DialogueContext(filters=CaseQueryFilters(keyword='마우스'))
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION')]):
        result = turn(service(Semantic()), '아니 선택할 일까지 포함해서', prior)
    assert result.dialogue.filters.keyword == '마우스'
    assert result.meta['total_count'] == 1


def test_explicitly_removed_item_does_not_stick_to_followup():
    class Semantic(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', capability='count_cases', context_mode='refine', clear_filters=['keyword'])
    prior = DialogueContext(filters=CaseQueryFilters(keyword='모니터', waiting_for='external'))
    with patch(PATH, return_value=[row()]):
        result = turn(service(Semantic()), '품목은 상관없이 세어줘', prior)
    assert result.dialogue.filters.keyword is None
    assert result.dialogue.filters.waiting_for == 'external'
    assert result.meta['total_count'] == 1


def test_completed_context_survives_semantic_refinement():
    class Semantic(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', capability='count_cases', context_mode='refine')
    prior = DialogueContext(filters=CaseQueryFilters(status='COMPLETED', include_closed=True))
    with patch(PATH, return_value=[row('COMPLETED', 'COMPLETED')]) as listing:
        result = turn(service(Semantic()), '그래서 전부 몇 건이란 거야?', prior)
    assert listing.call_args.kwargs['include_closed'] is True
    assert result.meta['total_count'] == 1


def test_summary_recall_keyword_is_not_overwritten_by_sentence_prefix():
    class Recall(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', capability='count_cases', filters=CaseQueryFilters(keyword='무선 마우스'))
    with patch(PATH, return_value=[row()]):
        result = turn(service(Recall()), '처음 검색했던 품목으로 돌아가서 구매 작업 총 몇 개인지 알려줘')
    assert result.dialogue.filters.keyword == '무선 마우스'
    assert result.meta['total_count'] == 1


def test_my_external_wait_is_not_my_pending_decision():
    with patch(PATH, return_value=[row()]):
        result = turn(service(), '내가 외부 회신 기다리는 작업 보여줘')
    assert result.dialogue.filters.task_scope == 'assigned'
    assert len(result.records) == 1


def test_ninth_turn_compacts_old_intent_without_business_answers():
    svc, previous = service(), None
    with patch(PATH, return_value=[row(item_name='PRIVATE CONTRACT PRICE')]):
        for index in range(10):
            result = turn(svc, '무선 마우스 구매 작업 몇 건이야?', previous)
            previous = result.dialogue
    assert len(previous.memory.recent) == 8
    assert previous.memory.compacted_turns == 2
    assert previous.memory.summary
    assert 'PRIVATE CONTRACT PRICE' not in str(planner_memory(previous))
    assert '무선 마우스' in str(previous.memory.summary)


def test_new_topic_drops_filters_but_keeps_conversation_memory():
    svc = service()
    with patch(PATH, return_value=[]):
        first = turn(svc, '무선 마우스 구매 작업 몇 건이야?')
        second = turn(svc, '외부 응답 대기 작업 보여줘', first.dialogue)
    assert second.dialogue.filters.keyword is None
    assert len(second.dialogue.memory.recent) == 2
    assert len(first.dialogue.memory.recent) == 1  # No in-place mutation.


def test_legacy_session_restores_eight_turn_intents_not_private_text():
    messages = []
    for index in range(10):
        messages += [{'sender': 'user', 'text': f'질문 {index}'}, {'sender': 'agent', 'text': 'PRIVATE PRICE', 'response': {'intent': 'case_query'}}]
    dialogue = restore_memory({'dialogue': None, 'messages': messages})
    assert len(dialogue.memory.recent) == 8
    assert dialogue.memory.recent[0].question == '질문 2'
    assert 'PRIVATE PRICE' not in str(planner_memory(dialogue))


def test_unsupported_live_data_does_not_query_or_compose_facts():
    class Unsupported(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='unsupported', capability='unsupported')
    model = Unsupported()
    with patch(PATH) as listing:
        result = turn(service(model), '현금 잔액 알려줘')
    listing.assert_not_called()
    assert not model.composes
    assert '직접 조회할 수 없습니다' in result.answer


def test_chosen_approval_meaning_is_not_asked_again():
    svc = service()
    pending = turn(svc, '내 승인을 기다리는 작업 몇 개야?')
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION')]):
        selected = turn(svc, '결정할 작업 전체', pending.dialogue)
        again = turn(svc, '그럼 내 승인을 기다리는 건 몇 개?', selected.dialogue)
    assert again.meta['total_count'] == 1
    assert again.intent == 'case_query'


def test_followup_guide_uses_previous_screen_not_list_filters():
    with patch(PATH) as listing:
        previous = DialogueContext(guide_query='견적 요청 발송', guide_target='vendor-select')
        result = turn(service(), '거기서 어떻게 하면 돼?', previous)
    listing.assert_not_called()
    assert result.intent == 'help'


def test_model_cannot_invent_feature_id():
    class Invented(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='feature_guide', capability='feature_guide', feature_id='delete-everything')
    with patch(PATH) as listing:
        result = turn(service(Invented()), '설정 어디야?')
    assert result.intent == 'clarification'
    assert not result.actions
    listing.assert_not_called()


def test_unsupported_filter_is_not_silently_dropped():
    class UnsupportedCondition(TrackedModel):
        def plan(self, **kwargs):
            return AssistantPlan(intent='case_query', capability='count_cases', unhandled_conditions=['생성일=어제'])
    with patch(PATH) as listing:
        result = turn(service(UnsupportedCondition()), '어제 생성된 작업 몇 개야?')
    listing.assert_not_called()
    assert result.intent == 'clarification'


def test_full_dictionary_semantic_selection_drives_exact_navigation():
    from backend_logic2.assistant.adapters.json_feature_catalog import JsonFeatureCatalog
    from pathlib import Path
    class Semantic(TrackedModel):
        def plan(self, **kwargs):
            assert len(kwargs['feature_index']) == 27
            return AssistantPlan(intent='feature_guide', capability='feature_guide', feature_id='quotation-original', query='문서')
    svc = service(Semantic())
    svc.feature_catalog = JsonFeatureCatalog(Path(__file__).parents[1]/'backend_logic2/assistant/data/features.json')
    with patch(PATH) as listing:
        result = turn(svc, '업체가 보낸 종이를 다시 열고 싶어')
    listing.assert_not_called()
    assert result.actions[0].target == 'vendor-select'
    assert '원본' in result.actions[0].label


def test_persisted_eight_turn_memory_and_pending_restore(client_store, monkeypatch):
    from uuid import uuid4
    from backend_logic2.assistant import api
    client, store, _ = client_store
    svc = service()
    monkeypatch.setattr(api, 'get_assistant_service', lambda: svc)
    sid = str(uuid4())
    assert client.post('/api/assistant/sessions', json={'id': sid}).status_code == 201
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION')]):
        first = client.post('/api/assistant/messages', json={'message':'내 승인 대기 몇 개야?', 'session_id':sid, 'session_version':0})
        assert first.json()['intent'] == 'clarification'
        # Reload from DB, not forged browser memory or its truncated history.
        restored = client.get(f'/api/assistant/sessions/{sid}').json()
        assert restored['dialogue']['pending']['kind'] == 'approval_meaning'
        result = client.post('/api/assistant/messages', json={'message':'결정할 작업 전체', 'session_id':sid, 'session_version':1, 'dialogue':{'filters':{'keyword':'FORGED'}}})
        assert result.json()['meta']['total_count'] == 1
        for version in range(2, 11):
            result = client.post('/api/assistant/messages', json={'message':'내가 결정할 작업 몇 개야?', 'session_id':sid, 'session_version':version})
            assert result.status_code == 200
        restored = client.get(f'/api/assistant/sessions/{sid}').json()
        assert len(restored['dialogue']['memory']['recent']) == 8
        assert restored['dialogue']['memory']['compacted_turns'] == 3
        assert len(restored['messages']) == 22  # Compression did not delete display history.
        assert 'FORGED' not in str(restored['dialogue'])
