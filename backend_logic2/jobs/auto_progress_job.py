"""마감이 지난 케이스를 깨워 자동 진행을 시도하는 스케줄 잡.

실행: python -m backend_logic2.jobs.auto_progress_job
보통은 설치할 필요가 없다 - API 서버가 같은 스캔을 10분마다 직접 돌린다
(services/auto_progress_scheduler.py). 이 잡은 손으로 한 번 돌려보거나,
API와 분리해서 systemd 타이머로 돌리고 싶을 때 쓴다
(deploy/systemd/biddingflow-auto-progress.{service,timer}.example).
둘을 같이 켜도 Postgres 자문 잠금 때문에 실제로 도는 건 하나뿐이다.

워크플로는 사람의 답을 기다리며 꺼져 있다. "마감 시각이 됐다"는 사건이
아니어서 아무도 깨워주지 않으면 영원히 멈춰 있는데, 이 잡이 그 알람이다.
겹쳐 도는 것은 Postgres 자문 잠금으로 막는다.
"""

from __future__ import annotations

import sys

from backend_logic2.services.auto_progress_runner import run_due_auto_progress


def main() -> int:
    counts = run_due_auto_progress()
    print(
        f"[auto_progress_job] 마감 경과 {counts['scanned']}건 "
        f"/ 자동 진행 {counts['advanced']}건 / 조건 미충족 {counts['blocked']}건 "
        f"/ 기록만 {counts['recorded']}건 / 마감 자동연장 {counts['deadline_extended']}건 "
        f"/ 보류 {counts['held']}건 / 다른 인스턴스 {counts['not_mine']}건 "
        f"/ 실패 {counts['failed']}건"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
