import unittest
from unittest.mock import patch

from backend_logic2.nodes.po.create_and_send_po import (
    create_and_send_direct_po,
    create_and_send_po,
)
from backend_logic2.nodes.quotation.sq_evaluation import submit_finalized_quotations
from backend_logic2.workflow.process_commands import check_quotations_command


class QuotationAndPurchaseOrderFinalizationTests(unittest.TestCase):
    @patch("backend_logic2.nodes.quotation.sq_evaluation.submit_finalized_quotations")
    @patch("backend_logic2.nodes.quotation.sq_evaluation.print_evaluation")
    @patch("backend_logic2.nodes.quotation.sq_evaluation.evaluate_quotations")
    @patch("backend_logic2.workflow.process_commands.interrupt")
    def test_finalize_submits_ranked_quotes_before_supplier_selection(
        self,
        interrupt,
        evaluate,
        _print_evaluation,
        submit_finalized,
    ):
        ranking = [{"name": "SUP-QTN-0001", "supplier": "공급사 A", "rank": 1}]
        interrupt.return_value = {"decision": "finalize", "supplier": "공급사 A"}
        evaluate.return_value = {"quotations": [{}], "ranking": ranking}

        command = check_quotations_command({"rfq_name": "PUR-RFQ-0001"})

        submit_finalized.assert_called_once_with("PUR-RFQ-0001", ranking)
        self.assertEqual(command.goto, "final_selection")
        self.assertEqual(command.update["requested_supplier"], "공급사 A")

    @patch("backend_logic2.nodes.quotation.sq_evaluation.erp_submit")
    @patch("backend_logic2.nodes.quotation.sq_evaluation.erp_get_one")
    def test_finalize_submits_draft_supplier_quotation(self, get_one, submit):
        get_one.return_value = {
            "name": "SUP-QTN-0001",
            "docstatus": 0,
            "items": [{"request_for_quotation": "PUR-RFQ-0001"}],
        }

        result = submit_finalized_quotations(
            "PUR-RFQ-0001",
            [{"name": "SUP-QTN-0001", "supplier": "공급사 A"}],
        )

        self.assertEqual(result, ["SUP-QTN-0001"])
        submit.assert_called_once_with("Supplier Quotation", "SUP-QTN-0001")

    @patch("backend_logic2.nodes.po.create_and_send_po.erp_submit")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_post")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get_one")
    @patch("backend_logic2.nodes.po.create_and_send_po.get_quotations_for_rfq")
    def test_po_items_retain_material_request_links(
        self,
        get_quotations,
        get_one,
        get_many,
        post,
        submit,
    ):
        get_quotations.return_value = [{
            "name": "SUP-QTN-0001",
            "supplier": "공급사 A",
            "items": [{
                "name": "SQI-0001",
                "item_code": "ITEM-001",
                "qty": 2,
                "rate": 1000,
                "expected_delivery_date": "2026-09-10",
                "request_for_quotation_item": "RFQI-0001",
            }],
        }]
        get_many.return_value = []
        get_one.side_effect = [
            {"items": [{
                "name": "RFQI-0001",
                "item_code": "ITEM-001",
                "material_request": "MAT-MR-0001",
                "material_request_item": "MRI-0001",
            }]},
            {"items": [{"name": "MRI-0001", "item_code": "ITEM-001"}]},
            {"items": [{
                "item_code": "ITEM-001",
                "material_request": "MAT-MR-0001",
                "material_request_item": "MRI-0001",
            }]},
        ]
        post.return_value = {"name": "PUR-ORD-0001"}

        result = create_and_send_po(
            "PUR-RFQ-0001",
            "공급사 A",
            mr_name="MAT-MR-0001",
            send_email=False,
        )

        po_item = post.call_args.args[1]["items"][0]
        self.assertEqual(po_item["material_request"], "MAT-MR-0001")
        self.assertEqual(po_item["material_request_item"], "MRI-0001")
        self.assertEqual(po_item["supplier_quotation"], "SUP-QTN-0001")
        submit.assert_called_once_with("Purchase Order", "PUR-ORD-0001")
        self.assertEqual(result["name"], "PUR-ORD-0001")

    @patch("backend_logic2.nodes.po.create_and_send_po.erp_submit")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_post")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get_one")
    def test_direct_po_uses_recent_transaction_and_retains_mr_links(
        self,
        get_one,
        get_many,
        post,
        submit,
    ):
        get_one.side_effect = [
            {
                "name": "MAT-MR-0001",
                "docstatus": 1,
                "items": [{
                    "name": "MRI-0001",
                    "item_code": "ITEM-001",
                    "qty": 3,
                    "uom": "Nos",
                    "warehouse": "Stores - T",
                    "schedule_date": "2099-09-20",
                }],
            },
            {
                "name": "PUR-ORD-0002",
                "items": [{
                    "item_code": "ITEM-001",
                    "material_request": "MAT-MR-0001",
                    "material_request_item": "MRI-0001",
                }],
            },
        ]
        get_many.return_value = []
        post.return_value = {"name": "PUR-ORD-0002"}

        result = create_and_send_direct_po(
            "MAT-MR-0001",
            "공급사 A",
            {
                "ITEM-001": {
                    "supplier": "공급사 A",
                    "rate": 1250,
                    "reference_po": "PUR-ORD-OLD",
                }
            },
            send_email=False,
        )

        payload = post.call_args.args[1]
        self.assertEqual(payload["supplier"], "공급사 A")
        self.assertEqual(payload["items"][0]["rate"], 1250)
        self.assertEqual(payload["items"][0]["material_request"], "MAT-MR-0001")
        self.assertEqual(payload["items"][0]["material_request_item"], "MRI-0001")
        submit.assert_called_once_with("Purchase Order", "PUR-ORD-0002")
        self.assertTrue(result["direct_purchase"])


if __name__ == "__main__":
    unittest.main()
