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
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
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
# 저절로 풀릴 수 있는 이유(규격 평가가 아직 도는 중 등)로 멈췄을 때, 사람을
# 부르기 전에 얼마나 더 기다려 볼지. 마감 시각 기준이다. 이 시간이 지나도
# 안 끝났으면 일시적인 지연이 아니라 고장이므로 사람을 부른다.
_TRANSIENT_RETRY_GRACE = timedelta(minutes=60)

# 판정 결과를 사람이 읽는 말로. 화면 토스트와 AI 판단 기록이 같은 문구를 쓴다.
OUTCOME_MESSAGES = {
    "advanced": "조건을 통과해 다음 단계로 넘어갔습니다.",
    "blocked": "조건에 걸려 멈췄습니다. 판정 내용을 확인하세요.",
    "waiting": "규격 평가처럼 곧 끝날 일을 기다리는 중입니다. 잠시 뒤 다시 판정합니다.",
    "recorded": "판정만 기록했습니다(기록 모드).",
    "deadline_extended": "회신이 없어 마감을 자동으로 연장했습니다.",
    "held": "담당자가 자동 진행을 보류해 둔 건입니다.",
    "disabled": "회사 정책에서 자동 진행이 꺼져 있습니다.",
    "unchanged": "지난 판정 이후 상황이 달라지지 않아 다시 판정하지 않았습니다.",
    "no_task": "지금 견적 확인을 기다리는 단계가 아니라 판정할 것이 없습니다.",
    "not_mine": "이 서버에 이 건의 워크플로 진행 상황이 없습니다.",
    "not_due": "아직 마감 전이고 모든 협력사가 회신하지 않아 판정할 때가 아닙니다.",
    "busy": "다른 처리가 이 건을 잡고 있어 이번에는 건너뛰었습니다. 다음 스캔에서 다시 봅니다.",
    "failed": "판정 중 오류가 났습니다.",
    "queued": "자동 진행 판정을 요청했습니다. 결과는 잠시 뒤 화면과 AI 판단 기록에 반영됩니다.",
    "already_queued": "이미 판정이 예약되어 있습니다. 잠시 뒤 화면을 확인해 주세요.",
}


class CaseBusy(Exception):
    """다른 인스턴스가 이 케이스를 처리 중이다."""


def _scalar(result, column: str) -> Any:
    """한 칸짜리 결과를 읽는다.

    ⚠️ get_connection은 row_factory=dict_row라 fetchone()이 딕셔너리를 돌려준다.
    여기서 예전처럼 fetchone()[0]으로 숫자 인덱스를 쓰면 KeyError(0)이 난다.
    그 예외 때문에 10분 주기 스캔이 잠금을 잡는 첫 줄에서 매번 죽어서, 마감이
    지나도 아무 건도 자동 진행되지 않았다(화면에는 "판정 중 오류가 났습니다. (0)"
    로만 보였다). 컬럼 이름으로 읽는다.
    """
    row = result.fetchone()
    if row is None:
        raise RuntimeError(f"{column} 결과를 읽지 못했습니다.")
    return row[column]


@contextmanager
def _scan_lock() -> Iterator[bool]:
    """잡이 겹쳐 도는 것을 막는다. 못 얻으면 이번 턴은 건너뛴다."""
    connection = None
    acquired = False
    try:
        connection = get_connection().__enter__()
        acquired = bool(
            _scalar(
                connection.execute(
                    "SELECT pg_try_advisory_lock(%(key)s) AS acquired",
                    {"key": _SCAN_LOCK_KEY},
                ),
                "acquired",
            )
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
    """한 케이스를 다른 인스턴스와 동시에 처리하지 않게 한다. **절대 기다리지 않는다.**

    ⚠️ 예전엔 pg_advisory_xact_lock(기다리는 잠금)이라, 다른 쪽이 그래프를 몇 분
    돌리는 동안 스캔이 그 앞에서 하염없이 멈췄다. 스캔은 잡 전체 잠금까지
    쥔 채 멈추니까 다음 주기 스캔들도 전부 "다른 실행이 돌고 있다"며 건너뛰어,
    마감이 지나도 아무 건도 자동 처리되지 않았다. 게다가 긴 트랜잭션을 열어둔
    채였다. 지금은 못 잡으면 즉시 CaseBusy로 돌아가고, 다음 스캔이 다시 본다.
    같은 프로세스 안의 경합은 그래프 전용 스레드가 이미 한 줄로 세운다.
    """
    key = {"case_id": str(case_id)}
    with get_connection(autocommit=True) as connection:
        acquired = bool(
            _scalar(
                connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%(case_id)s, 0))"
                    " AS acquired",
                    key,
                ),
                "acquired",
            )
        )
        if not acquired:
            raise CaseBusy(str(case_id))
        try:
            yield
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%(case_id)s, 0))", key
            )


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


def _required_by(mr_name: str) -> date | None:
    """MR의 납기요청일.

    ⚠️ 예전엔 case 행의 required_by / schedule_date를 봤는데 procurement_case
    테이블에 그런 컬럼이 아예 없어서 항상 None이었다. 그래서 "납기 여유가
    정책값 이상 남았을 때만 연장한다"는 검사가 한 번도 작동하지 않았고,
    납기가 코앞이어도 연장이 그대로 나갔다. 납기는 ERPNext에서 읽는다.
    """
    from backend_logic2.integrations.erp_client import erp_get_one

    if not mr_name:
        return None
    try:
        material_request = erp_get_one("Material Request", mr_name) or {}
    except Exception:  # noqa: BLE001 - 조회 실패는 연장 포기 사유일 뿐이다
        LOGGER.warning("납기요청일 조회 실패: mr_name=%s", mr_name, exc_info=True)
        return None
    raw = str(material_request.get("schedule_date") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _within_retry_grace(case: dict[str, Any]) -> bool:
    """아직 더 기다려 볼 만한 시점인가."""
    deadline = _as_utc(case.get("quotation_deadline_at"))
    if deadline is None:
        return False
    return datetime.now(timezone.utc) <= deadline + _TRANSIENT_RETRY_GRACE


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

    # 납기를 모르면 연장하지 않는다 - 판단이 애매하면 사람에게 넘긴다.
    required_by = _required_by(str(case.get("mr_name") or ""))
    if required_by is None:
        LOGGER.info(
            "납기요청일을 알 수 없어 자동 연장을 하지 않습니다: case_id=%s",
            case.get("case_id"),
        )
        return False
    now = datetime.now(timezone.utc)
    new_deadline = now + timedelta(days=days)
    lead_left = (
        datetime.combine(required_by, datetime.min.time(), tzinfo=timezone.utc) - new_deadline
    ).days
    if lead_left < int(rules.auto_deadline_extension_min_lead_days):
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


def process_case(
    case: dict[str, Any], *, trigger: str = "deadline", force: bool = False
) -> str:
    """케이스 하나를 자동 진행 시도한다. 결과 코드를 돌려준다.

    ⚠️ 그래프를 돌리므로 반드시 그래프 전용 스레드에서만 부른다(enqueue_case).
    force=True는 사람이 '지금 확인'을 눌렀을 때 - 같은 상황이어도 다시 본다.
    """
    from backend_logic2.services import workflow_service

    case_id = str(case["case_id"])
    _LAST_ERROR.pop(case_id, None)
    if case.get("automation_hold"):
        return "held"

    policy = _policy_for(case)
    if policy.rules.automation_mode == "off":
        return "disabled"

    # 진행 상황을 가진 인스턴스만 이 건을 건드린다(마감 연장 포함).
    if not has_local_checkpoint(case):
        LOGGER.info(
            "이 인스턴스에 워크플로 진행 상황이 없어 건너뜁니다: case_id=%s", case_id
        )
        return "not_mine"

    if trigger == "deadline" and _extend_deadline_for_silence(case, policy):
        return "deadline_extended"

    task = _pending_quotation_task(case_id)
    if task is None:
        return "no_task"

    # ⚠️ 상황이 그대로면 어떤 경로로 불렸든 다시 평가하지 않는다. 예전엔
    # deadline일 때만 걸러서, 견적 동기화 폴러(10초 주기)가 부르는 전원 회신
    # 경로는 같은 건을 끝없이 재평가했다. 그래프 작업 줄이 그걸로 가득 차서
    # 사람이 누른 선정·승인이 뒤에서 계속 밀렸다.
    signature = situation_signature(case)
    if not force and case.get("auto_progress_signature") == signature:
        return "unchanged"

    try:
        with case_lock(case_id):
            try:
                workflow_service.resume_task(
                    str(task["task_id"]),
                    answer={"decision": "auto", "trigger": trigger},
                    answered_by=_AUTO_ANSWERED_BY,
                    expected_version=task.get("version"),
                )
            except Exception as exc:  # noqa: BLE001 - 한 건의 실패가 나머지를 막지 않는다
                LOGGER.exception("자동 진행 시도 실패: case_id=%s", case_id)
                _LAST_ERROR[case_id] = str(exc)[:300]
                case_repository.mark_auto_progress_attempt(case_id, signature)
                return "failed"
    except CaseBusy:
        return "busy"

    refreshed = case_repository.get_case(case_id) or case
    decision = _workflow_values(refreshed).get("auto_progress") or {}
    if str(refreshed.get("stage") or "") != str(case.get("stage") or ""):
        case_repository.mark_auto_progress_attempt(case_id, None)
        return "advanced"

    if decision.get("allowed") is False:
        if decision.get("retryable") and _within_retry_grace(refreshed):
            # 규격 평가가 아직 도는 중처럼 저절로 풀릴 수 있는 이유다.
            # 지문을 남기지 않아서 다음 스캔이 같은 건을 다시 본다.
            case_repository.mark_auto_progress_attempt(case_id, None)
            LOGGER.info("일시적인 이유로 멈춤 - 잠시 뒤 다시 봅니다: case_id=%s", case_id)
            return "waiting"
        case_repository.mark_auto_progress_attempt(case_id, signature)
        _notify_blocked(refreshed, decision)
        return "blocked"
    case_repository.mark_auto_progress_attempt(case_id, signature)
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
        # 여기서 직접 돌리지 않는다. 웹훅·견적 동기화 폴러 어디서 불리든
        # 그래프는 전용 스레드에서만 돈다.
        return "queued" if enqueue_case(str(case_id), trigger="all_responded") else "already_queued"
    except Exception:  # noqa: BLE001 - 웹훅 처리를 되돌리면 안 된다
        LOGGER.exception("전원 회신 자동 진행 예약 실패: case_id=%s", case_id)
        return "failed"


def run_due_auto_progress(
    now: datetime | None = None,
    *,
    interval_seconds: int | None = None,
    ran_by: str | None = None,
) -> dict[str, int]:
    """마감이 지난 케이스를 찾아 그래프 전용 스레드에 판정을 예약한다.

    ⚠️ 여기서 직접 판정하지 않는다. 예전엔 스캔 스레드가 그래프를 몇 분씩
    직접 돌리면서 잡 전체 잠금을 쥐고 있어, 한 건이 늘어지면 뒤 스캔이 전부
    건너뛰어졌다. 지금 스캔은 "누가 마감이 지났나"만 보고 몇 초 안에 끝난다.
    실제 판정은 전용 스레드에서 한 건씩 돌고, 결과는 AI 판단 기록에 남는다.
    """
    counts = {"scanned": 0, "queued": 0, "already_queued": 0, "skipped": 0}
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
            if enqueue_case(str(case["case_id"]), trigger="deadline"):
                counts["queued"] += 1
            else:
                counts["already_queued"] += 1

        try:
            case_repository.record_automation_scan(
                counts, interval_seconds=interval_seconds, ran_by=ran_by
            )
        except Exception:  # noqa: BLE001 - 기록 실패가 스캔을 실패시키면 안 된다
            LOGGER.warning("스캔 실행 기록에 실패했습니다.", exc_info=True)
    return counts



# ---------------------------------------------------------------------------
# 예약: 모든 자동 진행 판정은 여기를 거쳐 그래프 전용 스레드에서 돈다.
# ---------------------------------------------------------------------------

_PENDING_LOCK = threading.Lock()
_PENDING: set[str] = set()          # 예약됐거나 지금 도는 케이스
_LAST_ERROR: dict[str, str] = {}    # 마지막 실패 사유(기록용)
_LAST_LOGGED: dict[str, str] = {}   # 자동 판정 기록이 같은 말을 반복하지 않게

# 자동(스캔·전원 회신) 판정 중 매번 남길 결과 / 바뀔 때만 남길 결과.
# 나머지(unchanged, disabled, held, recorded)는 10분마다 쌓이는 잡음이라 남기지 않는다.
_ALWAYS_LOG = {"advanced", "deadline_extended", "failed"}
_LOG_ON_CHANGE = {"blocked", "waiting", "busy", "no_task", "not_mine"}
_TRIGGER_LABELS = {
    "manual": "지금 확인",
    "deadline": "마감 스캔",
    "all_responded": "전원 회신",
}


def enqueue_case(
    case_id: str, *, trigger: str, force: bool = False, manual: bool = False
) -> bool:
    """판정을 그래프 전용 스레드에 예약한다. 이미 예약·진행 중이면 False.

    같은 건이 스캔·전원 회신·'지금 확인'으로 여러 번 들어와도 한 번만 돈다.
    """
    from backend_logic2.services.workflow_service import submit_graph_work

    with _PENDING_LOCK:
        if case_id in _PENDING:
            return False
        _PENDING.add(case_id)
    try:
        submit_graph_work(
            _process_queued_case, case_id, trigger=trigger, force=force, manual=manual
        )
    except Exception:
        with _PENDING_LOCK:
            _PENDING.discard(case_id)
        raise
    return True


def _process_queued_case(
    case_id: str, *, trigger: str, force: bool, manual: bool
) -> str:
    outcome = "failed"
    try:
        case = case_repository.get_case(case_id)
        if case is None:
            return "no_task"
        from backend_logic2.services.workflow_service import graph_case_in_flight

        with graph_case_in_flight(case_id):
            outcome = process_case(case, trigger=trigger, force=force)
    except Exception as exc:  # noqa: BLE001 - 전용 스레드는 다음 작업을 계속해야 한다
        LOGGER.exception("자동 진행 판정 실패: case_id=%s", case_id)
        _LAST_ERROR[case_id] = str(exc)[:300]
        outcome = "failed"
    finally:
        with _PENDING_LOCK:
            _PENDING.discard(case_id)
    _record_outcome(case_id, outcome, trigger="manual" if manual else trigger, manual=manual)
    return outcome


def _record_outcome(case_id: str, outcome: str, *, trigger: str, manual: bool) -> None:
    """판정 결과를 AI 판단 기록에 남긴다 - 서버 로그를 못 봐도 화면에서 이유를 알 수 있게.

    "마감이 지났는데 왜 안 넘어가지"의 답이 전부 여기 남는다.
    """
    error = _LAST_ERROR.pop(case_id, None)
    if not manual:
        if outcome in _LOG_ON_CHANGE and _LAST_LOGGED.get(case_id) == outcome:
            return
        if outcome not in _ALWAYS_LOG and outcome not in _LOG_ON_CHANGE:
            return
    _LAST_LOGGED[case_id] = outcome
    message = OUTCOME_MESSAGES.get(outcome, outcome)
    if error:
        message = f"{message} ({error})"
    label = _TRIGGER_LABELS.get(trigger, trigger)
    try:
        from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision

        log_ai_decision(case_id, "auto_progress_scan", f"[{label}] {message}")
    except Exception:  # noqa: BLE001 - 기록 실패가 판정을 되돌리면 안 된다
        LOGGER.warning("자동 진행 결과 기록 실패: case_id=%s", case_id, exc_info=True)


def manual_check_trigger(case: dict[str, Any], now: datetime | None = None) -> str | None:
    """'지금 확인'을 눌렀을 때 어떤 근거로 판정할지. 근거가 없으면 None.

    ⚠️ 그래프는 trigger가 "all_responded"가 아니면 마감이 지났다고 믿는다.
    예전엔 '지금 확인'이 무조건 "deadline"을 넘겨서, 마감 전에 누르면 마감이
    지난 것처럼 자동 선정될 수 있었다. 사실대로만 넘긴다.
    """
    moment = now or datetime.now(timezone.utc)
    deadline = _as_utc(case.get("quotation_deadline_at"))
    if deadline is not None and deadline <= moment:
        return "deadline"
    snapshot = case.get("quotation_snapshot") or {}
    recipients = int(snapshot.get("recipient_count") or 0)
    responded = int(snapshot.get("responded_count") or 0)
    if recipients > 0 and responded >= recipients:
        return "all_responded"
    return None


def request_manual_check(case: dict[str, Any]) -> dict[str, Any]:
    """'자동 진행 지금 확인' 버튼. 즉시 돌아온다 - 판정은 전용 스레드에서.

    바로 알 수 있는 이유(보류, 꺼짐, 단계 아님, 마감 전)는 그 자리에서 답하고,
    나머지는 예약한 뒤 결과를 AI 판단 기록에 남긴다.
    """
    case_id = str(case["case_id"])
    if case.get("automation_hold"):
        outcome = "held"
    elif _policy_for(case).rules.automation_mode == "off":
        outcome = "disabled"
    elif str(case.get("stage") or "") != "QUOTATION_COLLECTION":
        outcome = "no_task"
    else:
        trigger = manual_check_trigger(case)
        if trigger is None:
            outcome = "not_due"
        elif enqueue_case(case_id, trigger=trigger, force=True, manual=True):
            outcome = "queued"
        else:
            outcome = "already_queued"
    return {
        "outcome": outcome,
        "message": OUTCOME_MESSAGES.get(outcome, outcome),
        "queued": outcome in {"queued", "already_queued"},
        "stage": case.get("stage"),
    }
