"""
nodes/rfq/test_remind_rfq.py

remind_rfq.py 단위 테스트. ERPNext에는 실제로 접속하지 않고, erp_client의
함수들(erp_get, erp_get_one, erp_get_document_email_communications,
erp_send_email)을 전부 mock으로 대체해서 로직만 검증한다.

실행:
    backend_logic2의 "상위" 폴더(예: SKN31-FINAL-3Team/)에서
        python -m unittest backend_logic2.nodes.rfq.test_remind_rfq -v
    또는
        python -m pytest backend_logic2/nodes/rfq/test_remind_rfq.py -v
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from backend_logic2.integrations.erp_client import ERPNextAPIError
from backend_logic2.nodes.rfq import remind_rfq as rr


def make_rfq(name="RFQ-0001", docstatus=1, suppliers=None):
    return {
        "name": name,
        "docstatus": docstatus,
        "suppliers": suppliers if suppliers is not None else [
            {"supplier": "대한안전산업", "email_id": "buyer@daehan.example.com"},
        ],
    }


def sent_comm(dt: str, recipients: str, subject="Request for Quotation: RFQ-0001"):
    return {
        "sent_or_received": "Sent",
        "sender": "us@ourcompany.example.com",
        "recipients": recipients,
        "cc": "",
        "subject": subject,
        "creation": dt,
        "communication_date": dt,
    }


def received_comm(dt: str, sender: str, subject="RE: Request for Quotation"):
    return {
        "sent_or_received": "Received",
        "sender": sender,
        "recipients": "us@ourcompany.example.com",
        "cc": "",
        "subject": subject,
        "creation": dt,
        "communication_date": dt,
    }


class GetSupplierMailStateTest(unittest.TestCase):
    def test_no_communications_means_never_sent(self):
        state = rr.get_supplier_mail_state([], "buyer@daehan.example.com")
        self.assertIsNone(state["first_sent_at"])
        self.assertFalse(state["replied"])

    def test_first_and_last_sent_and_reminder_count(self):
        comms = [
            sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com", subject="Request for Quotation: RFQ-0001"),
            sent_comm("2026-09-02 09:00:00", "buyer@daehan.example.com", subject="[자동독촉][RFQ] RFQ-0001 견적 회신 부탁드립니다"),
            sent_comm("2026-09-03 09:00:00", "buyer@daehan.example.com", subject="[자동독촉][RFQ] RFQ-0001 회신 마감일이 얼마 남지 않았습니다"),
        ]
        state = rr.get_supplier_mail_state(comms, "buyer@daehan.example.com")
        self.assertEqual(state["first_sent_at"], datetime(2026, 9, 1, 11, 0, 0))
        self.assertEqual(state["last_sent_at"], datetime(2026, 9, 3, 9, 0, 0))
        self.assertEqual(state["reminder_count"], 2)
        self.assertFalse(state["replied"])

    def test_reply_detected_by_sender_address(self):
        comms = [
            sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com"),
            received_comm("2026-09-02 10:00:00", "kcmini03@naver.com <buyer@daehan.example.com>"),
        ]
        state = rr.get_supplier_mail_state(comms, "buyer@daehan.example.com")
        self.assertTrue(state["replied"])
        self.assertEqual(state["last_received_at"], datetime(2026, 9, 2, 10, 0, 0))

    def test_other_suppliers_communications_are_ignored(self):
        comms = [
            sent_comm("2026-09-01 11:00:00", "other-supplier@example.com"),
            received_comm("2026-09-02 10:00:00", "other-supplier@example.com"),
        ]
        state = rr.get_supplier_mail_state(comms, "buyer@daehan.example.com")
        self.assertIsNone(state["first_sent_at"])
        self.assertFalse(state["replied"])


class ReplyDeadlineTest(unittest.TestCase):
    def test_default_deadline_when_no_override(self):
        with mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {}):
            with mock.patch.object(rr, "erp_get_one", return_value=None):
                days = rr.get_reply_deadline_days("아무개공급사")
        self.assertEqual(days, rr.DEFAULT_REPLY_DEADLINE_DAYS)

    def test_env_override_takes_priority(self):
        with mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {"대한안전산업": 5}):
            days = rr.get_reply_deadline_days("대한안전산업")
        self.assertEqual(days, 5)

    def test_supplier_custom_field_used_when_no_env_override(self):
        with mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {}):
            supplier_doc = {"custom_rfq_reply_deadline_days": 7}
            days = rr.get_reply_deadline_days("한빛보호구", supplier_doc=supplier_doc)
        self.assertEqual(days, 7)


class BuildReminderEmailTest(unittest.TestCase):
    def setUp(self):
        self.rfq = make_rfq()
        self.supplier = {"supplier": "대한안전산업", "email": "buyer@daehan.example.com"}

    def test_stage0_when_deadline_far_away(self):
        now = datetime(2026, 9, 2, 9, 0, 0)
        deadline = datetime(2026, 9, 5)  # 3 days left
        subject, body = rr.build_reminder_email(
            self.rfq, self.supplier, now=now, deadline_date=deadline, reminder_count=0
        )
        self.assertIn(rr.REMINDER_SUBJECT_PREFIX, subject)
        self.assertIn("RFQ-0001", subject)
        self.assertIn("2026-09-05", body)

    def test_stage2_when_deadline_passed(self):
        now = datetime(2026, 9, 6, 9, 0, 0)
        deadline = datetime(2026, 9, 5)  # -1 day left (overdue)
        subject, body = rr.build_reminder_email(
            self.rfq, self.supplier, now=now, deadline_date=deadline, reminder_count=2
        )
        self.assertIn("지났습니다", subject)

    def test_explicit_template_overrides_default(self):
        now = datetime(2026, 9, 2, 9, 0, 0)
        deadline = datetime(2026, 9, 5)
        template = {"subject": "커스텀 제목 {rfq_name}", "body": "커스텀 본문 {supplier_name}"}
        subject, body = rr.build_reminder_email(
            self.rfq, self.supplier, now=now, deadline_date=deadline,
            reminder_count=0, message_template=template,
        )
        self.assertEqual(subject, "커스텀 제목 RFQ-0001")
        self.assertEqual(body, "커스텀 본문 대한안전산업")

    def test_env_template_overrides_default_when_no_explicit_template(self):
        now = datetime(2026, 9, 2, 9, 0, 0)
        deadline = datetime(2026, 9, 5)
        with mock.patch.dict(
            "os.environ",
            {"RFQ_REMINDER_SUBJECT": "환경변수 제목", "RFQ_REMINDER_BODY": "환경변수 본문 {supplier_name}"},
        ):
            subject, body = rr.build_reminder_email(
                self.rfq, self.supplier, now=now, deadline_date=deadline, reminder_count=0
            )
        self.assertEqual(subject, "환경변수 제목")
        self.assertEqual(body, "환경변수 본문 대한안전산업")


class ProcessRfqReminderTest(unittest.TestCase):
    def setUp(self):
        self.rfq = make_rfq()
        self.now = datetime(2026, 9, 2, 9, 0, 0)  # RFQ 최초 발송 다음날

    def _run(self, communications, now=None):
        with mock.patch.object(rr, "erp_get_document_email_communications", return_value=communications), \
             mock.patch.object(rr, "erp_send_email") as mock_send, \
             mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {}), \
             mock.patch.object(rr, "erp_get_one", return_value=None):
            results = rr.process_rfq_reminder(self.rfq, now=now or self.now)
        return results, mock_send

    def test_no_reminder_on_the_same_day_as_send(self):
        comms = [sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com")]
        same_day = datetime(2026, 9, 1, 15, 0, 0)
        results, mock_send = self._run(comms, now=same_day)
        self.assertEqual(results[0]["action"], "skipped_not_due")
        mock_send.assert_not_called()

    def test_reminder_sent_the_day_after(self):
        comms = [sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com")]
        results, mock_send = self._run(comms)
        self.assertEqual(results[0]["action"], "sent")
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["doctype"], rr.RFQ_DOCTYPE)
        self.assertEqual(kwargs["recipients"], "buyer@daehan.example.com")
        self.assertIn(rr.REMINDER_SUBJECT_PREFIX, kwargs["subject"])

    def test_no_reminder_when_supplier_already_replied(self):
        comms = [
            sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com"),
            received_comm("2026-09-01 18:00:00", "buyer@daehan.example.com"),
        ]
        results, mock_send = self._run(comms)
        self.assertEqual(results[0]["action"], "skipped_replied")
        mock_send.assert_not_called()

    def test_no_duplicate_reminder_same_day(self):
        comms = [
            sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com"),
            sent_comm(
                "2026-09-02 08:00:00", "buyer@daehan.example.com",
                subject="[자동독촉][RFQ] RFQ-0001 견적 회신 부탁드립니다",
            ),
        ]
        results, mock_send = self._run(comms)
        self.assertEqual(results[0]["action"], "skipped_already_reminded_today")
        mock_send.assert_not_called()

    def test_no_email_means_skipped(self):
        rfq = make_rfq(suppliers=[{"supplier": "이메일없는공급사", "email_id": ""}])
        with mock.patch.object(rr, "erp_get_document_email_communications", return_value=[]), \
             mock.patch.object(rr, "erp_send_email") as mock_send:
            results = rr.process_rfq_reminder(rfq, now=self.now)
        self.assertEqual(results[0]["action"], "skipped_no_email")
        mock_send.assert_not_called()

    def test_draft_rfq_is_ignored(self):
        draft_rfq = make_rfq(docstatus=0)
        with mock.patch.object(rr, "erp_get_document_email_communications") as mock_comm, \
             mock.patch.object(rr, "erp_send_email") as mock_send:
            results = rr.process_rfq_reminder(draft_rfq, now=self.now)
        self.assertEqual(results, [])
        mock_comm.assert_not_called()
        mock_send.assert_not_called()

    def test_multiple_suppliers_handled_independently(self):
        rfq = make_rfq(suppliers=[
            {"supplier": "대한안전산업", "email_id": "buyer@daehan.example.com"},
            {"supplier": "한빛보호구", "email_id": "buyer@hanbit.example.com"},
        ])
        comms = [
            sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com"),
            sent_comm("2026-09-01 11:00:00", "buyer@hanbit.example.com"),
            received_comm("2026-09-01 15:00:00", "buyer@hanbit.example.com"),
        ]
        with mock.patch.object(rr, "erp_get_document_email_communications", return_value=comms), \
             mock.patch.object(rr, "erp_send_email") as mock_send, \
             mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {}), \
             mock.patch.object(rr, "erp_get_one", return_value=None):
            results = rr.process_rfq_reminder(rfq, now=self.now)

        results_by_supplier = {r["supplier"]: r for r in results}
        self.assertEqual(results_by_supplier["대한안전산업"]["action"], "sent")
        self.assertEqual(results_by_supplier["한빛보호구"]["action"], "skipped_replied")
        mock_send.assert_called_once()  # 답장한 공급사에게는 안 나감

    def test_erp_send_email_error_is_captured_not_raised(self):
        comms = [sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com")]
        with mock.patch.object(rr, "erp_get_document_email_communications", return_value=comms), \
             mock.patch.object(rr, "erp_send_email", side_effect=ERPNextAPIError("SMTP 오류")), \
             mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {}), \
             mock.patch.object(rr, "erp_get_one", return_value=None):
            results = rr.process_rfq_reminder(self.rfq, now=self.now)
        self.assertEqual(results[0]["action"], "error")
        self.assertIn("SMTP 오류", results[0]["detail"])


class RunDueRfqRemindersTest(unittest.TestCase):
    def test_iterates_all_submitted_rfqs(self):
        rfq1 = make_rfq(name="RFQ-0001")
        rfq2 = make_rfq(
            name="RFQ-0002",
            suppliers=[{"supplier": "한빛보호구", "email_id": "buyer@hanbit.example.com"}],
        )
        now = datetime(2026, 9, 2, 9, 0, 0)

        def fake_get_one(doctype, name):
            if doctype == "Supplier":
                return None
            return {"RFQ-0001": rfq1, "RFQ-0002": rfq2}[name]

        comms_by_rfq = {
            "RFQ-0001": [sent_comm("2026-09-01 11:00:00", "buyer@daehan.example.com")],
            "RFQ-0002": [sent_comm("2026-09-01 11:00:00", "buyer@hanbit.example.com")],
        }

        def fake_get_comms(doctype, name):
            return comms_by_rfq[name]

        with mock.patch.object(rr, "erp_get", return_value=[{"name": "RFQ-0001"}, {"name": "RFQ-0002"}]), \
             mock.patch.object(rr, "erp_get_one", side_effect=fake_get_one), \
             mock.patch.object(rr, "erp_get_document_email_communications", side_effect=fake_get_comms), \
             mock.patch.object(rr, "erp_send_email") as mock_send, \
             mock.patch.object(rr, "SUPPLIER_REPLY_DEADLINE_OVERRIDES", {}):
            results = rr.run_due_rfq_reminders(now=now)

        rfq_names = {r["rfq"] for r in results}
        self.assertEqual(rfq_names, {"RFQ-0001", "RFQ-0002"})
        self.assertEqual(mock_send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
