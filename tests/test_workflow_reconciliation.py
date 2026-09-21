import unittest
from unittest.mock import patch

from backend_logic2.integrations.erp_client import ERPNextAPIError
from backend_logic2.services import workflow_service


class WorkflowReconciliationTests(unittest.TestCase):
    @patch.object(workflow_service.case_repository, "list_open_case_references")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_lightweight_poll_does_not_reconcile_existing_cases(
        self, pending, open_cases
    ):
        pending.return_value = []

        result = workflow_service.sync_draft_material_requests(
            reconcile_existing=False
        )

        self.assertEqual(result, [])
        open_cases.assert_not_called()

    @patch.object(workflow_service.task_repository, "cancel_pending_tasks")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "list_open_case_references")
    @patch.object(workflow_service, "get_material_request_detail")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_deleted_erp_mr_closes_cached_case(
        self, pending, get_one, open_cases, transition, cancel_tasks
    ):
        pending.return_value = []
        open_cases.return_value = [
            {
                "case_id": "case-1",
                "mr_name": "MAT-MR-DELETED",
                "status": "AWAITING_MR_REVIEW",
                "stage": "MR_REVIEW",
            }
        ]
        get_one.side_effect = ERPNextAPIError(
            "GET Material Request/MAT-MR-DELETED: 404 - Not Found"
        )
        transition.return_value = {
            "case_id": "case-1",
            "status": "CANCELLED",
            "stage": "CANCELLED",
        }

        workflow_service.sync_draft_material_requests()

        transition.assert_called_once_with(
            "case-1",
            status="CANCELLED",
            stage="CANCELLED",
            reason="ERPNext에서 Material Request가 삭제되어 대사 과정에서 종료했습니다.",
            triggered_by="reconciliation",
        )
        cancel_tasks.assert_called_once_with(
            "case-1",
            reason="ERPNext에서 Material Request가 삭제되어 대사 과정에서 종료했습니다.",
        )

    @patch.object(workflow_service.task_repository, "cancel_pending_tasks")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "list_open_case_references")
    @patch.object(workflow_service, "get_material_request_detail")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_temporary_erp_failure_does_not_close_case(
        self, pending, get_one, open_cases, transition, cancel_tasks
    ):
        pending.return_value = []
        open_cases.return_value = [
            {
                "case_id": "case-1",
                "mr_name": "MAT-MR-RETRY",
                "status": "RUNNING",
                "stage": "ITEM_CHECK",
            }
        ]
        get_one.side_effect = ERPNextAPIError(
            "GET Material Request/MAT-MR-RETRY: 503 - temporarily unavailable"
        )

        workflow_service.sync_draft_material_requests()

        transition.assert_not_called()
        cancel_tasks.assert_not_called()

    @patch.object(workflow_service, "_delete_thread_checkpoint_safely")
    @patch.object(workflow_service, "_delete_case_notifications_safely")
    @patch.object(workflow_service.task_repository, "cancel_pending_tasks")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service.case_repository, "list_open_case_references")
    @patch.object(workflow_service, "get_material_request_detail")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_erp_submitted_unstarted_mr_is_removed_from_inbox(
        self,
        pending,
        get_one,
        open_cases,
        upsert,
        transition,
        cancel_tasks,
        delete_notifications,
        delete_checkpoint,
    ):
        pending.return_value = []
        cached = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-EXTERNAL",
            "thread_id": "MAT-MR-EXTERNAL",
            "status": "AWAITING_MR_REVIEW",
            "stage": "MR_REVIEW",
        }
        open_cases.return_value = [cached]
        get_one.return_value = {
            "name": "MAT-MR-EXTERNAL",
            "status": "Pending",
            "docstatus": 1,
            "items": [{"item_code": "ITEM-001"}],
        }
        transition.return_value = {
            **cached,
            "status": "CANCELLED",
            "stage": "CANCELLED",
        }

        workflow_service.sync_draft_material_requests()

        reason = (
            "ERPNext에서 Material Request가 외부 제출되어 "
            "Bidding Flow 대기 목록에서 종료했습니다."
        )
        transition.assert_called_once_with(
            "case-1",
            status="CANCELLED",
            stage="CANCELLED",
            reason=reason,
            triggered_by="reconciliation",
        )
        cancel_tasks.assert_called_once_with("case-1", reason=reason)
        delete_notifications.assert_called_once_with("case-1")
        delete_checkpoint.assert_called_once_with(cached)
        upsert.assert_not_called()

    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service.case_repository, "list_open_case_references")
    @patch.object(workflow_service, "get_material_request_detail")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_erp_submitted_running_mr_remains_active(
        self, pending, get_one, open_cases, upsert, transition
    ):
        pending.return_value = []
        cached = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-BIDDINGFLOW",
            "status": "RUNNING",
            "stage": "SUPPLIER_RECOMMENDATION",
        }
        document = {
            "name": "MAT-MR-BIDDINGFLOW",
            "status": "Pending",
            "docstatus": 1,
            "items": [{"item_code": "ITEM-001"}],
        }
        refreshed = {**cached, "summary": {"docstatus": 1}}
        open_cases.return_value = [cached]
        get_one.return_value = document
        upsert.return_value = refreshed

        result = workflow_service.sync_draft_material_requests()

        self.assertEqual(result, [refreshed])
        transition.assert_not_called()
        upsert.assert_called_once_with(document)

    @patch.object(workflow_service, "_delete_thread_checkpoint_safely")
    @patch.object(workflow_service, "_delete_case_notifications_safely")
    @patch.object(workflow_service.task_repository, "cancel_pending_tasks")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "get_case_by_mr")
    @patch.object(workflow_service, "get_material_request_detail")
    @patch.object(workflow_service.event_repository, "complete_event")
    @patch.object(workflow_service.event_repository, "begin_event")
    def test_submit_webhook_removes_unstarted_mr_from_inbox(
        self,
        begin_event,
        complete_event,
        get_one,
        get_case,
        transition,
        cancel_tasks,
        delete_notifications,
        delete_checkpoint,
    ):
        cached = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-EXTERNAL",
            "thread_id": "MAT-MR-EXTERNAL",
            "status": "AWAITING_MR_REVIEW",
            "stage": "MR_REVIEW",
        }
        closed = {**cached, "status": "CANCELLED", "stage": "CANCELLED"}
        begin_event.return_value = ({"event_id": "event-1"}, True)
        get_case.return_value = cached
        get_one.return_value = {
            "name": "MAT-MR-EXTERNAL",
            "status": "Pending",
            "docstatus": 1,
            "items": [{"item_code": "ITEM-001"}],
        }
        transition.return_value = closed

        result, created = workflow_service.register_material_request_event(
            {"doc": {"name": "MAT-MR-EXTERNAL", "modified": "2026-09-21 12:00:00"}}
        )

        self.assertTrue(created)
        self.assertEqual(result, closed)
        reason = (
            "ERPNext에서 Material Request가 외부 제출되어 "
            "Bidding Flow 대기 목록에서 종료했습니다."
        )
        transition.assert_called_once_with(
            "case-1",
            status="CANCELLED",
            stage="CANCELLED",
            reason=reason,
            triggered_by="erpnext_webhook",
        )
        cancel_tasks.assert_called_once_with("case-1", reason=reason)
        delete_notifications.assert_called_once_with("case-1")
        delete_checkpoint.assert_called_once_with(cached)
        complete_event.assert_called_once_with("event-1")


if __name__ == "__main__":
    unittest.main()
