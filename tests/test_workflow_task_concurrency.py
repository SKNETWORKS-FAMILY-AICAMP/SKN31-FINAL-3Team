import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend_logic2.services import workflow_service


class WorkflowTaskConcurrencyTests(unittest.TestCase):
    def _task(self, task_type="select_rfq_targets"):
        return {
            "task_id": "task-1",
            "case_id": "case-1",
            "task_type": task_type,
            "status": "PENDING",
            "version": 1,
        }

    def _case(self, stage="RFQ_TARGET_SELECTION"):
        return {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": "WAITING_INPUT",
            "stage": stage,
        }

    def test_answer_requires_the_rendered_task_version(self):
        with self.assertRaisesRegex(ValueError, "작업 버전"):
            workflow_service.resume_task(
                "task-1", answer={"suppliers": ["A"]}, answered_by="buyer"
            )

    @patch.object(workflow_service.task_repository, "claim_task")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_stage_mismatch_is_rejected_before_claim(self, get_task, get_case, claim):
        get_task.return_value = self._task("po_approval")
        get_case.return_value = self._case("RFQ_TARGET_SELECTION")

        with self.assertRaisesRegex(ValueError, "현재 단계"):
            workflow_service.resume_task(
                "task-1",
                answer={"decision": "approve"},
                answered_by="buyer",
                expected_version=1,
            )
        claim.assert_not_called()

    @patch.object(workflow_service.task_repository, "complete_claimed_task")
    @patch.object(workflow_service.task_repository, "release_claimed_task")
    @patch.object(workflow_service.task_repository, "claim_task")
    @patch.object(workflow_service, "_interrupt_payloads")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_graph_failure_releases_the_exact_claim(
        self, get_task, get_case, get_app, interrupt_payloads,
        claim, release, complete
    ):
        get_task.return_value = self._task()
        get_case.return_value = self._case()
        interrupt_payloads.return_value = [{"type": "select_rfq_targets"}]
        app = MagicMock()
        app.get_state.return_value = SimpleNamespace(tasks=())
        app.invoke.side_effect = RuntimeError("rfq failed")
        get_app.return_value = app
        claim.return_value = {**self._task(), "status": "PROCESSING", "version": 2}

        with self.assertRaisesRegex(RuntimeError, "rfq failed"):
            workflow_service.resume_task(
                "task-1",
                answer={"suppliers": ["A"]},
                answered_by="buyer",
                expected_version=1,
            )

        release.assert_called_once_with("task-1", claimed_version=2)
        complete.assert_not_called()

    @patch.object(workflow_service, "project_case_from_checkpoint")
    @patch.object(workflow_service.task_repository, "complete_claimed_task")
    @patch.object(workflow_service.task_repository, "claim_task")
    @patch.object(workflow_service, "_interrupt_payloads")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_success_completes_claim_before_returning_projection(
        self, get_task, get_case, get_app, interrupt_payloads,
        claim, complete, project
    ):
        get_task.return_value = self._task("check_quotations")
        get_case.return_value = self._case("QUOTATION_COLLECTION")
        interrupt_payloads.return_value = [{"type": "check_quotations"}]
        app = MagicMock()
        app.get_state.return_value = SimpleNamespace(tasks=())
        get_app.return_value = app
        claim.return_value = {
            **self._task("check_quotations"),
            "status": "PROCESSING",
            "version": 2,
        }
        project.return_value = {"case_id": "case-1", "stage": "QUOTATION_COLLECTION"}

        result = workflow_service.resume_task(
            "task-1",
            answer={"decision": "check"},
            answered_by="buyer",
            expected_version=1,
        )

        complete.assert_called_once_with("task-1", claimed_version=2)
        project.assert_called_once_with("case-1")
        self.assertEqual(result["case_id"], "case-1")


if __name__ == "__main__":
    unittest.main()
