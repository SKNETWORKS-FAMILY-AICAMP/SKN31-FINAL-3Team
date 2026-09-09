import os
import unittest
from unittest.mock import patch

from backend_logic2.integrations import erp_client
from backend_logic2.nodes.rfq import send_rfq
from backend_logic2.workflow import process_commands


class EmailDeliveryPolicyTests(unittest.TestCase):
    def test_test_mode_values_are_fail_closed_and_backward_compatible(self):
        expectations = {
            "true": ("block_all", True),
            "custom_only": ("custom_only", True),
            "false": ("send_all", False),
            "unexpected": ("block_all", True),
        }
        for value, expected in expectations.items():
            with self.subTest(value=value), patch.dict(os.environ, {"TEST_MODE": value}):
                self.assertEqual(erp_client.get_email_delivery_policy(), expected[0])
                self.assertEqual(erp_client.is_test_mode(), expected[1])

    @patch.object(send_rfq, "erp_submit")
    @patch.object(send_rfq, "erp_post")
    @patch.object(send_rfq, "erp_get_one")
    def test_custom_only_marks_only_manual_supplier_row_for_email(
        self, get_one, post, submit
    ):
        get_one.side_effect = [
            {
                "docstatus": 1,
                "company": "BiddingFlow",
                "items": [
                    {
                        "name": "MRI-1",
                        "item_code": "ITEM-1",
                        "qty": 1,
                        "uom": "Nos",
                    }
                ],
            },
            {"name": "AI 추천사", "email_id": "ai@example.com"},
            {"name": "직접 추가사", "email_id": "manual@example.com"},
            {
                "name": "RFQ-1",
                "items": [
                    {
                        "material_request": "MR-1",
                        "material_request_item": "MRI-1",
                    }
                ],
            },
        ]
        post.return_value = {"name": "RFQ-1"}

        send_rfq.create_rfq(
            "MR-1",
            ["AI 추천사", "직접 추가사"],
            send_email=True,
            email_supplier_names=["직접 추가사"],
        )

        suppliers = post.call_args.args[1]["suppliers"]
        self.assertEqual(
            [(row["supplier"], row["send_email"]) for row in suppliers],
            [("AI 추천사", 0), ("직접 추가사", 1)],
        )
        submit.assert_called_once_with("Request for Quotation", "RFQ-1")

    @patch("backend_logic2.nodes.rfq.send_rfq.create_and_send_rfq")
    @patch(
        "backend_logic2.integrations.erp_client.get_email_delivery_policy",
        return_value="custom_only",
    )
    def test_workflow_passes_only_manual_targets_in_custom_only_mode(
        self, _policy, create_and_send
    ):
        create_and_send.return_value = {"name": "RFQ-1"}

        command = process_commands.create_rfq_command(
            {
                "mr_name": "MR-1",
                "selected_suppliers": ["AI 추천사", "직접 추가사"],
                "custom_rfq_suppliers": ["직접 추가사"],
            }
        )

        self.assertEqual(command.goto, "check_quotations")
        create_and_send.assert_called_once_with(
            "MR-1",
            ["AI 추천사", "직접 추가사"],
            send_email=True,
            email_supplier_names=["직접 추가사"],
            submit=True,
        )


if __name__ == "__main__":
    unittest.main()
