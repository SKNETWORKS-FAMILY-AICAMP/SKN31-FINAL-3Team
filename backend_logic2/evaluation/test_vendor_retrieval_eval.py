import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from vendor_retrieval_eval import (  # noqa: E402
    _item_sources,
    add_human_reference,
    build_gold,
    build_label_sheet,
    candidate_matches_gold,
    evaluate,
    import_human_references,
    is_document_candidate,
    is_valid_vendor_candidate,
    metrics_at_k,
    reciprocal_rank_fusion,
)
try:
    from build_erpnext_evaluation_queries import export_queries  # noqa: E402
except ModuleNotFoundError:
    export_queries = None


class VendorMatchingTest(unittest.TestCase):
    def test_matches_by_business_number(self):
        candidate = {"name": "다른 표기", "business_no": "123-45-67890"}
        gold = {"vendor_name": "정식회사명", "business_no": "1234567890"}
        self.assertTrue(candidate_matches_gold(candidate, gold))

    def test_matches_naver_title_by_alias(self):
        candidate = {"name": "대한안전산업 | 산업안전용품 전문", "source_url": "https://shop.example.kr/a"}
        gold = {"vendor_name": "대한안전산업 주식회사", "aliases": ["대한안전산업"], "domains": []}
        self.assertTrue(candidate_matches_gold(candidate, gold))

    def test_matches_by_domain(self):
        candidate = {"name": "검색 제목", "source_url": "https://www.vendor.co.kr/products/1"}
        gold = {"vendor_name": "벤더", "domains": ["vendor.co.kr"]}
        self.assertTrue(candidate_matches_gold(candidate, gold))

    def test_does_not_substring_match_unregistered_vendor_name(self):
        candidate = {"name": "대한안전산업 검은별 안전모 납품 사례"}
        gold = {"vendor_name": "검은별", "aliases": [], "domains": []}
        self.assertFalse(candidate_matches_gold(candidate, gold))

    def test_detects_download_as_non_vendor_candidate(self):
        candidate = {
            "name": "계약 현황.xlsx",
            "source_url": "https://example.go.kr/FileDown.do?id=1",
        }
        self.assertTrue(is_document_candidate(candidate))
        self.assertFalse(is_valid_vendor_candidate(candidate))

    def test_explicit_local_vendor_is_valid(self):
        candidate = {
            "name": "대한안전산업",
            "candidate_type": "vendor",
            "source_channel": "business_entity_search",
        }
        self.assertTrue(is_valid_vendor_candidate(candidate))


class RetrievalMetricTest(unittest.TestCase):
    def setUp(self):
        self.gold = [
            {"vendor_name": "업체A", "aliases": [], "domains": [], "relevance": 3},
            {"vendor_name": "업체B", "aliases": [], "domains": [], "relevance": 2},
        ]
        self.candidates = [
            {"name": "무관업체"},
            {"name": "업체A"},
            {"name": "업체A"},
            {"name": "업체B"},
        ]

    def test_precision_recall_and_duplicate_penalty(self):
        metric = metrics_at_k(self.candidates, self.gold, 4)
        self.assertEqual(metric["hits"], 2)
        self.assertEqual(metric["precision"], 0.5)
        self.assertEqual(metric["recall"], 1.0)
        self.assertEqual(metric["reciprocal_rank"], 0.5)
        self.assertGreater(metric["ndcg"], 0)
        self.assertLess(metric["ndcg"], 1)

    def test_missing_results_are_counted_in_precision_denominator(self):
        metric = metrics_at_k([{"name": "업체A"}], self.gold, 5)
        self.assertEqual(metric["precision"], 0.2)
        self.assertEqual(metric["recall"], 0.5)

    def test_human_reference_missed_by_search_reduces_recall(self):
        gold = [
            {"vendor_name": "검색된 업체", "relevance": 3},
            {
                "vendor_name": "사람만 찾은 업체",
                "relevance": 3,
                "observed_sources": [{"source": "human_reference"}],
            },
        ]
        metric = metrics_at_k([{"name": "검색된 업체"}], gold, 1)
        self.assertEqual(metric["recall"], 0.5)

    def test_quality_and_pool_diagnostics(self):
        judgments = [
            {"vendor_name": "업체A", "aliases": [], "domains": [], "relevance": 3},
            {"vendor_name": "무관업체", "aliases": [], "domains": [], "relevance": 0},
        ]
        candidates = [
            {"name": "업체A", "candidate_type": "vendor"},
            {"name": "계약현황.pdf", "source_url": "https://x.kr/a.pdf"},
            {"name": "미라벨업체", "candidate_type": "vendor"},
        ]
        metric = metrics_at_k(candidates, self.gold, 3, judgments=judgments)
        self.assertAlmostEqual(metric["qualified_precision"], 1 / 3)
        self.assertAlmostEqual(metric["judged_rate"], 1 / 3)
        self.assertAlmostEqual(metric["unjudged_rate"], 2 / 3)
        self.assertAlmostEqual(metric["document_rate"], 1 / 3)
        self.assertAlmostEqual(metric["vendor_validity_rate"], 2 / 3)
        self.assertGreater(metric["average_precision"], 0)

    def test_rrf_merges_same_business(self):
        rag = [{"name": "업체A", "business_no": "123-45", "source": "rag", "retrieval_rank": 1}]
        naver = [{"name": "업체 A 홈페이지", "business_no": "12345", "source": "naver", "retrieval_rank": 2}]
        fused = reciprocal_rank_fusion([rag, naver])
        self.assertEqual(len(fused), 1)
        self.assertEqual(set(fused[0]["source_members"]), {"rag", "naver"})


class GoldBuilderTest(unittest.TestCase):
    def test_refuses_unlabeled_candidates(self):
        labels = {
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트",
                "labeling_status": "complete",
                "candidates": [{"candidate_name": "업체A", "relevance": None}],
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.json"
            output_path = Path(directory) / "gold.json"
            labels_path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_gold(labels_path, output_path)

    def test_builds_only_relevant_vendors(self):
        labels = {
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트",
                "labeling_status": "complete",
                "candidates": [
                    {"candidate_name": "업체A", "relevance": 3, "domains": [], "aliases": []},
                    {"candidate_name": "무관업체", "relevance": 0, "domains": [], "aliases": []},
                ],
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.json"
            output_path = Path(directory) / "gold.json"
            labels_path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
            gold = build_gold(labels_path, output_path)
            self.assertEqual(len(gold["items"][0]["relevant_vendors"]), 1)
            self.assertEqual(gold["items"][0]["relevant_vendors"][0]["vendor_name"], "업체A")
            self.assertEqual(len(gold["items"][0]["judgments"]), 2)
            self.assertEqual(gold["items"][0]["judgments"][1]["relevance"], 0)

    def test_adds_independent_human_reference_to_gold(self):
        labels = {
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트",
                "labeling_status": "complete",
                "candidates": [],
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.json"
            gold_path = Path(directory) / "gold.json"
            labels_path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")

            updated = add_human_reference(
                labels_path,
                "ITEM-1",
                "독립확인 공급사",
                3,
                "구매 계약서에서 해당 품목 납품 이력 확인",
                "검토자",
                domains=["https://supplier.example.com/catalog"],
                aliases=["독립확인"],
            )
            candidate = updated["items"][0]["candidates"][0]
            self.assertEqual(candidate["domains"], ["supplier.example.com"])
            self.assertEqual(candidate["observed_sources"][0]["source"], "human_reference")

            gold = build_gold(labels_path, gold_path)
            vendor = gold["items"][0]["relevant_vendors"][0]
            self.assertEqual(vendor["vendor_name"], "독립확인 공급사")
            self.assertEqual(vendor["observed_sources"][0]["source"], "human_reference")

    def test_imports_references_json_and_skips_blank_templates(self):
        labels = {
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트",
                "labeling_status": "complete",
                "candidates": [],
            }]
        }
        references = [
            {
                "item_code": "ITEM-1",
                "name": "일괄확인 공급사",
                "relevance": 2,
                "evidence": "공식 카탈로그에서 취급 품목 확인",
                "domains": ["https://batch.example.com/catalog"],
                "aliases": ["일괄확인"],
            },
            {
                "item_code": "ITEM-1",
                "name": "",
                "relevance": None,
                "evidence": "",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.json"
            references_path = Path(directory) / "references.json"
            labels_path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
            references_path.write_text(
                json.dumps(references, ensure_ascii=False), encoding="utf-8"
            )

            updated, imported, skipped = import_human_references(
                labels_path, references_path, "일괄검토자"
            )
            self.assertEqual((imported, skipped), (1, 1))
            candidate = updated["items"][0]["candidates"][0]
            self.assertEqual(candidate["candidate_name"], "일괄확인 공급사")
            self.assertEqual(candidate["verified_by"], "일괄검토자")
            self.assertEqual(candidate["domains"], ["batch.example.com"])

    def test_reference_import_is_atomic_when_a_row_is_invalid(self):
        labels = {
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트",
                "labeling_status": "complete",
                "candidates": [],
            }]
        }
        references = [
            {
                "item_code": "ITEM-1",
                "name": "정상 공급사",
                "relevance": 3,
                "evidence": "독립 확인",
            },
            {
                "item_code": "UNKNOWN",
                "name": "잘못된 행",
                "relevance": 3,
                "evidence": "독립 확인",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            labels_path = Path(directory) / "labels.json"
            references_path = Path(directory) / "references.json"
            original = json.dumps(labels, ensure_ascii=False)
            labels_path.write_text(original, encoding="utf-8")
            references_path.write_text(
                json.dumps(references, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaises(ValueError):
                import_human_references(labels_path, references_path)
            self.assertEqual(labels_path.read_text(encoding="utf-8"), original)


class LabelSheetTest(unittest.TestCase):
    def test_limits_each_search_source_to_label_depth(self):
        snapshot = {
            "created_at": "2026-08-25T00:00:00+00:00",
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트 품목",
                "existing": [{"name": "기존업체", "source": "existing", "retrieval_rank": 1}],
                "rag": [
                    {"name": f"RAG업체{i}", "source": "rag", "retrieval_rank": i}
                    for i in range(1, 13)
                ],
                "naver": [
                    {
                        "name": f"Naver업체{i}",
                        "source": "naver",
                        "retrieval_rank": i,
                        "source_url": f"https://vendor{i}.example.com",
                    }
                    for i in range(1, 13)
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            labels_path = Path(directory) / "labels.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            labels = build_label_sheet(snapshot_path, labels_path, depth=5)
            self.assertEqual(len(labels["items"][0]["candidates"]), 11)
            self.assertEqual(labels["items"][0]["candidates"][0]["relevance"], 3)

    def test_schema_v3_accepts_arbitrary_source_names(self):
        snapshot = {
            "schema_version": 3,
            "created_at": "2026-08-26T00:00:00+00:00",
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트 품목",
                "sources": {
                    "existing": [{"name": "기존업체", "retrieval_rank": 1}],
                    "search_api_v2": [{"name": "신규업체", "retrieval_rank": 1}],
                },
            }],
        }
        self.assertEqual(set(_item_sources(snapshot["items"][0])), {"existing", "search_api_v2"})
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            labels_path = Path(directory) / "labels.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            labels = build_label_sheet(snapshot_path, labels_path, depth=5)
            self.assertEqual(len(labels["items"][0]["candidates"]), 2)

    def test_preserves_human_reference_when_rebuilding_label_sheet(self):
        snapshot = {
            "schema_version": 3,
            "created_at": "2026-08-26T00:00:00+00:00",
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트 품목",
                "sources": {"search_api": [{"name": "검색된 업체", "retrieval_rank": 1}]},
            }],
        }
        labels = {
            "items": [{
                "item_code": "ITEM-1",
                "candidates": [{
                    "candidate_name": "사람이 찾은 업체",
                    "domains": ["human.example.com"],
                    "aliases": [],
                    "observed_sources": [{"source": "human_reference", "rank": None, "url": None}],
                    "relevance": 3,
                    "evidence": "독립 확인",
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            labels_path = Path(directory) / "labels.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            labels_path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")

            rebuilt = build_label_sheet(snapshot_path, labels_path, depth=5)
            candidates = rebuilt["items"][0]["candidates"]
            human = next(candidate for candidate in candidates if candidate["candidate_name"] == "사람이 찾은 업체")
            self.assertEqual(human["relevance"], 3)
            self.assertEqual(human["observed_sources"][0]["source"], "human_reference")


class DynamicSourceEvaluationTest(unittest.TestCase):
    def test_evaluates_custom_sources_and_builds_hybrid(self):
        created_at = "2026-08-26T00:00:00+00:00"
        snapshot = {
            "schema_version": 3,
            "created_at": created_at,
            "items": [{
                "item_code": "ITEM-1",
                "item_name": "테스트 품목",
                "sources": {
                    "vector_v2": [{"name": "업체A", "retrieval_rank": 1}],
                    "search_api_v2": [{"name": "업체B", "retrieval_rank": 1}],
                    "existing": [],
                },
            }],
        }
        gold = {
            "source_snapshot_created_at": created_at,
            "items": [{
                "item_code": "ITEM-1",
                "judgment_status": "complete",
                "judgments": [
                    {"vendor_name": "업체A", "relevance": 3},
                    {"vendor_name": "업체B", "relevance": 2},
                ],
                "relevant_vendors": [
                    {"vendor_name": "업체A", "relevance": 3},
                    {"vendor_name": "업체B", "relevance": 2},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            gold_path = Path(directory) / "gold.json"
            report_path = Path(directory) / "report.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
            report = evaluate(snapshot_path, gold_path, report_path, (1,))
            self.assertEqual(
                set(report["aggregate_macro_average"]),
                {"vector_v2", "search_api_v2", "hybrid"},
            )
            self.assertEqual(
                report["gold_diagnostics"]["items_without_independent_references"],
                ["ITEM-1"],
            )


@unittest.skipIf(export_queries is None, "ERP 평가 query 생성기가 없어 테스트 생략")
class EvaluationQueryMergeTest(unittest.TestCase):
    def test_merges_generic_group_and_preserves_selection(self):
        existing = {
            "schema_version": 2,
            "evaluation_selection": {
                "mode": "pilot",
                "description": "기존 선택",
                "item_codes": ["OLD-001"],
                "label_depth": 5,
            },
            "items": [{
                "item_code": "OLD-001",
                "item_name": "기존 품목",
                "item_group": "기존 그룹",
                "enabled": True,
            }],
        }
        source = {
            "items": [{
                "item_code": "NEW-001",
                "item_name": "새 품목",
                "item_group": "새 그룹",
                "disabled": 0,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "queries.json"
            source_path = Path(directory) / "source.json"
            output_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            merged = export_queries(
                output_path,
                groups=("새 그룹",),
                pilot_codes=None,
                source_path=source_path,
            )
            self.assertEqual(len(merged["items"]), 2)
            self.assertEqual(merged["evaluation_selection"]["item_codes"], ["OLD-001"])
            new_item = next(item for item in merged["items"] if item["item_code"] == "NEW-001")
            self.assertTrue(new_item["enabled"])
            self.assertNotIn("erpnext_import_enabled", new_item)

    def test_replaces_retired_evaluation_group_and_selection(self):
        existing = {
            "schema_version": 2,
            "evaluation_selection": {
                "item_codes": ["SAFE-001", "OLD-REA-001"],
                "label_depth": 5,
            },
            "items": [
                {"item_code": "SAFE-001", "item_name": "안전 품목", "item_group": "안전용품"},
                {"item_code": "OLD-REA-001", "item_name": "이전 시약", "item_group": "시약"},
            ],
        }
        source = {
            "items": [{
                "item_code": "MCH-001",
                "item_name": "기계 품목",
                "item_group": "기계부품",
                "disabled": 0,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "queries.json"
            source_path = Path(directory) / "source.json"
            output_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

            merged = export_queries(
                output_path,
                groups=("기계부품",),
                pilot_codes=("MCH-001",),
                source_path=source_path,
                remove_groups=("시약",),
            )
            self.assertEqual(
                merged["evaluation_selection"]["item_codes"],
                ["SAFE-001", "MCH-001"],
            )
            self.assertEqual(
                {item["item_code"] for item in merged["items"]},
                {"SAFE-001", "MCH-001"},
            )


class EvaluationDatasetTest(unittest.TestCase):
    def test_selected_item_codes_exist_in_queries(self):
        base_dir = Path(__file__).resolve().parent
        queries = json.loads(
            (base_dir / "vendor_retrieval_queries.json").read_text(encoding="utf-8")
        )
        reference_files = {
            "references_safety.json": "안전용품",
            "references_office.json": "사무용품",
            "references_mechanical_parts.json": "기계부품",
        }
        references = [
            record
            for file_name in reference_files
            for record in json.loads(
                (base_dir / file_name).read_text(encoding="utf-8")
            )
        ]

        query_codes = {item["item_code"] for item in queries["items"]}
        selected_codes = set(queries["evaluation_selection"]["item_codes"])
        reference_codes = {record["item_code"] for record in references}

        self.assertTrue(selected_codes.issubset(query_codes))
        self.assertTrue(selected_codes.issubset(reference_codes))
        self.assertEqual(len(selected_codes), 15)
        self.assertEqual(len(references), 30)
        selected_group_counts = {
            group: sum(
                item["item_code"] in selected_codes and item["item_group"] == group
                for item in queries["items"]
            )
            for group in reference_files.values()
        }
        self.assertEqual(
            selected_group_counts,
            {"안전용품": 5, "사무용품": 5, "기계부품": 5},
        )
        query_name_by_code = {
            item["item_code"]: item["item_name"] for item in queries["items"]
        }
        for item_code in selected_codes:
            item_templates = [
                record for record in references
                if record["item_code"] == item_code
            ]
            self.assertEqual(len(item_templates), 2)
            self.assertEqual(
                {record["relevance"] for record in item_templates},
                {2, 3},
            )
            self.assertTrue(all(
                record["item_name"] == query_name_by_code[item_code]
                for record in item_templates
            ))


if __name__ == "__main__":
    unittest.main()
