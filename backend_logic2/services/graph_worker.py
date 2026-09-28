"""그래프를 안전하게 돌리기 위한 실행 경로.

자동화 v1이 무너진 자리가 전부 여기다. 판정 로직은 거의 틀리지 않았고,
"그래프를 **누가 어디서** 돌리느냐"가 문제였다. 그래프 한 번은 몇 분이 걸릴
수 있고(ERPNext 쓰기, 메일 발송, 공급사 검색), 그 몇 분을 어디서 기다리느냐에
따라 사고의 종류가 달랐다.

  - HTTP 요청 안에서 기다림 -> nginx /api/ 60초 제한에 걸려 504. 화면에는
    "구매 작업 API 요청에 실패했습니다"만 남고, 그래프는 계속 돌아서 케이스가
    실패도 아닌 "...중"에 갇힌다.
  - BackgroundTasks에서 기다림 -> 동기 엔드포인트와 **같은** anyio
    스레드풀(기본 40개)을 먹는다. 쌓이면 로그인까지 스레드를 못 받는다.
  - async 라우트에서 기다림 -> 이벤트 루프가 멈춰 서버 전체가 먹통.
  - 폴러 스레드에서 기다림 -> 잠금을 쥔 채 다른 경로를 줄 세운다.

그래서 규칙은 하나다. **자동 경로가 그래프를 돌릴 때는 여기에 예약만 하고
즉시 돌아온다.** 사람이 누르는 경로는 지금 동작을 그대로 둔다 - 몇 주간 쓰던
검증된 길이라 건드리지 않는 것이 가장 안전하다.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Iterator

from procurement_db import get_connection

LOGGER = logging.getLogger(__name__)

_WORKER_PREFIX = "biddingflow-graph"
# 그래프는 전용 스레드 하나에서만 돈다. 어차피 워크플로 잠금이 한 번에
# 하나씩만 돌게 하므로, 스레드를 여러 개 두면 락을 기다리며 스레드만 먹는다.
# 스레드를 굶기는 대신 순서를 기다리게 한다.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix=_WORKER_PREFIX)
_DISPATCH_LOCK = threading.RLock()
_OUTSTANDING = 0


def _dispatch(work: Any, *args: Any, **kwargs: Any) -> Future:
    """Count queued AND executing work, so automatic jobs cannot build a backlog."""
    global _OUTSTANDING
    with _DISPATCH_LOCK:
        _OUTSTANDING += 1
        try:
            future = _EXECUTOR.submit(work, *args, **kwargs)
        except BaseException:
            _OUTSTANDING -= 1
            raise

        def finished(_future):
            global _OUTSTANDING
            with _DISPATCH_LOCK:
                _OUTSTANDING -= 1
        future.add_done_callback(finished)
        return future


def try_submit_idle(work: Any, *args: Any, **kwargs: Any) -> Future | None:
    """Low-priority automatic work: only admit ONE when the graph lane is idle."""
    with _DISPATCH_LOCK:
        if _OUTSTANDING:
            return None
        return _dispatch(work, *args, **kwargs)


def queued_count() -> int:
    with _DISPATCH_LOCK:
        return _OUTSTANDING

_IN_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()


class CaseBusy(Exception):
    """다른 인스턴스가 이 케이스를 잡고 있다. 기다리지 않고 다음 턴에 다시 본다."""


def on_graph_worker() -> bool:
    """지금 그래프 전용 스레드 위에서 돌고 있는가."""
    return threading.current_thread().name.startswith(_WORKER_PREFIX)


def submit(work: Any, *args: Any, **kwargs: Any) -> Future:
    """그래프 작업을 전용 스레드에 예약하고 즉시 돌아온다.

    예외는 여기서 삼키고 남긴다 - 한 건의 실패가 뒤에 선 작업을 막으면 안 된다.
    각 작업은 자기 실패를 케이스 상태와 기록에 스스로 남겨야 한다.
    """

    def _run() -> Any:
        try:
            return work(*args, **kwargs)
        except Exception:  # noqa: BLE001
            LOGGER.exception("[graph worker] %s 실패", getattr(work, "__name__", work))
            return None

    return _dispatch(_run)


def run_and_wait(work: Any, *args: Any, **kwargs: Any) -> Any:
    """전용 스레드에서 돌리고 끝날 때까지 기다린다.

    ⚠️ 요청 처리 중에는 쓰지 말 것. 기다리는 동안 그 요청이 60초 제한에 걸린다.
    이미 전용 스레드 위라면 그대로 부른다 - 스레드가 하나뿐이라 자기 자신을
    기다리면 영원히 멈춘다.
    """
    if on_graph_worker():
        return work(*args, **kwargs)
    return _dispatch(work, *args, **kwargs).result()


@contextmanager
def case_in_flight(case_id: str) -> Iterator[None]:
    """이 케이스의 그래프가 지금 돌고 있다고 표시한다."""
    key = str(case_id)
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.add(key)
    try:
        yield
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(key)


def is_case_in_flight(case_id: str) -> bool:
    """지금 실제로 도는 중인가.

    ⚠️ 케이스의 RUNNING 표시만 보고 "처리 중"이라고 막으면 안 된다. 그 표시는
    DB에 남고 실제 작업은 메모리에만 있어서, 재시작이나 예외로 작업이 사라지면
    표시만 영원히 남는다. 그러면 사용자는 "이미 처리가 진행 중입니다"만 보고
    아무것도 못 하게 된다(v1에서 실제로 그렇게 막혔다). 진짜 기준은 이 등록부다.
    """
    with _IN_FLIGHT_LOCK:
        return str(case_id) in _IN_FLIGHT


def _scalar(result: Any, column: str) -> Any:
    """한 칸짜리 결과를 읽는다.

    ⚠️ get_connection은 row_factory=dict_row라 fetchone()이 딕셔너리를 돌려준다.
    여기서 fetchone()[0]으로 숫자 인덱스를 쓰면 KeyError(0)이 난다. v1의 마감
    스캔이 이것 때문에 잠금을 잡는 첫 줄에서 매번 죽어서 **한 번도 돌지
    못했고**, 화면에는 "판정 중 오류가 났습니다. (0)"으로만 보였다.
    """
    row = result.fetchone()
    if row is None:
        raise RuntimeError(f"{column} 결과를 읽지 못했습니다.")
    return row[column]


@contextmanager
def case_lock(case_id: str) -> Iterator[None]:
    """한 케이스를 여러 인스턴스가 동시에 건드리지 않게 한다. **절대 기다리지 않는다.**

    ⚠️ v1은 기다리는 잠금(pg_advisory_xact_lock)이었다. 다른 쪽이 그래프를 몇 분
    돌리는 동안 스캔이 그 앞에서 하염없이 멈췄고, 스캔은 잡 전체 잠금까지 쥔
    채 멈추니까 다음 주기 스캔들도 전부 건너뛰어져서 마감이 지나도 아무 건도
    처리되지 않았다. 못 잡으면 즉시 CaseBusy로 돌아가고 다음 턴에 다시 본다.
    같은 프로세스 안의 경합은 전용 스레드가 이미 한 줄로 세운다.
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
