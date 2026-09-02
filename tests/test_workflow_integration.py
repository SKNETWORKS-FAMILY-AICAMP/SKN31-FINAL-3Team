import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend_logic2.repositories.cases import (
    is_recreated_material_request,
    material_request_thread_id,
    material_request_summary,
)
from backend_logic2.services.workflow_projection import (
    project_graph_status,
    task_input_schema,
    task_presentation,
)
from backend_logic2.services.workflow_service import project_substitute_decision
from backend_logic2.workflow.process_commands import (
    _cancel_urgent_mr_without_supplier,
    _submit_mr_for_purchase,
    await_order_start_command,
    check_mr_item_command,
    final_selection_command,
    po_approval_command,
)


class WorkflowIntegrationTests(unittest.TestCase):
    def test_new_erp_document_with_reused_mr_name_is_detected(self):
        existing = {
            "status": "CANCELLED",
            "cancelled_at": datetime(2026, 9, 2, 6, 46, 33, tzinfo=timezone.utc),
        }
        material_request = {"creation": "2026-09-02 15:46:50"}

        self.assertTrue(is_recreated_material_request(existing, material_request))

    def test_rejected_original_draft_is_not_treated_as_recreated(self):
        existing = {
            "status": "REJECTED",
            "cancelled_at": datetime(2026, 9, 2, 6, 46, 33, tzinfo=timezone.utc),
        }
        original_material_request = {"creation": "2026-09-02 15:30:00"}

        self.assertFalse(
            is_recreated_material_request(existing, original_material_request)
        )

    def test_recreated_mr_uses_a_new_checkpoint_thread(self):
        thread_id = material_request_thread_id(
            "MAT-MR-0001",
            {"creation": "2026-09-02 15:46:50"},
            recreated=True,
        )

        self.assertNotEqual(thread_id, "MAT-MR-0001")
        self.assertTrue(thread_id.startswith("MAT-MR-0001:recreated:"))

    def test_material_request_summary_uses_the_single_item_projection(self):
        summary = material_request_summary(
            {
                "name": "MAT-MR-0001",
                "owner": "requester@example.com",
                "schedule_date": "2026-09-10",
                "items": [
                    {
                        "item_code": "ITEM-001",
                        "item_name": "볼 밸브",
                        "qty": 3,
                        "rate": 1200,
                        "uom": "EA",
                    }
                ],
            }
        )

        self.assertEqual(summary["mr_name"], "MAT-MR-0001")
        self.assertEqual(summary["item_code"], "ITEM-001")
        self.assertEqual(summary["amount"], "3600")
        self.assertEqual(summary["requester"], "requester@example.com")

    def test_graph_status_projection_distinguishes_selection_and_po_approval(self):
        self.assertEqual(project_graph_status("supplier_selected"), ("WAITING_INPUT", "ORDER_START"))
        self.assertEqual(project_graph_status("awaiting_po_approval"), ("WAITING_INPUT", "PRE_PO_APPROVAL"))
        self.assertEqual(project_graph_status("po_sent"), ("RUNNING", "DELIVERY"))
        self.assertEqual(
            project_graph_status("catalog_purchase_required"),
            ("FAILED", "HUMAN_REVIEW"),
        )
        self.assertEqual(
            project_graph_status("urgent_no_supplier_cancelled"),
            ("CANCELLED", "CANCELLED"),
        )

    @patch("backend_logic2.integrations.erp_client.erp_submit")
    @patch("backend_logic2.integrations.erp_client.erp_get_one")
    def test_purchase_path_submits_draft_mr_once(self, get_one, submit):
        get_one.return_value = {"name": "MAT-MR-0001", "docstatus": 0, "status": "Draft"}
        submit.return_value = {"name": "MAT-MR-0001", "docstatus": 1}

        result = _submit_mr_for_purchase("MAT-MR-0001")

        submit.assert_called_once_with("Material Request", "MAT-MR-0001")
        self.assertEqual(result["docstatus"], 1)

    @patch("backend_logic2.workflow.process_commands._submit_mr_for_purchase")
    @patch("backend_logic2.nodes.mr.find_substitute.notify_requester_of_substitutes")
    @patch("backend_logic2.nodes.mr.find_substitute.find_substitutes_for_mr")
    @patch("backend_logic2.integrations.erp_client.erp_get_one")
    def test_substitute_candidates_keep_mr_draft_until_requester_decides(
        self,
        get_one,
        find_substitutes,
        notify_requester,
        submit_mr,
    ):
        material_request = {
            "name": "MAT-MR-0001",
            "docstatus": 0,
            "status": "Draft",
            "items": [{"item_code": "ITEM-001", "qty": 1}],
        }
        substitutes = {
            "ITEM-001": {
                "qty_needed": 1,
                "substitutes": [{"item_code": "ITEM-SUB-001"}],
            }
        }
        get_one.return_value = material_request
        find_substitutes.return_value = substitutes

        command = check_mr_item_command({"mr_name": "MAT-MR-0001"})

        self.assertEqual(command.goto, "substitute_selection")
        self.assertEqual(command.update["status"], "awaiting_substitute_selection")
        notify_requester.assert_called_once_with(material_request, substitutes)
        submit_mr.assert_not_called()

    @patch("backend_logic2.services.workflow_service._create_notification_safely")
    @patch("backend_logic2.services.workflow_service.project_case_from_checkpoint")
    @patch("backend_logic2.services.workflow_service.case_repository.get_case_by_mr")
    def test_substitute_selection_projects_cancel_and_notifies_frontend(
        self,
        get_case,
        project_case,
        notify,
    ):
        get_case.return_value = {
            "case_id": "case-1",
            "assigned_user_id": "buyer@example.com",
        }
        project_case.return_value = {"stage": "SUBSTITUTE_SELECTED"}

        project_substitute_decision(
            "MAT-MR-0001",
            selected_item_code="ITEM-SUB-001",
        )

        project_case.assert_called_once_with("case-1")
        self.assertEqual(notify.call_args.kwargs["notification_type"], "SUBSTITUTE_SELECTED")
        self.assertEqual(notify.call_args.kwargs["payload"]["stage"], "SUBSTITUTE_SELECTED")

    @patch("backend_logic2.services.workflow_service._create_notification_safely")
    @patch("backend_logic2.services.workflow_service.project_case_from_checkpoint")
    @patch("backend_logic2.services.workflow_service.case_repository.get_case_by_mr")
    def test_new_purchase_projects_next_stage_and_notifies_frontend(
        self,
        get_case,
        project_case,
        notify,
    ):
        get_case.return_value = {
            "case_id": "case-1",
            "assigned_user_id": "buyer@example.com",
        }
        project_case.return_value = {"stage": "RFQ_TARGET_SELECTION"}

        project_substitute_decision("MAT-MR-0001", new_purchase=True)

        project_case.assert_called_once_with("case-1")
        self.assertEqual(
            notify.call_args.kwargs["notification_type"],
            "SUBSTITUTE_NEW_PURCHASE_REQUESTED",
        )
        self.assertEqual(notify.call_args.kwargs["payload"]["stage"], "RFQ_TARGET_SELECTION")

    @patch("backend_logic2.nodes.mr.reject_material_request.reject_material_request")
    @patch("backend_logic2.integrations.erp_client.erp_get_one")
    def test_urgent_mr_without_connected_supplier_is_cancelled(self, get_one, reject):
        get_one.return_value = {"item_code": "ITEM-001", "supplier_items": []}
        results = {
            "ITEM-001": {
                "needs_bidding": False,
                "reasons": ["긴급발주 (납기까지 2일, 긴급 기준 7일 이하)"],
            }
        }

        reason = _cancel_urgent_mr_without_supplier("MAT-MR-0001", results)

        self.assertIn("최근 거래한 협력사가 없어", reason)
        reject.assert_called_once()

    def test_order_start_interrupt_is_a_frontend_confirmation_form(self):
        payload = {"type": "order_start", "selected_supplier": "공급사 A"}
        presentation = task_presentation(payload)
        schema = task_input_schema(payload)

        self.assertEqual(presentation["audience"], "BUYER")
        self.assertEqual(presentation["channel"], "BIDDINGFLOW")
        self.assertEqual(schema["confirm_value"], "start_order")

    def test_final_selection_does_not_create_po_immediately(self):
        state = {
            "mr_name": "MAT-MR-0001",
            "rfq_name": "PUR-RFQ-0001",
            "quotation_ranking": [{"supplier": "공급사 A"}],
        }
        with patch(
            "backend_logic2.workflow.process_commands.interrupt",
            return_value={"supplier": "공급사 A"},
        ):
            command = final_selection_command(state)

        self.assertEqual(command.goto, "await_order_start")
        self.assertEqual(command.update["status"], "supplier_selected")

    def test_order_start_then_po_approval_are_separate_decisions(self):
        state = {
            "mr_name": "MAT-MR-0001",
            "rfq_name": "PUR-RFQ-0001",
            "selected_supplier": "공급사 A",
        }
        with patch(
            "backend_logic2.workflow.process_commands.interrupt",
            return_value={"decision": "start_order"},
        ):
            order_command = await_order_start_command(state)
        self.assertEqual(order_command.goto, "po_approval")
        self.assertEqual(order_command.update["status"], "awaiting_po_approval")

        with patch(
            "backend_logic2.workflow.process_commands.interrupt",
            return_value={"decision": "approve"},
        ):
            approval_command = po_approval_command(state)
        self.assertEqual(approval_command.goto, "create_po")
        self.assertEqual(approval_command.update["status"], "creating_po")


if __name__ == "__main__":
    unittest.main()
