"""견적 마감 스캔 - 자동화 v2의 5단계 c.

Legacy compatibility only. Do not register this whole-case sweep as a periodic
task again; the opt-in durable replacement is services.deadline_scheduler.

마감이 지났는데 아무 일도 일어나지 않는 건을 찾아 판정을 시작한다.

v1에서 이 자리가 가장 많이 터졌다. 그래서 지키는 것:

  - **그래프는 전용 스레드에서만 돈다.** 요청 안에서 기다리면 504, 배경작업에
    맡기면 스레드풀이 차서 로그인이 멈췄다.
  - **케이스 잠금은 절대 기다리지 않는다.** 기다리는 잠금 하나가 스캔 전체를
    세우고 그 뒤 스캔까지 연쇄로 막았다.
  - **체크포인트가 없는 케이스는 건드리지 않는다.** DB는 다른 인스턴스와
    함께 보지만 체크포인트는 인스턴스마다 따로다.
  - **RUNNING 표시가 아니라 실제로 도는지를 본다.**
  - **같은 상태를 두 번 판정하지 않는다.** 판정하면 대기 작업이 다시
    생기므로, 막지 않으면 60초마다 같은 판정을 반복한다(v1의 무한 뺑뺑이).
"""

from __future__ import annotations

import logging
from typing import Any

from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import tasks as task_repository
from backend_logic2.services import deadline_readiness, graph_worker
from backend_logic2.services.case_recovery import has_local_checkpoint

LOGGER = logging.getLogger(__name__)

QUOTATION_TASK_TYPES = {"quotation_check", "check_quotations"}
AUTOMATION_ACTOR = "system:auto-final-selection"

# 이미 판정한 상태의 지문. 판정하면 대기 작업이 새로 생기므로 이게 없으면
# 스캔이 같은 건을 영원히 다시 판정한다.
#
# ⚠️ 일부러 메모리에만 둔다. 재시작하면 비므로 배포 후 한 번 더 판정할 수
# 있는데, 그건 무해하다(판정은 조건에 걸리면 아무것도 바꾸지 않는다). DB에
# 표시를 남기면 그 표시가 낡아서 재비딩을 막는 쪽이 훨씬 위험하다 - v1에서
# 실제로 지난 판정이 남아 다음 차수를 막았다.
_JUDGED: dict[str, str] = {}


def _fingerprint(readiness: deadline_readiness.Readiness) -> str:
    """무엇이 바뀌면 다시 판정해야 하는가: 마감과 견적 구성."""
    parts = [readiness.deadline.isoformat() if readiness.deadline else ""]
    for row in sorted(
        [*readiness.late, *readiness.pending, *readiness.unknown],
        key=lambda item: str(item.get("quotation_id") or ""),
    ):
        parts.append(str(row.get("quotation_id") or ""))
    parts.append(f"{readiness.responded}/{readiness.recipients}")
    return "|".join(parts)


def forget(case_id: str) -> None:
    """이 케이스의 판정 기록을 잊는다(재비딩·마감 변경 등으로 판을 새로 깔 때)."""
    _JUDGED.pop(str(case_id), None)


def scan_due_cases() -> dict[str, int]:
    """마감이 지난 건을 찾아 판정을 시작한다. 전용 스레드에서 부른다."""
    counts = {"checked": 0, "waiting": 0, "judged": 0, "busy": 0, "skipped": 0}
    try:
        cases = case_repository.list_cases_awaiting_quotation_deadline()
    except Exception:
        LOGGER.exception("마감 스캔 대상 조회 실패")
        return counts

    live = {str(case["case_id"]) for case in cases}
    for stale in set(_JUDGED) - live:
        _JUDGED.pop(stale, None)

    for case in cases:
        case_id = str(case["case_id"])
        try:
            if not has_local_checkpoint(case):
                counts["skipped"] += 1
                continue
            if graph_worker.is_case_in_flight(case_id):
                counts["busy"] += 1
                continue
            counts["checked"] += 1
            _consider(case, counts)
        except graph_worker.CaseBusy:
            counts["busy"] += 1
        except Exception:
            # ⚠️ 한 건이 터져도 나머지는 계속 본다. v1에서는 앞의 한 건이
            # 스캔 전체를 세웠다.
            LOGGER.exception("마감 판정 실패: case_id=%s", case_id)
    return counts


def _consider(case: dict[str, Any], counts: dict[str, int]) -> None:
    case_id = str(case["case_id"])
    # 판정 시점 판단에는 회사 정책이 필요 없다(마감과 처리 완료 여부만 본다).
    # 조건 판정은 그래프 안에서 돌고, 거기서 케이스에 고정된 정책이 바인딩된다.
    readiness = deadline_readiness.judge(case)
    if not readiness.ready:
        counts["waiting"] += 1
        LOGGER.info("마감 판정 대기: case_id=%s %s", case_id, readiness.reason)
        return

    fingerprint = _fingerprint(readiness)
    if _JUDGED.get(case_id) == fingerprint:
        counts["skipped"] += 1
        return

    task = _pending_quotation_task(case_id)
    if task is None:
        # 대기 작업이 없으면 사람 입력을 기다리는 지점이 아니다.
        counts["skipped"] += 1
        return

    # 실제로 판정을 시작하기 직전에만 표시한다. 여기서 실패하면 다시
    # 시도해야 하므로, 성공 후에 기록한다.
    with graph_worker.case_lock(case_id), graph_worker.case_in_flight(case_id):
        _resume(case, task, readiness)
    _JUDGED[case_id] = fingerprint
    counts["judged"] += 1


def _pending_quotation_task(case_id: str) -> dict[str, Any] | None:
    for task in task_repository.list_tasks(
        case_id=case_id, audience="BUYER", status="PENDING"
    ):
        if str(task.get("task_type")) in QUOTATION_TASK_TYPES:
            return task
    return None


def _resume(
    case: dict[str, Any],
    task: dict[str, Any],
    readiness: deadline_readiness.Readiness,
) -> None:
    from backend_logic2.services.workflow_service import resume_task

    LOGGER.info(
        "마감 판정 시작: case_id=%s %s", case["case_id"], readiness.reason
    )
    resume_task(
        str(task["task_id"]),
        answer={
            "decision": "auto",
            "ready": True,
            "ready_reason": readiness.reason,
            "late": readiness.late,
        },
        answered_by=AUTOMATION_ACTOR,
        expected_version=int(task["version"]),
    )


__all__ = ["AUTOMATION_ACTOR", "forget", "scan_due_cases"]
