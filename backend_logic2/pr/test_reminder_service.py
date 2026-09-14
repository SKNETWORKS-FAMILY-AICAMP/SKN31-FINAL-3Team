from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from backend_logic2.pr import reminder_service
from backend_logic2.pr.email_template import render_reminder_email


NOW = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)


def make_pr(**overrides):
    row = {
        "pr_id": "11111111-1111-1111-1111-111111111111",
        "mr_name": "MAT-MR-2026-00001",
        "supplier_id": "대한안전산업",
        "supplier_email": "buyer@example.com",
        "status": "SENT",
        "sent_at": NOW - timedelta(hours=25),
        "expires_at": NOW + timedelta(hours=47),
        "reminder_count": 0,
    }
    row.update(overrides)
    return row


class ReminderTemplateTest(unittest.TestCase):
    def test_does_not_expose_or_expect_response_token(self):
        subject, body = render_reminder_email(make_pr(), now=NOW)
        self.assertIn("[재안내][PR:", subject)
        self.assertIn("최초 PR 안내 메일", body)
        self.assertNotIn("/api/public/pr/respond/", body)


class ReminderServiceTest(unittest.TestCase):
    @mock.patch.object(reminder_service.repository, "mark_reminder_sent")
    @mock.patch.object(reminder_service.repository, "claim_reminder")
    @mock.patch.object(reminder_service.repository, "list_due_reminders")
    @mock.patch.object(reminder_service.repository, "expire_due_requests")
    @mock.patch.object(reminder_service, "erp_send_email")
    def test_sends_and_records_due_pr(
        self, send_email, expire, list_due, claim, mark_sent
    ):
        pr = make_pr()
        expire.return_value = 2
        list_due.return_value = [pr]
        claim.return_value = pr
        mark_sent.return_value = {**pr, "reminder_count": 1}

        summary = reminder_service.run_due_pr_reminders(now=NOW)

        self.assertEqual(summary["expired_count"], 2)
        self.assertEqual(summary["results"][0]["action"], "sent")
        send_email.assert_called_once()
        mark_sent.assert_called_once_with(pr["pr_id"], claimed_at=NOW)

    @mock.patch.object(reminder_service.repository, "mark_reminder_failed")
    @mock.patch.object(reminder_service.repository, "claim_reminder")
    @mock.patch.object(reminder_service.repository, "list_due_reminders")
    @mock.patch.object(reminder_service.repository, "expire_due_requests")
    @mock.patch.object(reminder_service, "erp_send_email")
    def test_failed_email_releases_claim(
        self, send_email, expire, list_due, claim, mark_failed
    ):
        pr = make_pr()
        expire.return_value = 0
        list_due.return_value = [pr]
        claim.return_value = pr
        send_email.side_effect = RuntimeError("SMTP 오류")

        summary = reminder_service.run_due_pr_reminders(now=NOW)

        self.assertEqual(summary["results"][0]["action"], "error")
        mark_failed.assert_called_once_with(
            pr["pr_id"], claimed_at=NOW, error="SMTP 오류"
        )
        mark_sent = getattr(reminder_service.repository, "mark_reminder_sent")
        self.assertIsNotNone(mark_sent)

    @mock.patch.object(reminder_service.repository, "claim_reminder")
    @mock.patch.object(reminder_service.repository, "list_due_reminders")
    @mock.patch.object(reminder_service.repository, "expire_due_requests")
    @mock.patch.object(reminder_service, "erp_send_email")
    def test_parallel_worker_claim_prevents_duplicate(
        self, send_email, expire, list_due, claim
    ):
        pr = make_pr()
        expire.return_value = 0
        list_due.return_value = [pr]
        claim.return_value = None

        summary = reminder_service.run_due_pr_reminders(now=NOW)

        self.assertEqual(
            summary["results"][0]["action"], "skipped_not_due_or_claimed"
        )
        send_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
