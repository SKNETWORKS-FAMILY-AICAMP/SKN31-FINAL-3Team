"""Offline guidance regressions: no real model, ERP, mail or database calls."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import get_args
from unittest.mock import patch

from pydantic import ValidationError
from backend_logic2.assistant.adapters.json_feature_catalog import JsonFeatureCatalog
from backend_logic2.assistant.adapters.sqlite_help_knowledge import SQLiteHelpKnowledge
from backend_logic2.assistant.models import AssistantAction, AssistantMessageRequest, NavigationTarget
from backend_logic2.assistant.service import AssistantService, SCREEN_GUIDE_QUERIES, _heuristic_plan
from test_assistant import FakeModel


DATA = Path(__file__).resolve().parents[1] / 'backend_logic2' / 'assistant' / 'data'


class ForbiddenAdapter:
    def query_cases(self, *args, **kwargs):
        raise AssertionError('Guide must not query procurement records')

    def refresh_reference(self, *args, **kwargs):
        raise AssertionError('Guide must not refresh ERP state')


class GuidanceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows asyncio uses a loopback socketpair for its in-process test
        # client. Permit that plumbing, but forbid connections to external hosts.
        original_connect = socket.socket.connect
        def local_only(sock, address):
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('External network forbidden')
            return original_connect(sock, address)
        self.net = patch.object(socket.socket, 'connect', local_only)
        self.net.start()
        self.addCleanup(self.net.stop)
        self.catalog = JsonFeatureCatalog(DATA / 'features.json')
        self.help = SQLiteHelpKnowledge(DATA / 'help_articles.json', Path(self.temp.name) / 'help.sqlite3')
        self.service = AssistantService(feature_catalog=self.catalog, help_knowledge=self.help,
            procurement_query=ForbiddenAdapter(), freshness=ForbiddenAdapter(), model=FakeModel())

    def ask(self, message, tab='dashboard'):
        return self.service.answer(AssistantMessageRequest(message=message,
            context={'current_tab': tab}), current_user={'erp_user_id': 'buyer@example.com'})

    def test_real_catalog_questions_link_to_expected_screen(self):
        cases = [
            ('회사 구매 정책 사용법', 'company-policy', '시작 시점'),
            ('AI 판단 기록 사용법', 'ai-decision-log', '실행 결과'),
            ('자동 진행 끄기는 어디에 있어?', 'vendor-select', '⋯'),
            ('자동 진행 판정 방법', 'vendor-select', '조회 전용'),
            ('견적 원본 확인 방법', 'vendor-select', '원본 확인'),
            ('화이트리스트 수정 방법', 'company-policy', 'TEST_MODE'),
            ('공급사 탐색 소스 설정 방법', 'company-policy', '외부 API'),
            ('런팟 워커 사용법', 'company-policy', '비용'),
            ('협력사 평가 작성 방법', 'po-manage', '1~5점'),
            ('PO 승인 권한 설명', 'po-manage', 'Purchase Master Manager'),
            ('컬럼 이동 방법', 'mr-list', '브라우저 탭'),
            ('협력사 연락처 확인 방법', 'vendor-select', '미등록'),
            ('공급사 수주 확인 이후의 진행 경로 설명', 'po-manage', '최종 승인'),
            ('협력사 선정 화면 사용법', 'vendor-select', '자동'),
            ('담당자 배정 방법', 'company-policy', '즉시 재배정'),
        ]
        for question, target, expected in cases:
            with self.subTest(question=question):
                response = self.ask(question)
                self.assertTrue(response.meta['query_available'])
                self.assertEqual(response.intent, 'help')
                self.assertEqual(response.actions[0].target, target)
                self.assertIn(expected, response.answer)
                self.assertEqual(response.records, [])
                self.assertTrue(response.meta['read_only'])

    def test_every_current_screen_has_a_matching_guide(self):
        self.assertEqual(set(SCREEN_GUIDE_QUERIES), set(get_args(NavigationTarget)))
        for tab in get_args(NavigationTarget):
            with self.subTest(tab=tab):
                response = self.ask('현재 화면에서 무엇을 할 수 있어?', tab)
                self.assertEqual(response.intent, 'help')
                self.assertEqual(response.actions[0].target, tab)
                self.assertIn(SCREEN_GUIDE_QUERIES[tab], response.answer)

    def test_schema_rejects_arbitrary_urls_and_mutation_actions(self):
        for target in ('https://example.com', 'javascript:alert(1)', '../admin', 'unknown'):
            with self.subTest(target=target), self.assertRaises(ValidationError):
                AssistantAction(label='bad', target=target)
        with self.assertRaises(ValidationError):
            AssistantAction(type='approve_po', label='bad', target='po-manage')

    def test_authenticated_api_accepts_new_context_without_running_a_workflow(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from auth_service.dependencies import require_authenticated_user
        from backend_logic2.assistant import api

        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[require_authenticated_user] = lambda: {'erp_user_id': 'buyer@example.com'}
        with patch.object(api, 'get_assistant_service', return_value=self.service), \
             patch.object(api, 'assistant_enabled', return_value=True), TestClient(app) as client:
            for tab in get_args(NavigationTarget):
                with self.subTest(tab=tab):
                    response = client.post('/api/assistant/messages', json={
                        'message': '현재 화면에서 무엇을 할 수 있어?', 'context': {'current_tab': tab},
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['actions'][0]['target'], tab)
            self.assertEqual(client.post('/api/assistant/messages', json={
                'message': 'hi', 'context': {'current_tab': 'https://example.com'},
            }).status_code, 422)

    def test_catalog_and_help_are_valid_and_have_unique_ids(self):
        for filename in ('features.json', 'help_articles.json'):
            entries = json.loads((DATA / filename).read_text(encoding='utf-8'))
            self.assertEqual(len(entries), len({entry['id'] for entry in entries}))
            for entry in entries:
                self.assertIn(entry['target'], get_args(NavigationTarget))
        selection = next(f for f in self.catalog.all() if f.id == 'supplier-selection')
        self.assertNotIn('발주 시작', selection.steps)
        self.assertIn('자동', selection.summary)

    def test_explicit_case_queries_keep_the_existing_route(self):
        for question, intent in (
            ('MAT-MR-2026-00322 현재 화면에서 어디야?', 'case_status'),
            ('승인 대기 MR 보여줘', 'case_query'),
            ('납기 3일 이내 MR 보여줘', 'case_query'),
            ('RFQ 발송해줘', 'feature_guide'),
            ('MR 검토 방법', 'help'),
        ):
            with self.subTest(question=question):
                self.assertEqual(_heuristic_plan(question).intent, intent)

    def test_execution_request_only_returns_navigation(self):
        for question in ('PO 승인해줘', 'RFQ 발송해줘', '화이트리스트 삭제해줘'):
            with self.subTest(question=question):
                response = self.ask(question)
                self.assertIn('대신 실행하지', response.answer)
                self.assertTrue(response.actions)
                self.assertTrue(all(a.type in ('navigate', 'navigate_with_filters') for a in response.actions))

    def test_prompt_distinguishes_overview_from_execution_and_unqueried_records(self):
        # Guard the factual instructions; live-model QA separately reviews wording.
        from backend_logic2.assistant.prompting import COMPOSER_INSTRUCTIONS
        self.assertIn('대시보드에서도 PO 승인 대기', COMPOSER_INSTRUCTIONS)
        self.assertIn('실제 PO 승인은 PO 관리', COMPOSER_INSTRUCTIONS)
        self.assertIn('조회하지 않은 상태와 조회 결과가 빈 상태를 구분', COMPOSER_INSTRUCTIONS)

    def test_sqlite_help_index_rebuilds_after_source_change_on_startup(self):
        path = Path(self.temp.name) / 'help.json'
        db = Path(self.temp.name) / 'reload.sqlite3'
        article = {'id': 'reload', 'title': '고유도움말', 'keywords': ['고유도움말'],
                   'target': 'dashboard', 'content': 'before'}
        path.write_text(json.dumps([article]), encoding='utf-8')
        self.assertEqual(SQLiteHelpKnowledge(path, db).search('고유도움말')[0].content, 'before')
        article['content'] = 'after'
        path.write_text(json.dumps([article]), encoding='utf-8')
        self.assertEqual(SQLiteHelpKnowledge(path, db).search('고유도움말')[0].content, 'after')


if __name__ == '__main__':
    unittest.main()
