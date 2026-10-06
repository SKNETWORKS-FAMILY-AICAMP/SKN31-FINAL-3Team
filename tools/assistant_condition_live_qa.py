"""Opt-in production read-only condition checks; aggregate results only.

No model requests, ERP calls, sessions, approvals, or workflow changes. PostgreSQL
enforces read-only transactions and a five-second statement timeout. Help-index
writes are isolated in a temporary directory. Run only with deployment authority.
"""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    os.environ['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=5000'
    from procurement_db import get_connection
    from backend_logic2.assistant.models import AssistantMessageRequest
    from backend_logic2.assistant.query_routing import business_today
    from backend_logic2.assistant.service import get_assistant_service

    class OfflineModel:
        available = False
        model_name = 'readonly-qa-no-network'
        def plan(self, **kwargs): return None
        def compose(self, **kwargs): raise AssertionError('Model egress forbidden')

    class NoFreshness:
        def refresh_reference(self, *args, **kwargs): raise AssertionError('ERP access forbidden')

    open_only = "status NOT IN ('COMPLETED','CANCELLED','REJECTED')"
    attachment = "coalesce(summary->'attachments','[]'::jsonb) NOT IN ('[]'::jsonb,'null'::jsonb,'0'::jsonb,'{}'::jsonb)"
    external = """((stage IN ('SUBSTITUTE_DECISION','QUOTATION_COLLECTION','PR_RESPONSE_WAITING')
        AND status='WAITING_INPUT') OR (stage='DELIVERY' AND status IN ('WAITING_INPUT','RUNNING','PENDING')))
        AND NOT jsonb_path_exists(coalesce(workflow_snapshot,'{}'::jsonb),
            '$.values.quotation_ranking_meta.auto_progress.checks[*] ? (@.status == "blocked")')"""
    today = business_today()
    cases = [
        ('completed-only', '완료된 구매 작업은 몇 건이야?', "status='COMPLETED'", [], False),
        ('exclude-completed', '그중 완료된 건 빼고 진행 중인 작업을 세어줘', open_only, [], True),
        ('compound', '첨부파일 있고 3일 이내 납기인 작업은 몇 개야?',
         open_only + ' AND ' + attachment + " AND NULLIF(summary->>'schedule_date','')::date BETWEEN %s AND %s", [today, today+timedelta(days=3)], False),
        ('po-only', 'PO 승인 대기 작업을 세어줘', open_only + " AND stage='PRE_PO_APPROVAL' AND status='WAITING_INPUT'", [], False),
        ('external-count', '외부 응답 대기 작업을 세어줘', open_only + ' AND ' + external, [], False),
        ('external-no-attachment', '그중 첨부 없는 것만 세어줘', open_only + ' AND ' + external + ' AND NOT (' + attachment + ')', [], True),
        ('mr-review', 'MR 승인 대기 작업 몇 건이야?', open_only + " AND stage='MR_REVIEW' AND status IN ('AWAITING_MR_REVIEW','WAITING_INPUT')", [], False),
        ('approval-general', '승인 대기 중인 MR 몇 건이야?', open_only + " AND ((stage='MR_REVIEW' AND status IN ('AWAITING_MR_REVIEW','WAITING_INPUT')) OR (stage='PRE_PO_APPROVAL' AND status='WAITING_INPUT'))", [], False),
    ]

    def count(where, params):
        with get_connection() as conn:
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] == 'on'
            return conn.execute('SELECT count(*) AS n FROM procurement.procurement_case WHERE ' + where, params).fetchone()['n']

    results = []
    with tempfile.TemporaryDirectory(prefix='assistant-condition-readonly-') as tmp:
        os.environ['ASSISTANT_HELP_DB_PATH'] = str(Path(tmp)/'help.sqlite3')
        svc = get_assistant_service()
        svc.model, svc.freshness = OfflineModel(), NoFreshness()
        for repeat in (1, 2):
            dialogue = None
            for label, message, where, params, followup in cases:
                expected_before = count(where, params)
                response = svc.answer(AssistantMessageRequest(message=message, dialogue=dialogue if followup else None), current_user={'erp_user_id':'Administrator'})
                expected_after = count(where, params)
                dialogue = response.dialogue
                actual = response.meta.get('total_count')
                stable = expected_before == expected_after
                passed = stable and actual == expected_after and response.meta.get('query_available') is True
                results.append(dict(round=repeat, scenario=label, expected=expected_after, actual=actual,
                    stable=stable, passed=passed, filters=response.meta.get('applied_filters')))
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(total=len(results), passed=sum(r['passed'] for r in results))))
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
