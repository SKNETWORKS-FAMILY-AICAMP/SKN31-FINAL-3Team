"""Scheduler entry point for unanswered supplier PR reminders."""

from __future__ import annotations

import sys

from backend_logic2.pr.reminder_service import run_due_pr_reminders


def main() -> int:
    summary = run_due_pr_reminders()
    results = summary["results"]
    sent = [row for row in results if row.get("action") == "sent"]
    errors = [row for row in results if row.get("action") == "error"]
    print(
        f"[pr_reminder_job] 대상 {len(results)}건 / 발송 {len(sent)}건 "
        f"/ 만료 {summary['expired_count']}건 / 에러 {len(errors)}건"
    )
    for row in sent:
        print(
            f"  - 발송: {row['pr_id']} -> {row.get('supplier')}({row.get('email')}), "
            f"{row.get('reminder_count')}회차"
        )
    for row in errors:
        print(f"  - 에러: {row.get('pr_id')}: {row.get('detail')}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
