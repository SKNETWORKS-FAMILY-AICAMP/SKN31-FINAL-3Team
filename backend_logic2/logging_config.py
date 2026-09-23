"""Central logging setup shared by every FastAPI entry point.

``main.py``(포트 8000)와 ``webhook_gateway.py``(포트 18000, webhook-dev.ps1이
``-WindowStyle Hidden``으로 띄우는 별도 프로세스)는 서로 다른 프로세스라서,
콘솔 창이 보이든 숨겨져 있든 상관없이 로그가 디스크 파일에 남도록 여기서
한 번만 설정한다. 실사용 중인 앱이라 "터미널을 띄워둬야 로그가 보인다"는
전제 자체가 맞지 않는다 - 프로세스가 어떻게 시작되든 항상 파일로 남아야
한다.

각 entry point(main.py, webhook_gateway.py)는 다른 모든 import보다 먼저
``configure_logging()``을 호출한다.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def configure_logging() -> None:
    """Attach a rotating file handler (+ console) to the root logger.

    두 번째 호출부터는 아무 일도 하지 않는다(같은 프로세스 안에서 여러
    entry point가 겹쳐 import될 때 핸들러가 중복 추가되는 것을 막는다).
    """

    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = Path(__file__).resolve().parents[1] / ".runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    logging.getLogger(__name__).info(
        "logging configured: level=%s file=%s", level_name, log_file,
    )
