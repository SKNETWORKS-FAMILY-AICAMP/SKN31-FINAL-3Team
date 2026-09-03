import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend_logic2.services import workflow_service


class WorkflowRetryTests(unittest.TestCase):
    @patch.object(workflow_service, "_delete_case_notifications_safely")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service, "_validate_erp_mr")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    def test_failed_submitted_case_queues_checkpoint_retry(
        self, get_case, get_app, validate, transition, delete_notifications
    ):
        get_case.return_value = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": "FAILED",
            "stage": "SUPPLIER_RECOMMENDATION",
            "version": 7,
            "workflow_snapshot": {"values": {"status": "resolving_supplier_pool"}},
        }
        get_app.return_value.get_state.return_value = SimpleNamespace(
            values={"status": "resolving_supplier_pool"}, next=("search_new_suppliers",)
        )
        transition.return_value = {"status": "QUEUED"}

        workflow_service.queue_case_start("case-1", triggered_by="buyer")

        validate.assert_called_once_with("MAT-MR-0001", allow_submitted=True)
        self.assertTrue(
            transition.call_args.kwargs["workflow_snapshot"]["retry_from_checkpoint"]
        )
        self.assertEqual(
            transition.call_args.kwargs["stage"], "SUPPLIER_RECOMMENDATION"
        )
        delete_notifications.assert_called_once_with("case-1")

    @patch.object(workflow_service, "project_case_from_checkpoint")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service, "_validate_erp_mr")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    def test_queued_retry_continues_from_next_checkpoint_node(
        self, get_case, get_app, validate, upsert, transition, project
    ):
        get_case.return_value = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": "QUEUED",
            "stage": "SUPPLIER_RECOMMENDATION",
            "version": 8,
            "workflow_snapshot": {"retry_from_checkpoint": True},
            "assigned_user_id": None,
        }
        validate.return_value = {
            "name": "MAT-MR-0001",
            "docstatus": 1,
            "status": "Pending",
        }
        app = MagicMock()
        app.get_state.return_value = SimpleNamespace(
            values={"status": "resolving_supplier_pool"}, next=("search_new_suppliers",)
        )
        get_app.return_value = app
        upsert.return_value = get_case.return_value

        workflow_service.run_queued_case("case-1", triggered_by="buyer")

        validate.assert_called_once_with("MAT-MR-0001", allow_submitted=True)
        app.invoke.assert_called_once_with(
            None,
            config={"configurable": {"thread_id": "MAT-MR-0001"}},
        )
        project.assert_called_once_with("case-1")

    @patch.object(workflow_service, "_delete_case_notifications_safely")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service, "_validate_erp_mr")
    @patch.object(workflow_service.case_repository, "transition_case")
    def test_catalog_terminal_checkpoint_queues_bidding_recheck(
        self, transition, validate, get_case, get_app, delete_notifications
    ):
        get_case.return_value = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": "FAILED",
            "stage": "HUMAN_REVIEW",
            "version": 3,
            "workflow_snapshot": {"values": {"status": "catalog_purchase_required"}},
        }
        get_app.return_value.get_state.return_value = SimpleNamespace(
            values={"status": "catalog_purchase_required"}, next=()
        )

        transition.return_value = {"status": "QUEUED"}

        workflow_service.queue_case_start("case-1", triggered_by="buyer")

        validate.assert_called_once_with("MAT-MR-0001", allow_submitted=True)
        self.assertTrue(
            transition.call_args.kwargs["workflow_snapshot"]["restart_from_bidding"]
        )
        self.assertEqual(transition.call_args.kwargs["stage"], "BIDDING_DECISION")
        delete_notifications.assert_called_once_with("case-1")

    @patch.object(workflow_service, "project_case_from_checkpoint")
    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service, "_validate_erp_mr")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    def test_queued_catalog_case_restarts_at_bidding_decision(
        self, get_case, get_app, validate, upsert, transition, project
    ):
        case = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": "QUEUED",
            "stage": "BIDDING_DECISION",
            "version": 8,
            "workflow_snapshot": {"restart_from_bidding": True},
            "assigned_user_id": None,
        }
        get_case.return_value = case
        validate.return_value = {
            "name": "MAT-MR-0001",
            "docstatus": 1,
            "status": "Pending",
        }
        upsert.return_value = case
        app = MagicMock()
        get_app.return_value = app

        workflow_service.run_queued_case("case-1", triggered_by="buyer")

        validate.assert_called_once_with("MAT-MR-0001", allow_submitted=True)
        app.invoke.assert_called_once_with(
            {
                "entrypoint": "bidding_recheck",
                "mr_name": "MAT-MR-0001",
                "case_id": "case-1",
                "status": "checking_bidding",
                "direct_purchase": False,
                "direct_purchase_items": {},
                "error": "",
            },
            config={"configurable": {"thread_id": "MAT-MR-0001"}},
        )
        project.assert_called_once_with("case-1")

    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    def test_unrecoverable_terminal_checkpoint_cannot_be_queued(self, get_case, get_app):
        get_case.return_value = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": "FAILED",
            "stage": "HUMAN_REVIEW",
            "version": 3,
            "workflow_snapshot": {"values": {"status": "human_review"}},
        }
        get_app.return_value.get_state.return_value = SimpleNamespace(
            values={"status": "human_review"}, next=()
        )

        with self.assertRaisesRegex(ValueError, "종료된 체크포인트"):
            workflow_service.queue_case_start("case-1", triggered_by="buyer")


if __name__ == "__main__":
    unittest.main()
