import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend_logic2.assistant.adapters.json_feature_catalog import JsonFeatureCatalog
from backend_logic2.assistant.adapters.postgres_procurement_query import (
    PostgresProcurementQuery,
)
from backend_logic2.assistant.adapters.sqlite_help_knowledge import SQLiteHelpKnowledge
from backend_logic2.assistant.models import (
    AssistantMessageRequest,
    AssistantPlan,
    AssistantRecord,
    CaseQueryFilters,
    FeatureMatch,
    HelpMatch,
)
from backend_logic2.assistant.service import AssistantService


class FakeModel:
    available = False
    model_name = "gpt-5.6-luna"

    def plan(self, **_kwargs):
        return None

    def compose(self, **_kwargs):
        return None


class FakeFreshness:
    def refresh_reference(self, reference, *, actor):
        del reference, actor
        return False


class FakeCatalog:
    def search(self, query, *, limit=5):
        del query
        return [
            FeatureMatch(
                id="mr-review",
                title="구매 요청 검토",
                summary="구매 요청을 확인합니다.",
                target="mr-list",
                keywords=["MR", "승인"],
                steps=["MR 목록 열기", "상세 확인"],
            )
        ][:limit]


class FakeHelp:
    def search(self, query, *, limit=4):
        del query
        return [
            HelpMatch(
                id="read-only",
                title="읽기 전용",
                content="업무는 사용자가 화면에서 최종 실행합니다.",
                target="mr-list",
            )
        ][:limit]


class FakeQuery:
    def __init__(self):
        self.last_actor = None

    def query_cases(self, filters, *, actor):
        self.last_actor = actor
        if filters.exact_reference != "MAT-MR-2026-00322":
            return []
        return [
            AssistantRecord(
                case_id="case-1",
                reference="MAT-MR-2026-00322",
                item_name="안전모",
                stage="QUOTATION_COLLECTION",
                stage_label="견적 회신 대기",
                status="WAITING_INPUT",
                status_label="사용자 확인 대기",
                waiting_on="공급사",
                next_action="회신 현황을 확인하거나 견적 마감일까지 기다립니다.",
                target="vendor-select",
            )
        ]


class AssistantServiceTests(unittest.TestCase):
    def setUp(self):
        self.query = FakeQuery()
        self.service = AssistantService(
            feature_catalog=FakeCatalog(),
            help_knowledge=FakeHelp(),
            procurement_query=self.query,
            freshness=FakeFreshness(),
            model=FakeModel(),
        )

    def test_exact_mr_status_is_grounded_and_navigable(self):
        response = self.service.answer(
            AssistantMessageRequest(message="MAT-MR-2026-00322 지금 어디야?"),
            current_user={"erp_user_id": "buyer@example.com"},
        )

        self.assertEqual(response.intent, "case_status")
        self.assertEqual(response.records[0].stage, "QUOTATION_COLLECTION")
        self.assertEqual(response.actions[0].target, "vendor-select")
        self.assertEqual(response.actions[0].search_query, "MAT-MR-2026-00322")
        self.assertTrue(response.meta["read_only"])
        self.assertEqual(self.query.last_actor, "buyer@example.com")

    def test_mutation_word_returns_guide_not_a_mutation_action(self):
        response = self.service.answer(
            AssistantMessageRequest(message="이 MR 작업 시작해줘"),
            current_user={"email": "buyer@example.com"},
        )

        self.assertEqual(response.intent, "feature_guide")
        self.assertTrue(response.actions)
        self.assertTrue(all(action.type in {"navigate", "navigate_with_filters"} for action in response.actions))
        self.assertIn("대신 실행하지", response.answer)


class AssistantAdapterTests(unittest.TestCase):
    def test_json_catalog_and_sqlite_help_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feature_source = root / "features.json"
            feature_source.write_text(
                json.dumps(
                    [
                        {
                            "id": "rfq",
                            "title": "견적 요청 발송",
                            "summary": "협력사에 RFQ를 보냅니다.",
                            "target": "vendor-select",
                            "keywords": ["RFQ", "이메일"],
                            "steps": ["협력사 선택", "발송"],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            help_source = root / "help.json"
            help_source.write_text(
                json.dumps(
                    [
                        {
                            "id": "rfq-email",
                            "title": "이메일 확인",
                            "keywords": ["RFQ", "이메일"],
                            "target": "vendor-select",
                            "content": "이메일이 없는 후보는 발송 전에 입력합니다.",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            catalog = JsonFeatureCatalog(feature_source)
            knowledge = SQLiteHelpKnowledge(help_source, root / "help.sqlite3")

            self.assertEqual(catalog.search("RFQ 이메일")[0].id, "rfq")
            self.assertEqual(knowledge.search("RFQ 이메일")[0].id, "rfq-email")

    @patch(
        "backend_logic2.assistant.adapters.postgres_procurement_query.case_repository.list_cases"
    )
    def test_regular_user_query_is_scoped_to_assignee(self, list_cases):
        list_cases.return_value = [
            {
                "case_id": "case-1",
                "mr_name": "MAT-MR-2026-00001",
                "status": "WAITING_INPUT",
                "stage": "MR_REVIEW",
                "summary": {"item_name": "안전모", "attachments": ["spec.pdf"]},
            }
        ]

        records = PostgresProcurementQuery().query_cases(
            CaseQueryFilters(has_attachments=True),
            actor="buyer@example.com",
        )

        self.assertEqual(records[0].reference, "MAT-MR-2026-00001")
        self.assertEqual(list_cases.call_args.kwargs["assigned_user_id"], "buyer@example.com")
        self.assertFalse(list_cases.call_args.kwargs["include_closed"])


if __name__ == "__main__":
    unittest.main()
