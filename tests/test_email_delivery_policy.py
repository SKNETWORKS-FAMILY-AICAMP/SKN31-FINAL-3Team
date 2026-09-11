import os
import json
import tempfile
import unittest
from pathlib import Path
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
    def test_custom_only_marks_only_allowlisted_supplier_row_for_email(
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

        with patch.dict(
            os.environ,
            {
                "TEST_MODE": "custom_only",
                "EMAIL_RECIPIENT_ALLOWLIST": "manual@example.com",
                "EMAIL_RECIPIENT_ALLOWLIST_PATH": "",
                "TEST_RECIPIENT_OVERRIDE": "",
            },
        ):
            send_rfq.create_rfq(
                "MR-1",
                ["AI 추천사", "직접 추가사"],
                send_email=True,
                email_supplier_names=None,
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
    def test_workflow_delegates_address_filtering_in_custom_only_mode(
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
            email_supplier_names=None,
            submit=True,
        )

    @patch.object(send_rfq, "erp_submit")
    @patch.object(send_rfq, "erp_post")
    @patch.object(send_rfq, "erp_get_one")
    def test_send_all_keeps_contact_only_supplier_enabled(
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
            {"name": "연락처 공급사", "supplier_primary_contact": "CONTACT-1"},
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

        with patch.dict(
            os.environ,
            {"TEST_MODE": "false", "TEST_RECIPIENT_OVERRIDE": ""},
        ):
            send_rfq.create_rfq(
                "MR-1",
                ["연락처 공급사"],
                send_email=True,
            )

        suppliers = post.call_args.args[1]["suppliers"]
        self.assertEqual(suppliers[0]["send_email"], 1)
        self.assertEqual(suppliers[0]["contact"], "CONTACT-1")
        submit.assert_called_once_with("Request for Quotation", "RFQ-1")

    @patch.object(erp_client.requests, "post")
    def test_direct_email_custom_only_sends_only_allowlisted_addresses(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"message": {"name": "COMM-1"}}

        with patch.dict(
            os.environ,
            {
                "TEST_MODE": "custom_only",
                "EMAIL_RECIPIENT_ALLOWLIST": "SAFE@example.com",
                "EMAIL_RECIPIENT_ALLOWLIST_PATH": "",
            },
        ):
            erp_client.erp_send_email(
                "Purchase Order",
                "PO-1",
                ["safe@example.com", "blocked@example.com"],
                "subject",
                "content",
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["recipients"], ["safe@example.com"])

    @patch.object(erp_client.requests, "post")
    def test_direct_email_custom_only_blocks_when_allowlist_is_empty(self, post):
        with patch.dict(
            os.environ,
            {
                "TEST_MODE": "custom_only",
                "EMAIL_RECIPIENT_ALLOWLIST": "",
                "EMAIL_RECIPIENT_ALLOWLIST_PATH": "",
            },
        ):
            result = erp_client.erp_send_email(
                "Material Request",
                "MR-1",
                "supplier@example.com",
                "subject",
                "content",
            )

        post.assert_not_called()
        self.assertFalse(result["email_sent"])
        self.assertEqual(result["email_policy"], "custom_only")

    def test_allowlist_parser_normalizes_common_address_formats(self):
        with patch.dict(
            os.environ,
            {
                "TEST_MODE": "custom_only",
                "EMAIL_RECIPIENT_ALLOWLIST_PATH": "",
                "EMAIL_RECIPIENT_ALLOWLIST": (
                    "Supplier One <ONE@example.com>; two@example.com"
                ),
            },
        ):
            self.assertEqual(
                erp_client.filter_email_recipients(
                    ["one@example.com", "TWO@example.com", "other@example.com"]
                ),
                ["one@example.com", "two@example.com"],
            )

    def test_json_allowlist_is_reloaded_without_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allowlist.json"
            path.write_text(
                json.dumps({"enabled": True, "recipients": ["first@example.com"]}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "TEST_MODE": "custom_only",
                    "EMAIL_RECIPIENT_ALLOWLIST_PATH": str(path),
                },
            ):
                self.assertEqual(
                    erp_client.filter_email_recipients(
                        ["first@example.com", "second@example.com"]
                    ),
                    ["first@example.com"],
                )

                path.write_text(
                    json.dumps(
                        {"enabled": True, "recipients": ["second@example.com"]}
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    erp_client.filter_email_recipients(
                        ["first@example.com", "second@example.com"]
                    ),
                    ["second@example.com"],
                )

    def test_invalid_configured_json_allowlist_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allowlist.json"
            path.write_text("{invalid", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "TEST_MODE": "custom_only",
                    "EMAIL_RECIPIENT_ALLOWLIST_PATH": str(path),
                },
            ):
                self.assertEqual(
                    erp_client.filter_email_recipients("safe@example.com"),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
