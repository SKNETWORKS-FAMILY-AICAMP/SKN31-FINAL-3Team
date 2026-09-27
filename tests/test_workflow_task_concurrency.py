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
        # 전용 그래프 스레드로 넘긴다 - 요청 스레드풀을 붙잡으면 안 된다.
        self.assertIs(queued[0][0], workflow_service.submit_graph_work)
        self.assertIs(queued[0][1], workflow_service._run_queued_task_answer)
        self.assertEqual(queued[1]["expected_version"], 3)
        transition.assert_called_once()
        self.assertEqual(transition.call_args[1]["status"], "RUNNING")

    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_a_double_submit_is_refused_while_it_really_runs(self, get_task, get_case):
        get_task.return_value = self._task()
        get_case.return_value = self._case(status="RUNNING")

        with workflow_service.graph_case_in_flight("case-1"):
            with self.assertRaisesRegex(ValueError, "이미 처리가 진행 중"):
                workflow_service.queue_task_answer(
                    "task-1",
                    answer={"suppliers": ["동관컴퍼니"]},
                    answered_by="buyer",
                    expected_version=3,
                    background_tasks=MagicMock(),
                )

    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service, "_settle_case_after_failure")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_a_leftover_running_mark_is_cleaned_up_instead_of_blocking(
        self, get_task, get_case, settle, transition
    ):
        """⚠️ RUNNING 표시는 DB에, 실제 작업은 메모리에 있다.

        재시작이나 예외로 작업이 사라지면 표시만 남는데, 그걸 '처리 중'으로
        믿고 막으면 사용자는 아무것도 못 하게 된다(실제로 그렇게 막혔다).
        진짜 도는 일이 없으면 정리하고 진행시킨다.
        """
        get_task.return_value = self._task()
        get_case.side_effect = [
            self._case(status="RUNNING"),   # 남겨진 표시
            self._case(status="WAITING_INPUT"),  # 정리된 뒤
        ]

        result = workflow_service.queue_task_answer(
            "task-1",
            answer={"suppliers": ["동관컴퍼니"]},
            answered_by="buyer",
            expected_version=3,
            background_tasks=MagicMock(),
        )

        settle.assert_called_once()
        self.assertTrue(result["accepted"])

    @patch.object(workflow_service.case_repository, "transition_case")
    @patch.object(workflow_service, "_settle_case_after_failure")
    @patch.object(workflow_service.case_repository, "get_case")
    @patch.object(workflow_service.task_repository, "get_task")
    def test_a_run_that_stopped_mid_node_asks_for_a_retry(
        self, get_task, get_case, settle, transition
    ):
        get_task.return_value = self._task()
        get_case.side_effect = [
            self._case(status="RUNNING"),
            self._case(status="FAILED", stage="HUMAN_REVIEW"),
        ]

        with self.assertRaisesRegex(ValueError, "다시 시도"):
            workflow_service.queue_task_answer(
                "task-1",
                answer={"suppliers": ["동관컴퍼니"]},
                answered_by="buyer",
                expected_version=3,
                background_tasks=MagicMock(),
            )

    def test_work_is_only_in_flight_while_it_runs(self):
        self.assertFalse(workflow_service.is_graph_case_in_flight("case-1"))
        with workflow_service.graph_case_in_flight("case-1"):
            self.assertTrue(workflow_service.is_graph_case_in_flight("case-1"))
        self.assertFalse(workflow_service.is_graph_case_in_flight("case-1"))

    def test_a_crash_still_clears_the_in_flight_mark(self):
        with self.assertRaises(RuntimeError):
            with workflow_service.graph_case_in_flight("case-1"):
                raise RuntimeError("boom")
        self.assertFalse(workflow_service.is_graph_case_in_flight("case-1"))

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

    def _failure_env(self, *, task_status, waiting_on_person=True):
        return (
            patch.object(workflow_service, "resume_task",
                         side_effect=RuntimeError("ERPNext 응답이 없습니다")),
            patch.object(workflow_service.task_repository, "get_task",
                         return_value={**self._task(), "status": task_status}),
            patch.object(workflow_service, "project_case_from_checkpoint",
                         return_value={"status": "WAITING_INPUT", "stage": "RFQ_TARGET_SELECTION"}),
            patch.object(workflow_service.case_repository, "get_case",
                         return_value=self._case(status="RUNNING")),
            patch.object(workflow_service, "get_process_app",
                         return_value=SimpleNamespace(
                             get_state=lambda config: SimpleNamespace(next=("x",), values={}))),
            patch.object(workflow_service, "_interrupt_payloads",
                         return_value=[{"type": "select_rfq_targets"}] if waiting_on_person else []),
            patch.object(workflow_service.case_repository, "transition_case"),
        )

    def _run_failure(self, **kwargs):
        patches = self._failure_env(**kwargs)
        mocks = [p.start() for p in patches]
        try:
            workflow_service._run_queued_task_answer(
                "task-1",
                answer={"suppliers": ["동관컴퍼니"]},
                answered_by="buyer",
                expected_version=3,
                case_id="case-1",
                stage="RFQ_TARGET_SELECTION",
            )
        finally:
            for p in patches:
                p.stop()
        return mocks

    def test_a_background_failure_becomes_visible_instead_of_staying_running(self):
        *_, project, _get_case, _app, _interrupts, transition = self._run_failure(
            task_status="PENDING"
        )

        transition.assert_called_once()
        recorded = transition.call_args[1]
        # 체크포인트가 가리키는 실제 위치(사람 답 대기)에 이유를 붙인다.
        self.assertEqual(recorded["status"], "WAITING_INPUT")
        self.assertIn("ERPNext", recorded["last_error"])

    def test_losing_a_race_to_auto_progress_does_not_rewind_the_case(self):
        """⚠️ 자동 진행이 같은 작업을 먼저 처리했으면 오류가 아니다.

        예전엔 진 쪽이 케이스를 '원래 단계의 대기'로 되돌려서, 이미 발주
        단계로 넘어간 건을 견적 단계로 되감았다.
        """
        *_, project, _get_case, _app, _interrupts, transition = self._run_failure(
            task_status="COMPLETED"
        )

        project.assert_called_once_with("case-1")
        transition.assert_not_called()

    def test_a_failure_mid_node_becomes_retryable(self):
        *_, transition = self._run_failure(task_status="PENDING", waiting_on_person=False)

        recorded = transition.call_args[1]
        self.assertEqual(recorded["status"], "FAILED")
        self.assertEqual(recorded["stage"], "HUMAN_REVIEW")


class GraphWorkerIsolationTests(unittest.TestCase):
    """그래프 작업이 요청 스레드풀을 굶기면 로그인까지 막힌다.

    Starlette의 BackgroundTasks와 동기 엔드포인트는 같은 anyio 스레드풀(기본
    40개)을 쓴다. 그래프 한 번이 몇 분씩 걸리는데 그걸 배경 작업으로 돌리면
    워커 스레드를 그 시간 내내 붙잡고, _GRAPH_LOCK 때문에 뒤따르는 건들도
    스레드를 쥔 채 락을 기다린다. 40개가 차면 로그인이 무한 로딩에 걸린다.
    """

    def test_graph_work_runs_on_a_single_dedicated_thread(self):
        seen: list[str] = []

        def slow_graph_work(label):
            import threading
            seen.append(threading.current_thread().name)

        for label in ("a", "b", "c"):
            workflow_service.submit_graph_work(slow_graph_work, label)
        workflow_service._GRAPH_EXECUTOR.submit(lambda: None).result(timeout=10)

        self.assertEqual(len(seen), 3)
        # 전부 같은 전용 스레드에서, 요청 스레드풀 밖에서 돌았다.
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(seen[0].startswith("biddingflow-graph"))

    def test_submitting_returns_immediately_without_running_the_work(self):
        import threading

        started = threading.Event()
        release = threading.Event()

        def blocking_work():
            started.set()
            release.wait(timeout=10)

        workflow_service.submit_graph_work(blocking_work)
        started.wait(timeout=10)
        # 제출한 쪽은 이미 돌아와 있다. 작업이 끝나길 기다리지 않는다.
        self.assertTrue(started.is_set())
        release.set()
        workflow_service._GRAPH_EXECUTOR.submit(lambda: None).result(timeout=10)

    def test_a_failing_job_does_not_kill_the_worker(self):
        """한 건이 터져도 다음 건은 계속 돌아야 한다."""
        done = []

        workflow_service.submit_graph_work(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        workflow_service.submit_graph_work(lambda: done.append(True))
        workflow_service._GRAPH_EXECUTOR.submit(lambda: None).result(timeout=10)

        self.assertEqual(done, [True])
