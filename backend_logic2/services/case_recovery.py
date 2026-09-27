"""케이스 상태를 그래프 체크포인트와 다시 맞춘다.

두 가지를 한다.

1. **재시작 복구** - 그래프 작업 큐는 메모리에만 있다. 자동 배포는 푸시마다
   서버를 재시작하므로, 진행 중이던 작업은 사라지는데 DB에는 RUNNING /
   PROCESSING 표시만 남는다. 그러면 "이미 처리가 진행 중입니다"에 막혀
   재시도조차 못 하고 영원히 멈춘다(v1에서 실제로 그렇게 막혔다).

2. **멈춰 있는 건의 단계 재투영** - 케이스의 stage는 **직전 노드가 남긴 값**
   이다. 사람을 기다리는 노드는 interrupt()로 멈추는데 LangGraph는 노드가
   Command를 반환할 때만 state를 갱신하므로, 그 노드는 자기 상태를 남길 수
   없다. 그래서 어느 경로로 왔느냐에 따라 stage가 어긋났다.

   투영(project_case_from_checkpoint)은 그 어긋남을 바로잡지만, **가만히
   기다리는 건은 투영될 일이 없어서** 옛 값이 그대로 남는다. 실제로 기존
   협력사 풀로 충분한 건이 'RFQ 대상 선택'에서 사람을 기다리는데도 화면은
   '협력사 탐색'으로 보고 버튼을 막았다. 여기서 주기적으로 맞춰준다.

⚠️ 데이터베이스는 다른 인스턴스(개발 PC)와 함께 본다. 그래프 체크포인트는
인스턴스마다 따로 있는 SQLite 파일이므로, **이 인스턴스가 진행 상황을 갖고
있는 건만** 건드린다. 체크포인트가 없는 쪽이 케이스를 건드리면 처음부터 다시
도는 사고가 난다.
"""

from __future__ import annotations

import logging
from typing import Any

from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import tasks as task_repository

from . import graph_worker

LOGGER = logging.getLogger(__name__)

RESTART_MESSAGE = (
    "서버가 재시작되면서 진행 중이던 처리가 중단되었습니다. 다시 시도해 주세요."
)
RECOVERY_ACTOR = "system:recovery"
_UNFINISHED_STATUSES = {"RUNNING", "QUEUED"}


def has_local_checkpoint(case: dict[str, Any]) -> bool:
    """이 인스턴스가 그 케이스의 워크플로 진행 상황을 갖고 있는가."""
    from backend_logic2.workflow.process_graph import get_process_app

    thread_id = str(case.get("thread_id") or case.get("mr_name") or "").strip()
    if not thread_id:
        return False
    try:
        snapshot = get_process_app().get_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - 확인이 안 되면 건드리지 않는다
        LOGGER.warning("체크포인트 확인 실패: thread_id=%s", thread_id, exc_info=True)
        return False
    return bool(snapshot and (snapshot.next or snapshot.values))


def _settle(case_id: str, *, error: str | None, actor: str, reason: str) -> str:
    """한 건을 체크포인트가 가리키는 실제 위치로 맞춘다. 무엇을 했는지 돌려준다.

    ⚠️ 실패했다고 무조건 '원래 단계의 대기'로 되돌리면 안 된다. 같은 건을
    다른 경로가 이미 진행시켰을 수 있고, 그러면 넘어간 건을 과거 단계로
    되감게 된다. 기준은 언제나 체크포인트다.
    """
    from .workflow_service import (
        _TERMINAL_CASE_STATUSES,
        _config,
        _interrupt_payloads,
        project_case_from_checkpoint,
    )
    from .workflow_projection import project_waiting_point
    from backend_logic2.workflow.process_graph import get_process_app

    projected = project_case_from_checkpoint(case_id)
    if projected.get("status") in _TERMINAL_CASE_STATUSES:
        return "terminal"

    case = case_repository.get_case(case_id) or projected
    snapshot = get_process_app().get_state(_config(case["thread_id"] or case["mr_name"]))
    waiting = project_waiting_point(_interrupt_payloads(snapshot))

    if waiting is None and (
        projected.get("status") in _UNFINISHED_STATUSES or snapshot.next
    ):
        # 사람을 기다리는 지점이 아닌데 멈춰 있다 = 노드 한가운데서 끊겼다.
        # FAILED로 두면 '다시 시도'가 그 지점부터 이어서 돈다.
        case_repository.transition_case(
            case_id,
            status="FAILED",
            stage="HUMAN_REVIEW",
            reason=reason,
            triggered_by=actor,
            last_error=error,
        )
        return "failed"
    if error is not None:
        # 사람의 답을 기다리는 지점이다. 그 자리는 그대로 두고 이유만 남긴다.
        status, stage = waiting or (
            str(projected.get("status")),
            projected.get("stage"),
        )
        case_repository.transition_case(
            case_id,
            status=status,
            stage=stage,
            reason=reason,
            triggered_by=actor,
            last_error=error,
        )
    return "waiting"


def recover_interrupted_work() -> dict[str, int]:
    """재시작 전에 처리 중이던 건을 다시 누를 수 있는 상태로 되돌린다."""
    counts = {"tasks_released": 0, "cases_settled": 0}
    touched: set[str] = set()

    for task in task_repository.list_tasks(status="PROCESSING"):
        case = case_repository.get_case(str(task["case_id"]))
        if case is None or not has_local_checkpoint(case):
            continue
        if task_repository.release_claimed_task(
            str(task["task_id"]), claimed_version=int(task["version"])
        ):
            counts["tasks_released"] += 1
        touched.add(str(case["case_id"]))

    for case in case_repository.list_open_case_references():
        if str(case.get("status")) in _UNFINISHED_STATUSES and has_local_checkpoint(case):
            touched.add(str(case["case_id"]))

    for case_id in sorted(touched):
        try:
            _settle(
                case_id,
                error=RESTART_MESSAGE,
                actor=RECOVERY_ACTOR,
                reason="서버 재시작으로 중단된 처리를 복구했습니다.",
            )
            counts["cases_settled"] += 1
        except Exception:  # noqa: BLE001 - 한 건 실패가 나머지를 막으면 안 된다
            LOGGER.exception("재시작 복구 실패: case_id=%s", case_id)

    if any(counts.values()):
        LOGGER.warning("재시작 복구: %s", counts)
    return counts


def resync_waiting_cases() -> dict[str, int]:
    """사람을 기다리는 건의 단계가 체크포인트와 어긋났으면 맞춘다.

    달라진 것만 쓴다 - transition_case는 부를 때마다 이력 행을 남기므로,
    같은 값을 다시 쓰면 이력이 의미 없이 불어난다.
    """
    from .workflow_service import _config, _interrupt_payloads
    from .workflow_projection import project_waiting_point
    from backend_logic2.workflow.process_graph import get_process_app

    counts = {"checked": 0, "resynced": 0}
    for case in case_repository.list_open_case_references():
        if not has_local_checkpoint(case):
            continue
        counts["checked"] += 1
        case_id = str(case["case_id"])
        try:
            snapshot = get_process_app().get_state(
                _config(case["thread_id"] or case["mr_name"])
            )
            waiting = project_waiting_point(_interrupt_payloads(snapshot))
            if waiting is None:
                continue
            status, stage = waiting
            if case.get("status") == status and case.get("stage") == stage:
                continue
            LOGGER.info(
                "대기 단계 재동기화: case_id=%s %s/%s -> %s/%s",
                case_id, case.get("status"), case.get("stage"), status, stage,
            )
            case_repository.transition_case(
                case_id,
                status=status,
                stage=stage,
                reason="워크플로가 기다리는 지점에 맞춰 단계를 정정했습니다.",
                triggered_by=RECOVERY_ACTOR,
            )
            counts["resynced"] += 1
        except Exception:  # noqa: BLE001
            LOGGER.exception("대기 단계 재동기화 실패: case_id=%s", case_id)
    return counts
