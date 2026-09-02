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
    @patch.object(workflow_service, "erp_get_one")
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
    @patch.object(workflow_service, "erp_get_one")
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


if __name__ == "__main__":
    unittest.main()
