import unittest
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from backend_logic2.repositories import events
from backend_logic2.services import receipt_service
from backend_logic2.workflow.process_commands import to_checkpoint_data


class PurchaseDocumentTrackingTests(unittest.TestCase):
    def test_checkpoint_payload_serializes_database_uuid(self):
        value = uuid4()
        self.assertEqual(to_checkpoint_data({"delivery_id": value}), {"delivery_id": str(value)})

    @patch.object(receipt_service, "erp_get")
    def test_parent_lookup_uses_supported_frappe_child_filter(self, erp_get):
        erp_get.return_value = [{"name": "MAT-PRE-0001"}]

        result = receipt_service._parent_names(
            "Purchase Receipt", "Purchase Receipt Item", "purchase_order", "PUR-ORD-0001"
        )

        self.assertEqual(result, ["MAT-PRE-0001"])
        self.assertEqual(
            erp_get.call_args.kwargs["filters"],
            [["Purchase Receipt Item", "purchase_order", "=", "PUR-ORD-0001"]],
        )

    def test_paid_invoice_updates_projection_and_notifies(self):
        document = {
            "name": "ACC-PINV-0001",
            "modified": "2026-09-02 12:00:00",
            "posting_date": "2026-09-02",
            "docstatus": 1,
            "status": "Paid",
            "grand_total": 1000,
            "outstanding_amount": 0,
            "items": [{"purchase_order": "PUR-ORD-0001", "base_net_amount": 1000}],
        }
        event = {"event_id": "event-1"}
        case = {"case_id": "case-1", "assigned_user_id": "buyer"}
        with (
            patch.object(receipt_service.event_repository, "begin_event", return_value=(event, True)),
            patch.object(receipt_service.event_repository, "complete_event") as complete,
            patch.object(receipt_service.case_repository, "get_case_by_po", return_value=case),
            patch.object(
                receipt_service.delivery_repository,
                "get_delivery_by_case",
                return_value={"payment_status": "UNPAID"},
            ),
            patch.object(receipt_service.delivery_repository, "record_purchase_invoice") as record,
            patch.object(
                receipt_service.delivery_repository,
                "refresh_financial_progress",
                return_value={"payment_status": "PAID", "paid_amount": Decimal("1000")},
            ),
            patch.object(receipt_service.notification_repository, "create_notification") as notify,
        ):
            projections, created = receipt_service.register_purchase_invoice_event(document)

        self.assertTrue(created)
        self.assertTrue(projections[0]["matched"])
        record.assert_called_once()
        notify.assert_called_once()
        complete.assert_called_once_with("event-1")

    def test_failed_integration_event_can_be_claimed_again(self):
        connection = MagicMock()
        inserted = MagicMock()
        inserted.fetchone.return_value = None
        retried = MagicMock()
        retried.fetchone.return_value = {"event_id": "event-1", "status": "RECEIVED"}
        connection.execute.side_effect = [inserted, retried]

        with patch.object(events, "get_connection", return_value=nullcontext(connection)):
            row, claimed = events.begin_event(
                source="ERPNEXT",
                event_type="PURCHASE_RECEIPT_CHANGED",
                dedupe_key="receipt-1",
                external_id="MAT-PRE-0001",
                payload={"name": "MAT-PRE-0001"},
            )

        self.assertTrue(claimed)
        self.assertEqual(row["event_id"], "event-1")
        self.assertIn("STATUS = 'FAILED'", connection.execute.call_args_list[1].args[0].upper())


if __name__ == "__main__":
    unittest.main()
