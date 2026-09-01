"""
jobs/rfq_reminder_job.py

RFQ 자동독촉 Scheduler Entry Point.

외부 Scheduler(cron, Windows 작업 스케줄러, ERPNext Scheduled Job 등)에서는
이 파일만 실행하면 된다. 하루에 한 번(예: 매일 오전 9시) 실행되도록 등록할 것.

실행: python -m backend_logic2.jobs.rfq_reminder_job
      (backend_logic2의 상위 폴더에서 실행)
"""

import sys

from backend_logic2.nodes.rfq.remind_rfq import run_due_rfq_reminders


def main() -> int:
    results = run_due_rfq_reminders()

    sent = [r for r in results if r.get("action") == "sent"]
    replied = [r for r in results if r.get("action") == "skipped_replied"]
    errors = [r for r in results if r.get("action") == "error"]

    print(
        f"[rfq_reminder_job] 총 {len(results)}건 처리 "
        f"/ 독촉메일 발송 {len(sent)}건 "
        f"/ 회신완료 스킵 {len(replied)}건 "
        f"/ 에러 {len(errors)}건"
    )

    for r in sent:
        print(
            f"  - 발송: {r['rfq']} -> {r['supplier']}({r['email']}), "
            f"{r.get('reminder_count')}회차 독촉, 마감일 {r.get('deadline_date')}"
        )
    for r in errors:
        print(f"  - 에러: {r.get('rfq')} / {r.get('supplier')}: {r.get('detail')}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
