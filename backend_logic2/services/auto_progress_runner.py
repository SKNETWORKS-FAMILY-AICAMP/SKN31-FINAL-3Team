"""마감이 지난 케이스를 깨워 자동 진행을 시도하는 스캔 로직.

왜 필요한가: 워크플로는 사람의 답을 기다리며 **꺼져 있다**. 견적이 도착하면
그건 사건이라 웹훅이 깨울 수 있지만, "마감 시각이 됐다"는 아무 사건도
아니어서 아무도 깨워주지 않으면 영원히 멈춰 있다. 이 스캔이 그 알람이다.

새로운 재개 경로를 만들지는 않는다. 대기 중인 견적 확인 작업에 사람 대신
{"decision": "auto"}로 답해, 기존 경로를 그대로 탄다(누가 답했는지는
answered_by에 남는다).

동시성: 잡 전체에 하나, 케이스마다 하나씩 Postgres 자문 잠금을 건다.
프로세스가 여러 개여도(API 재시작, 타이머, 웹훅) DB는 하나라 이게 유일하게
믿을 수 있는 기준점이다.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from procurement_db import get_connection

from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.repositories import tasks as task_repository

LOGGER = logging.getLogger(__name__)

# 잡 전체 잠금 키(임의의 고정값). 케이스별 잠금은 case_id 해시를 쓴다.
_SCAN_LOCK_KEY = 8_241_007
_QUOTATION_TASK_TYPES = {"quotation_check", "check_quotations"}
_AUTO_ANSWERED_BY = "system:auto-progress"


@contextmanager
def _scan_lock() -> Iterator[bool]:
    """잡이 겹쳐 도는 것을 막는다. 못 얻으면 이번 턴은 건너뛴다."""
    connection = None
    acquired = False
    try:
        connection = get_connection().__enter__()
        acquired = bool(
            connection.execute(
                "SELECT pg_try_advisory_lock(%(key)s)", {"key": _SCAN_LOCK_KEY}
            ).fetchone()[0]
        )
        yield acquired
    finally:
        if connection is not None:
            try:
                if acquired:
                    connection.execute(
                        "SELECT pg_advisory_unlock(%(key)s)", {"key": _SCAN_LOCK_KEY}
                    )
            finally:
                connection.close()


@contextmanager
def case_lock(case_id: str) -> Iterator[None]:
    """한 케이스를 처리하는 동안 견적 웹훅 등이 끼어들지 못하게 한다."""
    with get_connection() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(case_id)s, 0))",
            {"case_id": str(case_id)},
        )
        yield


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _workflow_values(case: dict[str, Any]) -> dict[str, Any]:
    snapshot = case.get("workflow_snapshot") or {}
    values = snapshot.get("values") if isinstance(snapshot, dict) else {}
    return values if isinstance(values, dict) else {}


def situation_signature(case: dict[str, Any]) -> str:
    """지금 상황의 지문.

    같은 상황이면 스캔이 같은 건을 10분마다 다시 평가하며 알림을 쌓지
    않는다. 견적이 더 들어오거나 마감이 연장되면 지문이 바뀌어 다시 본다.
    """
    values = _workflow_values(case)
    snapshot = case.get("quotation_snapshot") or {}
    deadline = case.get("quotation_deadline_at")
    return "|".join([
        str(values.get("rfq_name") or ""),
        str(deadline.isoformat() if isinstance(deadline, datetime) else deadline or ""),
        str(snapshot.get("responded_count") or 0),
        str(snapshot.get("recipient_count") or 0),
    ])


def has_local_checkpoint(case: dict[str, Any]) -> bool:
    """이 인스턴스가 그 케이스의 워크플로 진행 상황을 갖고 있는가.

    ⚠️ 데이터베이스는 서버와 개발용 PC가 함께 보는데, 워크플로 체크포인트는
    인스턴스마다 따로 있는 SQLite 파일이다. 체크포인트가 없는 쪽이 케이스를
    재개하면 처음부터 다시 도는 사고가 난다. 그래서 스캔은 자기가 진행
    상황을 갖고 있는 건만 건드린다.
    """
    thread_id = str(case.get("thread_id") or case.get("mr_name") or "").strip()
    if not thread_id:
        return False
    try:
        from backend_logic2.workflow.process_graph import get_process_app

        snapshot = get_process_app().get_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - 확인이 안 되면 건드리지 않는다
        LOGGER.warning("체크포인트 확인 실패: thread_id=%s", thread_id, exc_info=True)
        return False
    return bool(snapshot and (snapshot.next or snapshot.values))


def _pending_quotation_task(case_id: str) -> dict[str, Any] | None:
    for task in task_repository.list_tasks(case_id=case_id, audience="BUYER", status="PENDING"):
        if str(task.get("task_type")) in _QUOTATION_TASK_TYPES:
            return task
    return None


def _policy_for(case: dict[str, Any]):
    from backend_logic2.policies.repository import for_case
    from backend_logic2.policies.schema import CompanyPolicy

    return CompanyPolicy.model_validate(for_case(str(case["case_id"]))["policy"])


def _extend_deadline_for_silence(case: dict[str, Any], policy) -> bool:
    """회신이 한 건도 없을 때 한 번만 자동으로 마감을 미룬다.

    연장만 반복하다 납기를 놓치는 게 가장 나쁜 결말이라, 납기 여유가
    정책값 이상 남았을 때만, 그리고 딱 한 번만 연장한다.
    """
    from backend_logic2.services import workflow_service

    rules = policy.rules
    # ⚠️ 기록 모드(섀도)는 판정만 남기고 아무것도 바꾸지 않아야 한다.
    # 마감을 실제로 미뤄버리면 "기록만 한다"는 약속이 깨진다.
    if rules.automation_mode != "on":
        return False
    days = int(rules.auto_deadline_extension_days)
    if days <= 0 or case.get("auto_deadline_extended_at"):
        return False
    snapshot = case.get("quotation_snapshot") or {}
    if int(snapshot.get("responded_count") or 0) > 0:
        return False

    required_by = case.get("required_by") or case.get("schedule_date")
    now = datetime.now(timezone.utc)
    new_deadline = now + timedelta(days=days)
    if isinstance(required_by, datetime):
        lead_left = (required_by - new_deadline).days
    elif hasattr(required_by, "isoformat"):  # date
        lead_left = (datetime.combine(required_by, datetime.min.time(), tzinfo=timezone.utc) - new_deadline).days
    else:
        lead_left = None
    if lead_left is not None and lead_left < int(rules.auto_deadline_extension_min_lead_days):
        return False

    try:
        workflow_service.extend_quotation_deadline(
            str(case["case_id"]),
            deadline_at=new_deadline.isoformat(),
            changed_by=_AUTO_ANSWERED_BY,
        )
    except Exception:  # noqa: BLE001 - 연장 실패는 자동 선정 시도로 넘어갈 뿐이다
        LOGGER.warning("회신 0건 자동 연장 실패: case_id=%s", case.get("case_id"), exc_info=True)
        return False
    case_repository.mark_auto_deadline_extended(str(case["case_id"]))
    LOGGER.info("회신 0건으로 마감을 %d일 자동 연장했습니다: case_id=%s", days, case["case_id"])
    return True


def _notify_blocked(case: dict[str, Any], decision_payload: dict[str, Any]) -> None:
    blockers = [row for row in decision_payload.get("checks") or [] if row.get("status") != "passed"]
    detail = "; ".join(str(row.get("detail")) for row in blockers[:2]) or "조건 확인 필요"
    notification_repository.create_notification(
        case_id=str(case["case_id"]),
        recipient_id=case.get("assigned_user_id"),
        notification_type="AUTO_PROGRESS_BLOCKED",
        title="자동 선정을 멈췄습니다 — 결정이 필요합니다",
        message=f"{case.get('mr_name') or ''} · {detail}",
        payload={
            "mr_name": case.get("mr_name"),
            "stage": case.get("stage"),
            "checks": decision_payload.get("checks"),
            "evidence": decision_payload.get("evidence"),
        },
    )


def process_case(case: dict[str, Any], *, trigger: str = "deadline") -> str:
    """케이스 하나를 자동 진행 시도한다. 결과 코드를 돌려준다."""
    from backend_logic2.services import workflow_service

    case_id = str(case["case_id"])
    if case.get("automation_hold"):
        return "held"

    policy = _policy_for(case)
    if policy.rules.automation_mode == "off":
        return "disabled"

    if trigger == "deadline" and _extend_deadline_for_silence(case, policy):
        return "deadline_extended"

    task = _pending_quotation_task(case_id)
    if task is None:
        return "no_task"

    if not has_local_checkpoint(case):
        LOGGER.info(
            "이 인스턴스에 워크플로 진행 상황이 없어 건너뜁니다: case_id=%s", case_id
        )
        return "not_mine"

    signature = situation_signature(case)
    if trigger == "deadline" and case.get("auto_progress_signature") == signature:
        return "unchanged"

    with case_lock(case_id):
        try:
            workflow_service.resume_task(
                str(task["task_id"]),
                answer={"decision": "auto", "trigger": trigger},
                answered_by=_AUTO_ANSWERED_BY,
                expected_version=task.get("version"),
            )
        except Exception:  # noqa: BLE001 - 한 건의 실패가 나머지를 막지 않는다
            LOGGER.exception("자동 진행 시도 실패: case_id=%s", case_id)
            case_repository.mark_auto_progress_attempt(case_id, signature)
            return "failed"

    refreshed = case_repository.get_case(case_id) or case
    decision = _workflow_values(refreshed).get("auto_progress") or {}
    if str(refreshed.get("stage") or "") != str(case.get("stage") or ""):
        case_repository.mark_auto_progress_attempt(case_id, None)
        return "advanced"

    case_repository.mark_auto_progress_attempt(case_id, signature)
    if decision.get("allowed") is False:
        _notify_blocked(refreshed, decision)
        return "blocked"
    return "recorded"


def trigger_on_full_response(case_id: str) -> str:
    """전원이 회신하면 마감을 기다리지 않고 바로 판정한다.

    마지막 견적이 도착하는 건 분명한 사건이라 스캔을 기다릴 이유가 없다.
    견적 웹훅이 실시간 순위를 갱신한 뒤 이어서 부른다.
    """
    try:
        case = case_repository.get_case(str(case_id))
        if case is None or str(case.get("stage") or "") != "QUOTATION_COLLECTION":
            return "skipped"
        snapshot = case.get("quotation_snapshot") or {}
        recipients = int(snapshot.get("recipient_count") or 0)
        responded = int(snapshot.get("responded_count") or 0)
        if recipients <= 0 or responded < recipients:
            return "waiting"
        return process_case(case, trigger="all_responded")
    except Exception:  # noqa: BLE001 - 웹훅 처리를 되돌리면 안 된다
        LOGGER.exception("전원 회신 자동 진행 실패: case_id=%s", case_id)
        return "failed"


def run_due_auto_progress(now: datetime | None = None) -> dict[str, int]:
    """마감이 지난 케이스를 찾아 자동 진행을 시도한다.

    한 번에 하나씩 순서대로 처리한다 - 워크플로 체크포인트가 SQLite 파일
    하나라 동시에 여러 건을 돌리면 파일 잠금에서 부딪힌다.
    """
    counts = {
        "scanned": 0, "advanced": 0, "blocked": 0, "recorded": 0,
        "deadline_extended": 0, "held": 0, "skipped": 0, "failed": 0,
        "not_mine": 0,
    }
    moment = now or datetime.now(timezone.utc)

    with _scan_lock() as acquired:
        if not acquired:
            LOGGER.info("다른 실행이 아직 돌고 있어 이번 턴을 건너뜁니다.")
            counts["skipped"] += 1
            return counts

        for case in case_repository.list_cases_for_quotation_reconciliation():
            if str(case.get("stage") or "") != "QUOTATION_COLLECTION":
                continue
            deadline = _as_utc(case.get("quotation_deadline_at"))
            if deadline is None or deadline > moment:
                continue
            counts["scanned"] += 1
            try:
                outcome = process_case(case)
            except Exception:  # noqa: BLE001
                LOGGER.exception("자동 진행 스캔 실패: case_id=%s", case.get("case_id"))
                outcome = "failed"
            if outcome in counts:
                counts[outcome] += 1
            else:
                counts["skipped"] += 1
    return counts
