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

    @patch.object(workflow_service, "_delete_case_notifications_safely")
    @patch.object(workflow_service, "project_case_from_checkpoint")
    @patch.object(workflow_service.task_repository, "complete_claimed_task")
    @patch.object(workflow_service.task_repository, "claim_task")
    @patch.object(workflow_service, "_interrupt_payloads")
    @patch.object(workflow_service, "get_process_app")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_success_completes_claim_before_returning_projection(
        self, get_task, get_case, get_app, interrupt_payloads,
        claim, complete, project, delete_notifications
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
        delete_notifications.assert_called_once_with("case-1")
        project.assert_called_once_with("case-1")
        self.assertEqual(result["case_id"], "case-1")


if __name__ == "__main__":
    unittest.main()


class QueuedTaskAnswerTests(unittest.TestCase):
    """사람이 답한 뒤의 그래프 실행은 HTTP 요청을 붙잡고 있으면 안 된다.

    ⚠️ 왜 중요한가: 그래프가 이어 돌면서 ERPNext 쓰기·메일 발송·공급사 검색을
    하는데, 그걸 요청 안에서 기다리면 nginx /api/ 기본 제한(60초)을 넘겨
    504가 나고 화면에는 "구매 작업 API 요청에 실패했습니다"만 남는다. 정작
    그래프는 계속 돌기 때문에 케이스는 실패도 아닌 "...중"에 갇힌다.
    """

    def _task(self, task_type="select_rfq_targets"):
        return {
            "task_id": "task-1",
            "case_id": "case-1",
            "task_type": task_type,
            "status": "PENDING",
            "version": 3,
        }

    def _case(self, status="WAITING_INPUT", stage="RFQ_TARGET_SELECTION"):
        return {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "thread_id": "MAT-MR-0001",
            "status": status,
            "stage": stage,
        }

    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_the_graph_runs_after_the_request_returns(
        self, get_task, get_case, transition
    ):
        get_task.return_value = self._task()
        get_case.return_value = self._case()
        background = MagicMock()

        result = workflow_service.queue_task_answer(
            "task-1",
            answer={"suppliers": ["동관컴퍼니"]},
            answered_by="buyer",
            expected_version=3,
            background_tasks=background,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "RUNNING")
        # 그래프는 예약만 되고, 이 호출 안에서는 돌지 않는다.
        background.add_task.assert_called_once()
        queued = background.add_task.call_args
        self.assertIs(queued[0][0], workflow_service._run_queued_task_answer)
        self.assertEqual(queued[1]["expected_version"], 3)
        transition.assert_called_once()
        self.assertEqual(transition.call_args[1]["status"], "RUNNING")

    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_a_double_submit_is_refused(self, get_task, get_case):
        get_task.return_value = self._task()
        get_case.return_value = self._case(status="RUNNING")

        with self.assertRaisesRegex(ValueError, "이미 처리가 진행 중"):
            workflow_service.queue_task_answer(
                "task-1",
                answer={"suppliers": ["동관컴퍼니"]},
                answered_by="buyer",
                expected_version=3,
                background_tasks=MagicMock(),
            )

    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_a_stage_mismatch_is_rejected_synchronously(self, get_task, get_case):
        """버전 충돌·단계 불일치는 사용자가 즉시 알아야 한다."""
        get_task.return_value = self._task("po_approval")
        get_case.return_value = self._case(stage="RFQ_TARGET_SELECTION")
        background = MagicMock()

        with self.assertRaisesRegex(ValueError, "현재 단계"):
            workflow_service.queue_task_answer(
                "task-1",
                answer={"decision": "approve"},
                answered_by="buyer",
                expected_version=3,
                background_tasks=background,
            )
        background.add_task.assert_not_called()

    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service, "resume_task")
    def test_a_background_failure_becomes_visible_instead_of_staying_running(
        self, resume, get_case, transition
    ):
        resume.side_effect = RuntimeError("ERPNext 응답이 없습니다")
        get_case.return_value = self._case(status="RUNNING")

        workflow_service._run_queued_task_answer(
            "task-1",
            answer={"suppliers": ["동관컴퍼니"]},
            answered_by="buyer",
            expected_version=3,
            case_id="case-1",
            stage="RFQ_TARGET_SELECTION",
        )

        transition.assert_called_once()
        recorded = transition.call_args[1]
        self.assertEqual(recorded["status"], "WAITING_INPUT")
        self.assertIn("ERPNext", recorded["last_error"])
