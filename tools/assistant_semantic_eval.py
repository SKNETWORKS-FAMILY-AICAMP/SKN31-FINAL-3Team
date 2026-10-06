"""Opt-in model eval using only synthetic data, public guides and fixed questions.

No real users, database or ERP are consulted. Credentials are loaded by file
path and never printed. Run outside pytest so normal CI never bills API calls.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from dotenv import dotenv_values
    settings = dotenv_values(args.env_file)
    for key in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ASSISTANT_MODEL', 'ASSISTANT_REASONING_EFFORT'):
        if settings.get(key):
            os.environ[key] = settings[key]
    if not os.environ.get('OPENAI_API_KEY'):
        raise SystemExit('API key is not configured; no request sent')
    from openai import OpenAI
    from backend_logic2.assistant.adapters.openai_responses import OpenAIResponsesAssistant
    from backend_logic2.assistant.adapters.json_feature_catalog import JsonFeatureCatalog
    from backend_logic2.assistant.adapters.sqlite_help_knowledge import SQLiteHelpKnowledge
    from backend_logic2.assistant.adapters.postgres_procurement_query import PostgresProcurementQuery
    from backend_logic2.assistant.service import AssistantService
    from backend_logic2.assistant.models import AssistantMessageRequest
    from backend_logic2.assistant.query_routing import business_today

    class NoFreshness:
        def refresh_reference(self, *args, **kwargs):
            raise AssertionError('Live ERP access forbidden in this eval')

    rows = [dict(case_id=f'demo-{i}', mr_name=f'MAT-MR-2026-99{i:03}', item_name='테스트 무선 마우스',
                 stage=stage, status=status, summary={'item_name': '테스트 무선 마우스'})
            for i, (stage, status) in enumerate([
                ('MR_REVIEW', 'AWAITING_MR_REVIEW'), ('RFQ_TARGET_SELECTION', 'WAITING_INPUT'),
                ('PRE_PO_APPROVAL', 'WAITING_INPUT'), ('PR_RESPONSE_WAITING', 'WAITING_INPUT'),
                ('QUOTATION_COLLECTION', 'WAITING_INPUT')], 1)]
    rows[0]['summary']['attachments'] = [{'file_name':'synthetic.txt'}]
    rows[0]['summary']['schedule_date'] = business_today().isoformat()
    client = OpenAI(timeout=25, max_retries=0)
    output = []
    with tempfile.TemporaryDirectory(prefix='assistant-semantic-') as temp, \
         patch('backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases', return_value=rows), \
         patch('backend_logic2.assistant.adapters.user_access.may_approve_po', return_value=False):
        model = OpenAIResponsesAssistant(client)
        service = AssistantService(model=model, procurement_query=PostgresProcurementQuery(), freshness=NoFreshness(),
            feature_catalog=JsonFeatureCatalog(ROOT/'backend_logic2/assistant/data/features.json'),
            help_knowledge=SQLiteHelpKnowledge(ROOT/'backend_logic2/assistant/data/help_articles.json', Path(temp)/'help.db'))
        def ask(name, question, previous=None, *, expected_intent=None, expected_count=None, expected_group=None, expected_scope=None, expected_target=None, expected_filter=None, expected_records=None):
            result = service.answer(AssistantMessageRequest(message=question, dialogue=previous), current_user={'erp_user_id': 'synthetic-eval-user'})
            errors = []
            if expected_intent and result.intent not in expected_intent: errors.append('intent')
            if expected_count is not None and result.meta.get('total_count') != expected_count: errors.append('total_count')
            filters = result.meta.get('applied_filters') or {}
            if expected_group and filters.get('waiting_for') not in expected_group: errors.append('waiting_group')
            if expected_scope and filters.get('task_scope') != expected_scope: errors.append('task_scope')
            if expected_target and not any(a.target == expected_target for a in result.actions): errors.append('navigation_target')
            if expected_filter and any(filters.get(k) != v for k, v in expected_filter.items()): errors.append('filter')
            if expected_records is not None and [r.reference for r in result.records] != expected_records: errors.append('records')
            entry = dict(name=name, question=question, intent=result.intent, filters=filters, total=result.meta.get('total_count'), answer=result.answer, records=[r.reference for r in result.records], targets=[a.target for a in result.actions], errors=errors, passed=not errors)
            output.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            return result.dialogue
        pending = ask('ambiguous-approval', '내 승인을 기다리는 작업이 몇 개야?', expected_intent={'clarification'})
        chosen = ask('choice-full-scope', '결정할 작업 전체', pending, expected_count=2, expected_group={'decision'}, expected_scope='actionable')
        ask('count-follow-up', '그래서 총 몇 건이라는 거야?', chosen, expected_count=2, expected_scope='actionable')
        pending = ask('ambiguous-again', '제가 승인해야 하는 일은 몇 개예요?', expected_intent={'clarification'})
        ask('choice-freeform', '결재만 말고 협력사를 고르는 것도 포함해줘', pending, expected_count=2, expected_group={'decision'}, expected_scope='actionable')
        ask('natural-my-task', '나한테 공 넘어온 거 얼마나 남았어?', expected_intent={'case_query'}, expected_count=2, expected_scope='actionable')
        ask('external-paraphrase', '아직 업체가 답장 안 한 작업 찾아줘', expected_intent={'case_query'}, expected_group={'external'})
        guide = ask('guide', '견적 다시 받으려면 어느 화면으로 가야 해?', expected_intent={'help', 'feature_guide'})
        ask('guide-followup', '거기서 어떻게 하면 돼?', guide, expected_intent={'help', 'feature_guide'})
        ask('unsupported', '회사 통장 잔액 얼마야?', expected_intent={'unsupported'})
        ask('task-filter', '마우스 구매 중 내가 직접 판단해야 할 것만 세어줘', expected_intent={'case_query'}, expected_count=2, expected_scope='actionable')
        ask('explicit-po', '내가 승인할 PO는 몇 개야?', expected_intent={'case_query'}, expected_count=0, expected_group={'po_approval'}, expected_scope='actionable')
        ask('unsupported-filter', '어제 생성된 구매 작업만 세어줘', expected_intent={'clarification'})
        item = ask('item-context', '무선 마우스 구매 작업 몇 개야?', expected_intent={'case_query'}, expected_count=5)
        ask('remove-item', '품목은 상관없이 외부에서 기다리는 것만 세어줘', item, expected_count=2, expected_group={'external'})
        original = ask('semantic-feature', '업체가 보냈던 견적서 파일 다시 열고 싶은데', expected_intent={'feature_guide', 'help'})
        assert original.guide_target == 'vendor-select', 'Wrong guide navigation'
        old = item
        for _ in range(9):
            old = service.answer(AssistantMessageRequest(message='MAT-MR-2026-99001 현재 상태', dialogue=old), current_user={'erp_user_id':'synthetic-eval-user'}).dialogue
        assert old.memory.compacted_turns == 2 and len(old.memory.recent) == 8
        ask('older-summary-recall', '처음 검색했던 품목으로 돌아가서 구매 작업 총 몇 개인지 알려줘', old, expected_intent={'case_query'}, expected_count=5)
        ask('exact-mr', 'MAT-MR-2026-99004 지금 어느 단계야?', expected_intent={'case_status'}, expected_filter={'exact_reference':'MAT-MR-2026-99004'}, expected_target='po-manage')
        ask('quotation-count', '견적 회신을 기다리는 작업은 몇 건이야?', expected_intent={'case_query'}, expected_count=1, expected_group={'quotation'})
        ask('assigned-count', '내 담당 구매 작업 전부 몇 개야?', expected_intent={'case_query'}, expected_count=5, expected_scope='assigned')
        ask('attachments', '첨부파일 있는 구매 요청만 보여줘', expected_intent={'case_query'}, expected_filter={'has_attachments':True, 'keyword':None}, expected_records=['MAT-MR-2026-99001'])
        ask('due-window', '앞으로 3일 안에 납기인 작업 찾아줘', expected_intent={'case_query'}, expected_filter={'due_within_days':3}, expected_records=['MAT-MR-2026-99001'])
        ask('completed', '완료된 무선 마우스 구매는 몇 건이야?', expected_intent={'case_query'}, expected_count=0, expected_filter={'include_closed':True, 'status':'COMPLETED'})
        ask('approval-guide', 'PO 승인 버튼이 안 보이는데 어떤 권한이 필요해?', expected_intent={'help','feature_guide'}, expected_target='po-manage')
        ask('deadline-guide', '업체가 시간이 더 필요하다는데 견적 마감일은 어디서 늘려?', expected_intent={'help','feature_guide'}, expected_target='vendor-select')
        ask('pause-guide', '이번 구매 건만 자동으로 진행되지 않게 하려면?', expected_intent={'help','feature_guide'}, expected_target='vendor-select')
        ask('allowlist-guide', '메일 받을 수 있는 주소는 어디에서 추가해?', expected_intent={'help','feature_guide'}, expected_target='company-policy')
        ask('scorecard-guide', '물건 다 받았는데 거래처 평가는 어디서 해?', expected_intent={'help','feature_guide'}, expected_target='po-manage')
        ask('mutating-command', '견적 요청 메일 보내줘', expected_intent={'help','feature_guide'}, expected_target='vendor-select')
        ask('substitute-guide', '대체품으로 바꿀지는 요청자가 어디서 결정해?', expected_intent={'help','feature_guide'}, expected_target='mr-list')
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'total': len(output), 'passed': sum(x['passed'] for x in output)}))
    return 0 if all(x['passed'] for x in output) else 1


if __name__ == '__main__':
    raise SystemExit(main())
