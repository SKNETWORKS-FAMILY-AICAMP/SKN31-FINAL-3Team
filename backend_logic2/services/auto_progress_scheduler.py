"""API 프로세스 안에서 도는 자동 진행 스캔 루프.

systemd 타이머 대신 쓸 수 있는 경로다. 서버에 따로 설치할 게 없어서
SSH 없이도 동작하고, 켜고 끄는 건 회사 정책 화면에서 한다(정책이 꺼짐이면
스캔은 돌아도 아무 건도 건드리지 않는다).

겹쳐 도는 것은 스캔 자체가 쓰는 Postgres 자문 잠금이 막는다. 그래서 이
루프와 systemd 타이머를 동시에 켜 둬도 실제로 도는 건 하나뿐이다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import partial

from backend_logic2.services.auto_progress_runner import run_due_auto_progress

LOGGER = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 600.0
# 테스트할 때 몇 초로 줄여 쓸 수 있게 하한을 낮게 둔다. 운영 기본값은
# 600초이고, 짧게 두면 그만큼 DB 조회가 잦아진다.
_MINIMUM_INTERVAL_SECONDS = 5.0


def scheduler_enabled() -> bool:
    """개발용 인스턴스에서 끄고 싶을 때 AUTO_PROGRESS_SCHEDULER=off.

    기본값은 켜짐이다 - 배포 서버가 아무 설정 없이 동작해야 하기 때문이다.
    데이터베이스를 같이 보는 개발 PC에서는 꺼 두는 편이 낫지만, 켜 두더라도
    체크포인트가 없는 건은 건드리지 않는다(run_due_auto_progress 참고).
    """
    raw = os.getenv("AUTO_PROGRESS_SCHEDULER", "on").strip().lower()
    return raw not in {"off", "false", "0", "no"}


def interval_seconds() -> float:
    try:
        configured = float(os.getenv("AUTO_PROGRESS_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS))
    except ValueError:
        configured = _DEFAULT_INTERVAL_SECONDS
    return max(_MINIMUM_INTERVAL_SECONDS, configured)


async def auto_progress_loop() -> None:
    """마감이 지난 케이스를 주기적으로 깨운다."""
    interval = interval_seconds()
    LOGGER.info("자동 진행 스캔 루프를 시작합니다 (%.0f초 주기)", interval)
    while True:
        try:
            # 기동 직후 다른 초기화와 겹치지 않게 한 주기 쉬고 시작한다.
            await asyncio.sleep(interval)
            counts = await asyncio.to_thread(
                partial(
                    run_due_auto_progress,
                    interval_seconds=int(interval),
                    ran_by="api-scheduler",
                )
            )
            if any(counts[key] for key in ("advanced", "blocked", "deadline_extended", "failed")):
                LOGGER.info("자동 진행 스캔: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 한 번의 실패로 루프가 끝나면 안 된다
            LOGGER.exception("자동 진행 스캔에 실패했습니다. %.0f초 뒤 다시 시도합니다.", interval)
