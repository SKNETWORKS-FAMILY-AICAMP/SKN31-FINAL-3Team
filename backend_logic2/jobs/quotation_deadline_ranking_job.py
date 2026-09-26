"""견적 마감이 지난 케이스의 순위를 확정 계산하고 담당자에게 알리는 스케줄 잡.

실행: python -m backend_logic2.jobs.quotation_deadline_ranking_job
(systemd timer 예시: deploy/systemd/biddingflow-quotation-ranking.timer.example)

그래프를 최종 선정 단계로 자동으로 넘기지는 않는다 - 견적 Submit과 재비딩
가능 여부가 걸려 있어서 최종 선정은 여전히 담당자가 시작한다. 여기서는
순위를 미리 계산해 두고(규격 평가 캐시 포함) "순위가 준비됐다"는 알림을
차수별로 한 번만 보낸다.
"""

from __future__ import annotations

import sys

from backend_logic2.services.quotation_service import refresh_due_live_rankings


def main() -> int:
    counts = refresh_due_live_rankings()
    print(
        f"[quotation_deadline_ranking_job] 마감 지난 케이스 {counts['checked']}건 "
        f"/ 순위 갱신 {counts['refreshed']}건 / 실패 {counts['failed']}건"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
