"""Scheduled reminders for unanswered supplier purchase responses."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from backend_logic2.integrations.erp_client import erp_send_email
from backend_logic2.policies.reminder_schedule import reminder_window_open

from . import repository
from .email_template import render_reminder_email


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def run_due_pr_reminders(now: datetime | None = None) -> dict[str, Any]:
    """Expire stale PRs and send reminders for live unanswered requests."""
    now = now or datetime.now(timezone.utc)
    start_after_hours = _positive_env_int("PR_REMINDER_START_HOURS", 24)
    interval_hours = _positive_env_int("PR_REMINDER_INTERVAL_HOURS", 24)
    expired_count = repository.expire_due_requests(now)
    if not reminder_window_open(now):
        return {"expired_count": expired_count, "results": [], "schedule_waiting": True}
    due = repository.list_due_reminders(
        now,
        start_after_hours=start_after_hours,
        interval_hours=interval_hours,
    )
    results: list[dict[str, Any]] = []

    for candidate in due:
        pr_id = str(candidate["pr_id"])
        claimed = repository.claim_reminder(
            pr_id,
            now,
            start_after_hours=start_after_hours,
            interval_hours=interval_hours,
        )
        if not claimed:
            results.append({"pr_id": pr_id, "action": "skipped_not_due_or_claimed"})
            continue

        recipient = os.getenv("PR_EMAIL_OVERRIDE", "").strip() or str(
            claimed.get("supplier_email") or ""
        ).strip()
        if "@" not in recipient:
            error = "유효한 공급사 이메일이 없습니다."
            repository.mark_reminder_failed(pr_id, claimed_at=now, error=error)
            results.append({"pr_id": pr_id, "action": "error", "detail": error})
            continue

        subject, content = render_reminder_email(claimed, now=now)
        if recipient != str(claimed.get("supplier_email") or "").strip():
            subject = f"[TEST] {subject}"
        try:
            delivery = erp_send_email(
                "Material Request",
                str(claimed["mr_name"]),
                [recipient],
                subject,
                content,
            )
            if not delivery.get("email_sent"):
                error = "수신자가 현재 메일 허용목록에 없어 발송하지 않았습니다."
                repository.mark_reminder_failed(pr_id, claimed_at=now, error=error)
                results.append(
                    {
                        "pr_id": pr_id,
                        "email": recipient,
                        "action": "blocked_by_email_policy",
                        "detail": error,
                        "email_policy": delivery.get("email_policy"),
                    }
                )
                continue
            updated = repository.mark_reminder_sent(pr_id, claimed_at=now)
            results.append(
                {
                    "pr_id": pr_id,
                    "supplier": claimed.get("supplier_id"),
                    "email": recipient,
                    "action": "sent",
                    "reminder_count": updated.get("reminder_count"),
                }
            )
        except Exception as exc:
            repository.mark_reminder_failed(pr_id, claimed_at=now, error=str(exc))
            results.append(
                {"pr_id": pr_id, "action": "error", "detail": str(exc)}
            )

    return {"expired_count": expired_count, "results": results}
