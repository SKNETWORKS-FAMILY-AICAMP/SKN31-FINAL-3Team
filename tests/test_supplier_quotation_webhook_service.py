import unittest
from unittest.mock import Mock, patch

from backend_logic2.services import quotation_service


class SupplierQuotationWebhookServiceTests(unittest.TestCase):
    @patch.object(quotation_service, "get_quotations_for_rfq")
    def test_snapshot_uses_actual_rfq_recipients_as_denominator(self, get_quotations):
        get_quotations.return_value = [
            {"name": "SQ-1", "supplier": "Supplier A", "grand_total": 1000},
            {"name": "SQ-X", "supplier": "Unrelated Supplier", "grand_total": 900},
        ]
        case = {
            "workflow_snapshot": {
                "values": {"selected_suppliers": ["Supplier A", "Supplier B", "Supplier C"]}
            }
        }

        snapshot = quotation_service.build_quotation_snapshot(case, "RFQ-1")

        self.assertEqual(snapshot["recipient_count"], 3)
        self.assertEqual(snapshot["responded_count"], 1)
        self.assertEqual(snapshot["response_rate"], 33)
        self.assertEqual(snapshot["responded_suppliers"], ["Supplier A"])

    def test_webhook_resolves_rfq_from_items_and_refreshes_case(self):
        payload = {
            "event": "after_insert",
            "doc": {
                "name": "SQ-1",
                "modified": "2026-09-03 10:00:00",
                "items": [{"request_for_quotation": "RFQ-1"}],
            },
        }
        case = {"case_id": "case-1", "mr_name": "MR-1"}
        updated = {
            **case,
            "quotation_snapshot": {"rfq_name": "RFQ-1", "responded_count": 1},
        }
        with (
            patch.object(
                quotation_service.event_repository,
                "begin_event",
                return_value=({"event_id": "event-1"}, True),
            ),
            patch.object(quotation_service.event_repository, "complete_event") as complete,
            patch.object(quotation_service.case_repository, "get_case_by_rfq", return_value=case),
            patch.object(
                quotation_service,
                "refresh_case_quotations",
                return_value=(updated, True),
            ) as refresh,
        ):
            rows, created = quotation_service.register_supplier_quotation_event(payload)

        self.assertTrue(created)
        self.assertEqual(rows[0]["case_id"], "case-1")
        self.assertTrue(rows[0]["changed"])
        refresh.assert_called_once_with(case, rfq_name="RFQ-1", notify=True)
        complete.assert_called_once_with("event-1")

    def test_empty_initial_snapshot_does_not_create_user_notification(self):
        case = {
            "case_id": "case-1",
            "mr_name": "MR-1",
            "assigned_user_id": "buyer",
            "quotation_snapshot": {},
            "workflow_snapshot": {
                "values": {"rfq_name": "RFQ-1", "selected_suppliers": ["Supplier A"]}
            },
        }
        snapshot = {
            "rfq_name": "RFQ-1",
            "recipient_suppliers": ["Supplier A"],
            "responded_suppliers": [],
            "recipient_count": 1,
            "responded_count": 0,
            "response_rate": 0,
            "quotations": [],
        }
        with (
            patch.object(quotation_service, "build_quotation_snapshot", return_value=snapshot),
            patch.object(
                quotation_service.case_repository,
                "update_quotation_snapshot",
                return_value=({**case, "quotation_snapshot": snapshot}, True),
            ),
            patch.object(
                quotation_service.notification_repository, "create_notification"
            ) as notify,
        ):
            _, changed = quotation_service.refresh_case_quotations(case)

        self.assertTrue(changed)
        notify.assert_not_called()

    def test_received_rfq_email_attachment_is_extracted_and_registered(self):
        payload = {
            "event": "after_insert",
            "doc": {
                "name": "COMM-1",
                "sent_or_received": "Received",
                "communication_medium": "Email",
                "sender": "Vendor <vendor@example.com>",
                "reference_doctype": "Request for Quotation",
                "reference_name": "RFQ-1",
            },
        }
        attachment = {
            "name": "FILE-1",
            "file_name": "quotation.jpg",
            "content_hash": "hash-1",
        }
        rfq = {
            "name": "RFQ-1",
            "suppliers": [{
                "supplier": "SUP-1",
                "supplier_name": "공급사 1",
                "email_id": "vendor@example.com",
            }],
        }
        requirements = Mock()
        requirements.model_dump.return_value = {"rfq_name": "RFQ-1", "items": []}

        def parser(*_args):
            return {
                "quotation_id": "EST-001",
                "currency": "KRW",
                "subtotal": 1000,
                "tax_amount": 100,
                "total_amount": 1100,
                "items": [{
                    "item_name": "품목 A",
                    "quantity": 1,
                    "unit_price": 1000,
                    "amount": 1000,
                }],
            }

        with (
            patch.object(
                quotation_service.event_repository,
                "begin_event",
                return_value=({"event_id": "event-1"}, True),
            ),
            patch.object(quotation_service.event_repository, "complete_event") as complete,
            patch.object(
                quotation_service,
                "erp_get_communication_attachments",
                return_value=[attachment],
            ),
            patch.object(
                quotation_service,
                "erp_get_one",
                side_effect=lambda doctype, _name: rfq if doctype == "Request for Quotation" else {},
            ),
            patch.object(
                quotation_service,
                "erp_download_file",
                return_value={
                    "content": b"image-bytes",
                    "content_type": "image/jpeg",
                },
            ) as download,
            patch.object(quotation_service, "load_rfq_requirements", return_value=requirements),
            patch.object(
                quotation_service,
                "register_supplier_quotation",
                return_value={"status": "created", "name": "SQ-1"},
            ) as register,
            patch.object(quotation_service.case_repository, "get_case_by_rfq", return_value=None),
        ):
            result, created = quotation_service.register_quotation_email_event(
                payload,
                model_parser=parser,
            )

        self.assertTrue(created)
        self.assertEqual(result["rfq_name"], "RFQ-1")
        self.assertEqual(result["supplier"], "SUP-1")
        quotation = register.call_args.args[0]
        self.assertIsNone(quotation.source.path)
        self.assertEqual(quotation.source.message_id, "COMM-1")
        download.assert_called_once_with(
            "FILE-1",
            expected_attached_to_doctype="Communication",
        )
        complete.assert_called_once_with("event-1")


if __name__ == "__main__":
    unittest.main()
