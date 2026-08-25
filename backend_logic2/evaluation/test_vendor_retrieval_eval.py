import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from vendor_retrieval_eval import (  # noqa: E402
    build_gold,
    build_label_sheet,
    candidate_matches_gold,
    is_document_candidate,
    is_valid_vendor_candidate,
    metrics_at_k,
    reciprocal_rank_fusion,
)
try:
    from build_erpnext_item_import import validate_items  # noqa: E402
except ModuleNotFoundError:
    validate_items = None


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
            "source_channel": "naver_local",
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


@unittest.skipIf(
    validate_items is None,
    "build_erpnext_item_import.py가 현재 작업공간에 없어 ERP 데이터셋 검증 생략",
)
class ErpItemDatasetTest(unittest.TestCase):
    def test_query_csv_and_gold_codes_stay_in_sync(self):
        base_dir = Path(__file__).resolve().parent
        queries = json.loads((base_dir / "vendor_retrieval_queries.json").read_text(encoding="utf-8"))
        gold = json.loads((base_dir / "vendor_retrieval_gold.json").read_text(encoding="utf-8"))

        validate_items(queries["items"])
        query_codes = [item["item_code"] for item in queries["items"]]
        gold_codes = [item["item_code"] for item in gold["items"]]
        self.assertEqual(query_codes, gold_codes)
        self.assertEqual(len(query_codes), 36)

        with (base_dir / "erpnext_item_import_safety.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            csv_codes = [row["Item Code"] for row in csv.DictReader(file)]
        self.assertEqual(query_codes, csv_codes)

        alternatives = [item for item in queries["items"] if item.get("alternative_for")]
        self.assertEqual(len(alternatives), 12)
        with (base_dir / "erpnext_item_alternative_import.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            alternative_rows = list(csv.DictReader(file))
        self.assertEqual(len(alternative_rows), 12)
        expected_pairs = {
            (item["alternative_for"], item["item_code"])
            for item in alternatives
        }
        actual_pairs = {
            (row["Item Code"], row["Alternative Item Code"])
            for row in alternative_rows
        }
        self.assertEqual(expected_pairs, actual_pairs)
        self.assertTrue(all(row["Two-way"] == "1" for row in alternative_rows))

    def test_stock_reconciliation_matches_all_items(self):
        base_dir = Path(__file__).resolve().parent
        queries = json.loads(
            (base_dir / "vendor_retrieval_queries.json").read_text(encoding="utf-8")
        )
        items = queries["items"]
        stock_config = queries["erpnext_stock_reconciliation_import"]

        quantities = [item["opening_qty"] for item in items]
        self.assertEqual(len(quantities), 36)
        self.assertEqual(len(set(quantities)), 36)
        self.assertTrue(all(quantity > 0 for quantity in quantities))

        with (base_dir / stock_config["csv_file"]).open(
            encoding="utf-8-sig", newline=""
        ) as file:
            rows = list(csv.DictReader(file))

        self.assertEqual(len(rows), 36)
        self.assertEqual(
            [item["item_code"] for item in items],
            [row["Item Code"] for row in rows],
        )
        self.assertEqual(
            quantities,
            [int(row["Quantity"]) for row in rows],
        )
        self.assertTrue(
            all(row["Warehouse"] == stock_config["warehouse"] for row in rows)
        )
        self.assertEqual(
            list(rows[0]),
            ["Item Code", "Warehouse", "Quantity", "Valuation Rate"],
        )
        self.assertTrue(all(float(row["Valuation Rate"]) > 0 for row in rows))


if __name__ == "__main__":
    unittest.main()
