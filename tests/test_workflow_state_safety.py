import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from backend_logic2.nodes.supplier.tools import case_logging
from backend_logic2.services import receipt_service, workflow_service
from backend_logic2.workflow import process_commands


class WorkflowStateSafetyTests(unittest.TestCase):
    @patch.object(case_logging, "_get_conn_or_none")
    def test_graph_status_logger_never_updates_case_read_model(self, get_connection_factory):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            "to_status": "checking_bidding"
        }
        get_connection_factory.return_value = lambda **_kwargs: nullcontext(connection)

        case_logging.log_status_change("case-1", "resolving_suppliers")

        statements = [str(call.args[0]).upper() for call in connection.execute.call_args_list]
        self.assertFalse(any("UPDATE PROCUREMENT.PROCUREMENT_CASE" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO PROCUREMENT.CASE_STATUS_HISTORY" in sql for sql in statements))

    @patch.object(workflow_service.task_repository, "cancel_pending_tasks")
    @patch.object(workflow_service.case_repository, "get_case")
    def test_terminal_case_is_not_resurrected_from_checkpoint(self, get_case, cancel):
        terminal = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "status": "CANCELLED",
            "stage": "CANCELLED",
        }
        get_case.return_value = terminal

        result = workflow_service.project_case_from_checkpoint("case-1")

        self.assertEqual(result, terminal)
        cancel.assert_called_once()

    @patch(
        "backend_logic2.nodes.supplier.register_candidate_suppliers.register_candidate_suppliers"
    )
    @patch.object(process_commands, "interrupt")
    def test_zero_search_results_accept_a_manually_entered_supplier(
        self, interrupt, register_suppliers
    ):
        interrupt.return_value = {
            "suppliers": ["직접입력상사"],
            "supplier_updates": [
                {"name": "직접입력상사", "email": "sales@example.com"}
            ],
            "quotation_deadline": "2026-09-05T18:00:00+09:00",
        }
        register_suppliers.return_value = [
            {"name": "직접입력상사", "status": "created"}
        ]

        command = process_commands.select_rfq_targets_command(
            {
                "case_id": "case-1",
                "mr_name": "MAT-MR-0001",
                "supplier_candidates": [],
                "existing_supplier_candidates": [],
            }
        )

        self.assertEqual(command.goto, "create_rfq")
        self.assertEqual(command.update["selected_suppliers"], ["직접입력상사"])
        registered = register_suppliers.call_args.args[0][0]
        self.assertEqual(registered["email"], "sales@example.com")
        self.assertEqual(registered["source"], "manual")

    def test_receipt_reversal_returns_completed_case_to_delivery(self):
        case = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "status": "COMPLETED",
            "stage": "COMPLETED",
            "assigned_user_id": "buyer",
        }
        event = {"event_id": "event-1"}
        payload = {
            "name": "MAT-PRE-0001",
            "modified": "2026-09-02 12:00:00",
            "posting_date": "2026-09-02",
            "docstatus": 2,
            "items": [{"purchase_order": "PUR-ORD-0001", "qty": 5}],
        }
        with (
            patch.object(receipt_service.event_repository, "begin_event", return_value=(event, True)),
            patch.object(receipt_service.event_repository, "complete_event") as complete_event,
            patch.object(receipt_service.delivery_repository, "record_purchase_receipt"),
            patch.object(receipt_service.case_repository, "get_case_by_po", return_value=case),
            patch.object(
                receipt_service.delivery_repository,
                "get_delivery_by_case",
                return_value={"delivery_status": "FULL", "scorecard_status": "COMPLETED"},
            ),
            patch.object(
                receipt_service,
                "ensure_delivery_for_po",
                return_value={
                    "delivery_status": "NOT_RECEIVED",
                    "scorecard_status": "LOCKED",
                    "full_receipt_date": None,
                },
            ),
            patch.object(receipt_service.task_repository, "cancel_pending_tasks_by_type") as cancel_scorecard,
            patch.object(receipt_service.case_repository, "transition_case") as transition,
            patch.object(receipt_service.notification_repository, "create_notification") as notify,
        ):
            receipt_service.register_purchase_receipt_event(payload)

        cancel_scorecard.assert_called_once()
        self.assertEqual(transition.call_args.kwargs["status"], "RUNNING")
        self.assertEqual(transition.call_args.kwargs["stage"], "DELIVERY")
        notify.assert_called_once()
        complete_event.assert_called_once_with("event-1")

    def test_purchase_order_cancellation_closes_case_and_tasks(self):
        case = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "assigned_user_id": "buyer",
        }
        event = {"event_id": "event-1"}
        payload = {"name": "PUR-ORD-0001", "modified": "now", "docstatus": 2}
        with (
            patch.object(receipt_service.event_repository, "begin_event", return_value=(event, True)),
            patch.object(receipt_service.event_repository, "complete_event") as complete_event,
            patch.object(receipt_service.case_repository, "get_case_by_po", return_value=case),
            patch.object(receipt_service.task_repository, "cancel_pending_tasks") as cancel,
            patch.object(receipt_service.case_repository, "transition_case", return_value={"status": "CANCELLED"}) as transition,
            patch.object(receipt_service.notification_repository, "create_notification") as notify,
        ):
            result, created = receipt_service.register_purchase_order_event(payload)

        self.assertTrue(created)
        self.assertTrue(result["matched"])
        cancel.assert_called_once()
        self.assertEqual(transition.call_args.kwargs["status"], "CANCELLED")
        notify.assert_called_once()
        complete_event.assert_called_once_with("event-1")


if __name__ == "__main__":
    unittest.main()
