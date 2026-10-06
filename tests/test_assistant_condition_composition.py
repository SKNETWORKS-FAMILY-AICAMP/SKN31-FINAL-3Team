"""Regressions from additional user journeys; no model, ERP or DB I/O."""
from unittest.mock import patch

import pytest

from backend_logic2.assistant.models import AssistantPlan, CaseQueryFilters, DialogueContext
from backend_logic2.assistant.query_constraints import apply_explicit_conditions
from backend_logic2.assistant.query_routing import business_today
from test_assistant_graph import PATH, TrackedModel, service, turn
from test_assistant_waiting_queries import row


class SemanticPlan(TrackedModel):
    def __init__(self, plan):
        super().__init__()
        self.result = plan

    def plan(self, **kwargs):
        return self.result.model_copy(deep=True)


@pytest.mark.parametrize('count_word', ['세어줘', '세어 주세요', '집계해줘', '세 줘'])
def test_followup_counts_all_matches_not_only_display_page(count_word):
    rows = [row(reference=f'MAT-MR-2099-{n:05}', item_name='무선 마우스') for n in range(12)]
    previous = DialogueContext(filters=CaseQueryFilters(keyword='무선 마우스'))
    with patch(PATH, return_value=rows):
        result = turn(service(), '그중 외부 응답 기다리는 것만 ' + count_word, previous)
    assert result.meta['total_count'] == 12
    assert len(result.records) == 10
    assert result.meta['has_more']


@pytest.mark.parametrize('question', ['내가 결재해야 할 PO 몇 건이야?', '제가 승인할 PO 몇 개야?', '내가 PO 승인해야 할 작업 세어줘'])
def test_explicit_po_scope_beats_broad_model_decision_plan(question):
    model = SemanticPlan(AssistantPlan(intent='case_query', capability='my_tasks', filters=CaseQueryFilters(waiting_for='decision')))
    with patch(PATH, return_value=[row('MR_REVIEW', 'AWAITING_MR_REVIEW', reference='mr'), row('PRE_PO_APPROVAL', reference='po')]), \
         patch('backend_logic2.assistant.adapters.user_access.may_approve_po', return_value=True):
        result = turn(service(model), question)
    assert result.meta['total_count'] == 1
    assert [r.reference for r in result.records] == ['po']
    assert result.dialogue.filters.waiting_for == 'po_approval'


@pytest.mark.parametrize('word', ['여섯', '일곱', '여덟', '아홉', '열', '열한', '열두', '11'])
def test_out_of_range_ordinal_clarifies_without_model_or_query(word):
    model = TrackedModel()
    previous = DialogueContext(references=['MAT-MR-2099-00001'])
    with patch(PATH) as listing:
        result = turn(service(model), word + ' 번째 건 알려줘', previous)
    assert result.intent == 'clarification'
    listing.assert_not_called()
    assert not model.plans


def test_valid_eighth_reference_is_reauthorized():
    refs = [f'MAT-MR-2099-{i:05}' for i in range(1, 11)]
    with patch(PATH, return_value=[]) as listing:
        result = turn(service(), '여덟 번째 건 알려줘', DialogueContext(references=refs))
    assert result.dialogue.filters.exact_reference == refs[7]
    assert listing.call_args.kwargs['assigned_user_id'] == 'buyer'


def test_due_guard_keeps_attachment_and_model_conditions():
    model = SemanticPlan(AssistantPlan(intent='case_query', filters=CaseQueryFilters(keyword='무선 마우스', has_attachments=True)))
    rows = [row(reference='with', item_name='무선 마우스'), row(reference='without', item_name='무선 마우스')]
    for r in rows:
        r['summary']['schedule_date'] = business_today().isoformat()
    rows[0]['summary']['attachments'] = [{'file_name': 'synthetic.txt'}]
    with patch(PATH, return_value=rows):
        result = turn(service(model), '무선 마우스 중 첨부파일 있고 3일 이내 납기인 작업은 몇 개야?')
    assert result.meta['total_count'] == 1
    assert result.records[0].reference == 'with'
    assert result.dialogue.filters.has_attachments is True
    assert result.dialogue.filters.due_within_days == 3


@pytest.mark.parametrize('question', ['완료된 건 빼고 같은 품목만 세어줘', '그중 완료된 건 제외하고 보여줘', '완료 말고 진행 중인 같은 품목만 세어줘'])
def test_closed_true_can_be_explicitly_reset_to_false(question):
    previous = DialogueContext(filters=CaseQueryFilters(keyword='마우스', status='COMPLETED', include_closed=True))
    model = SemanticPlan(AssistantPlan(intent='case_query', context_mode='refine', filters=CaseQueryFilters(include_closed=False)))
    with patch(PATH, return_value=[]) as listing:
        result = turn(service(model), question, previous)
    assert result.dialogue.filters.include_closed is False
    assert result.dialogue.filters.status is None
    assert result.dialogue.filters.keyword == '마우스'
    assert listing.call_args.kwargs['include_closed'] is False


@pytest.mark.parametrize('question,status,include', [
    ('완료된 무선 마우스 구매 몇 건이야?', 'COMPLETED', True),
    ('완료된 건도 포함해줘', None, True),
    ('완료된 건 제외해줘', None, False),
])
def test_completed_only_included_and_excluded_are_distinct(question, status, include):
    filters = apply_explicit_conditions(CaseQueryFilters(), question)
    assert filters.status == status
    assert filters.include_closed is include


def test_negative_external_clause_does_not_change_actionable_to_assigned():
    model = SemanticPlan(AssistantPlan(intent='case_query', context_mode='refine', filters=CaseQueryFilters(waiting_for='decision')))
    previous = DialogueContext(filters=CaseQueryFilters(keyword='마우스', waiting_for='external'))
    with patch(PATH, return_value=[row('RFQ_TARGET_SELECTION', item_name='마우스'), row('PRE_PO_APPROVAL', reference='po', item_name='마우스')]), \
         patch('backend_logic2.assistant.adapters.user_access.may_approve_po', return_value=False) as access:
        result = turn(service(model), '외부를 기다리는 건 말고 같은 품목 중 내가 결정해야 할 것만 세어줘', previous)
    assert result.meta['total_count'] == 1
    assert result.dialogue.filters.task_scope == 'actionable'
    access.assert_called_once_with('buyer')


def test_closed_exclusion_preserves_explicit_external_condition():
    filters = apply_explicit_conditions(CaseQueryFilters(include_closed=True), '완료된 건 빼고 외부 응답 대기만 세어줘')
    assert filters.include_closed is False
    assert filters.waiting_for == 'external'


@pytest.mark.parametrize('command', ['승인해줘', '발송해줘', '삭제해줘'])
def test_exact_mr_execution_request_still_explains_readonly(command):
    with patch(PATH, return_value=[row('PRE_PO_APPROVAL', reference='MAT-MR-2099-00007')]):
        result = turn(service(), 'MAT-MR-2099-00007 ' + command)
    assert '읽기 전용' in result.answer
    assert '직접 실행하지 않았습니다' in result.answer
    assert result.actions[0].target == 'po-manage'


def test_unsupported_condition_survives_context_merge():
    model = SemanticPlan(AssistantPlan(intent='case_query', unhandled_conditions=['금액'], context_mode='refine'))
    previous = DialogueContext(filters=CaseQueryFilters(keyword='마우스'))
    with patch(PATH) as listing:
        result = turn(service(model), '그중 백만원 이상만 찾아줘', previous)
    assert result.intent == 'clarification'
    listing.assert_not_called()


@pytest.mark.parametrize('replacement', ['키보드', '유선 마우스', '27인치 모니터'])
def test_explicit_item_replacement_does_not_invent_old_attributes(replacement):
    model = SemanticPlan(AssistantPlan(intent='case_query', context_mode='refine', filters=CaseQueryFilters(keyword='무선 ' + replacement)))
    previous = DialogueContext(filters=CaseQueryFilters(keyword='무선 마우스'))
    with patch(PATH, return_value=[]):
        result = turn(service(model), '마우스 말고 ' + replacement + '로 찾아줘', previous)
    assert result.dialogue.filters.keyword == replacement


def test_scope_correction_is_not_an_item_replacement():
    filters = apply_explicit_conditions(CaseQueryFilters(keyword='마우스'), '외부 말고 내가 승인할 작업으로 조회해줘')
    assert filters.keyword == '마우스'


def test_failed_subset_remains_supported_after_merge_refactor():
    previous = DialogueContext(filters=CaseQueryFilters(keyword='마우스', waiting_for='external'))
    with patch(PATH, return_value=[row('PO_CREATION_FAILED', 'FAILED', reference='failed'), row(reference='waiting')]):
        result = turn(service(), '그중 실패한 작업 세어줘', previous)
    assert result.meta['total_count'] == 1
    assert result.dialogue.filters.keyword == '마우스'
    assert result.dialogue.filters.status == 'FAILED'
    assert result.dialogue.filters.waiting_for is None
