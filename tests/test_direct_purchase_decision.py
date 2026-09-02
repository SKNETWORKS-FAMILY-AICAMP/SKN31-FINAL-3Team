import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backend_logic2.nodes.mr.decide_bidding import _decide_one_item
from backend_logic2.workflow.process_commands import decide_bidding_choice_command


class DirectPurchaseDecisionTests(unittest.TestCase):
    @patch("backend_logic2.nodes.mr.decide_bidding._get_past_purchases")
    @patch("backend_logic2.nodes.mr.decide_bidding.erp_get_one")
    def test_recent_supplier_and_rate_are_preserved_for_direct_purchase(
        self, get_one, get_purchases
    ):
        get_one.return_value = {"supplier_items": []}
        get_purchases.return_value = [{
            "date": date.today() - timedelta(days=30),
            "rate": 1500,
            "supplier": "공급사 A",
            "purchase_order": "PUR-ORD-OLD",
        }]

        item_code, result = _decide_one_item({
            "item_code": "ITEM-001",
            "qty": 2,
            "schedule_date": date.today() + timedelta(days=30),
        })

        self.assertEqual(item_code, "ITEM-001")
        self.assertFalse(result["needs_bidding"])
        self.assertEqual(result["direct_supplier"], "공급사 A")
        self.assertEqual(result["last_rate"], 1500)
        self.assertEqual(result["reference_po"], "PUR-ORD-OLD")

    @patch("backend_logic2.nodes.mr.decide_bidding.decide_bidding")
    def test_non_bidding_result_routes_to_po_approval(self, decide_bidding):
        decide_bidding.return_value = {
            "ITEM-001": {
                "needs_bidding": False,
                "reasons": ["최근 거래 반복구매"],
                "direct_supplier": "공급사 A",
                "last_rate": 1500,
                "reference_po": "PUR-ORD-OLD",
                "reference_date": "2026-08-01",
            }
        }

        command = decide_bidding_choice_command({"mr_name": "MAT-MR-0001"})

        self.assertEqual(command.goto, "po_approval")
        self.assertEqual(command.update["status"], "awaiting_po_approval")
        self.assertEqual(command.update["selected_supplier"], "공급사 A")
        self.assertTrue(command.update["direct_purchase"])


if __name__ == "__main__":
    unittest.main()
