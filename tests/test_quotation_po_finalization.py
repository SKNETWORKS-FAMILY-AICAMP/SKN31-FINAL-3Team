import unittest
from unittest.mock import patch

from backend_logic2.nodes.po.create_and_send_po import (
    build_po_portal_link,
    create_and_send_direct_po,
    create_and_send_po,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
    _enrich_ranking_with_prices,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    submit_finalized_quotations,
)
from backend_logic2.workflow.process_commands import check_quotations_command


class QuotationAndPurchaseOrderFinalizationTests(unittest.TestCase):
    def test_po_portal_link_uses_purchase_order_route(self):
        link = build_po_portal_link(
            "PUR-ORD-0001",
            domain="https://erp.example.com/",
        )

        self.assertEqual(
            link,
            "https://erp.example.com/purchase-orders/PUR-ORD-0001",
        )
        self.assertNotIn("/orders/", link)

    def test_po_portal_link_normalizes_missing_path_slash(self):
        link = build_po_portal_link(
            "PUR-ORD-0002",
            domain="https://erp.example.com",
            path_template="purchase-orders/{po_name}",
        )

        self.assertEqual(
            link,
            "https://erp.example.com/purchase-orders/PUR-ORD-0002",
        )

    def test_ranking_keeps_supplier_quotation_prices_for_frontend(self):
        ranking = [{"name": "SUP-QTN-0001", "supplier": "공급사 A", "rank": 1}]
        quotations = [{
            "name": "SUP-QTN-0001",
            "supplier": "공급사 A",
            "currency": "KRW",
            "transaction_date": "2026-09-03",
            "grand_total": 220000,
            "items": [{
                "qty": 2,
                "rate": 100000,
                "amount": 200000,
                "expected_delivery_date": "2026-09-10",
            }],
        }]

        enriched = _enrich_ranking_with_prices(ranking, quotations)

        self.assertEqual(enriched[0]["rate"], 100000)
        self.assertEqual(enriched[0]["amount"], 200000)
        self.assertEqual(enriched[0]["total_amount"], 220000)
        self.assertEqual(enriched[0]["expected_delivery_date"], "2026-09-10")
        self.assertEqual(enriched[0]["transaction_date"], "2026-09-03")

    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_registrar.submit_finalized_quotations")
    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.print_evaluation")
    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs")
    @patch("backend_logic2.workflow.process_commands.interrupt")
    def test_finalize_submits_ranked_quotes_before_supplier_selection(
        self,
        interrupt,
        evaluate,
        _print_evaluation,
        submit_finalized,
    ):
        ranking = [{
            "name": "SUP-QTN-0001",
            "supplier": "공급사 A",
            "rank": 1,
            "rfq_name": "PUR-RFQ-0001",
            "rfq_round": 1,
        }]
        interrupt.return_value = {"decision": "finalize", "supplier": "공급사 A"}
        evaluate.return_value = {"quotations": [{}], "ranking": ranking}

        command = check_quotations_command({"rfq_name": "PUR-RFQ-0001"})

        submit_finalized.assert_called_once_with(ranking)
        self.assertEqual(command.goto, "final_selection")
        self.assertEqual(command.update["requested_supplier"], "공급사 A")

    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_registrar.erp_submit")
    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_registrar.erp_get_one")
    def test_finalize_submits_draft_supplier_quotation(self, get_one, submit):
        get_one.return_value = {
            "name": "SUP-QTN-0001",
            "docstatus": 0,
            "items": [{"request_for_quotation": "PUR-RFQ-0001"}],
        }

        result = submit_finalized_quotations([{
            "name": "SUP-QTN-0001",
            "supplier": "공급사 A",
            "rfq_name": "PUR-RFQ-0001",
        }])

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

    @patch("backend_logic2.nodes.po.create_and_send_po.erp_send_email")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_submit")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_post")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get_one")
    @patch("backend_logic2.nodes.po.create_and_send_po.get_quotations_for_rfq")
    def test_rfq_po_email_contains_purchase_order_portal_link(
        self,
        get_quotations,
        get_one,
        get_many,
        post,
        _submit,
        send_email,
    ):
        get_quotations.return_value = [{
            "name": "SUP-QTN-0001",
            "supplier": "공급사 A",
            "items": [{
                "name": "SQI-0001",
                "item_code": "ITEM-001",
                "qty": 2,
                "rate": 1000,
                "expected_delivery_date": "2099-09-10",
                "request_for_quotation_item": "RFQI-0001",
            }],
        }]
        get_many.side_effect = [[], [{"email_id": "supplier@example.com"}]]
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
        send_email.return_value = {"email_sent": True}

        with (
            patch(
                "backend_logic2.nodes.po.create_and_send_po.ERP_DOMAIN",
                "https://erp.example.com",
            ),
            patch(
                "backend_logic2.nodes.po.create_and_send_po.ERP_PORTAL_PATH_TEMPLATE",
                "/purchase-orders/{po_name}",
            ),
        ):
            result = create_and_send_po(
                "PUR-RFQ-0001",
                "공급사 A",
                mr_name="MAT-MR-0001",
            )

        email_html = send_email.call_args.args[4]
        self.assertIn(
            "https://erp.example.com/purchase-orders/PUR-ORD-0001",
            email_html,
        )
        self.assertTrue(result["email_sent"])

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

    @patch("backend_logic2.nodes.po.create_and_send_po.erp_send_email")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_submit")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_post")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get")
    @patch("backend_logic2.nodes.po.create_and_send_po.erp_get_one")
    def test_direct_po_email_contains_purchase_order_portal_link(
        self,
        get_one,
        get_many,
        post,
        _submit,
        send_email,
    ):
        get_one.side_effect = [
            {
                "name": "MAT-MR-0001",
                "docstatus": 1,
                "items": [{
                    "name": "MRI-0001",
                    "item_code": "ITEM-001",
                    "qty": 3,
                    "schedule_date": "2099-09-20",
                }],
            },
            {"items": [{
                "item_code": "ITEM-001",
                "material_request": "MAT-MR-0001",
                "material_request_item": "MRI-0001",
            }]},
        ]
        get_many.side_effect = [[], [{"email_id": "supplier@example.com"}]]
        post.return_value = {"name": "PUR-ORD-0002"}
        send_email.return_value = {"email_sent": True}

        with (
            patch(
                "backend_logic2.nodes.po.create_and_send_po.ERP_DOMAIN",
                "https://erp.example.com",
            ),
            patch(
                "backend_logic2.nodes.po.create_and_send_po.ERP_PORTAL_PATH_TEMPLATE",
                "/purchase-orders/{po_name}",
            ),
        ):
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
            )

        email_html = send_email.call_args.args[4]
        self.assertIn(
            "https://erp.example.com/purchase-orders/PUR-ORD-0002",
            email_html,
        )
        self.assertTrue(result["email_sent"])


if __name__ == "__main__":
    unittest.main()
