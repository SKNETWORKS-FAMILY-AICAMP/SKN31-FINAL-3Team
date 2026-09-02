"""
nodes/rfq/remind_rfq.py

ERPNext RFQ 이메일 자동독촉.

판단 기준
---------
    1. Submit된(docstatus=1) RFQ
    2. 해당 공급사에게 RFQ 메일이 실제로 발송된 이력이 있음
       (RFQ의 Activity에 Communication(sent_or_received="Sent")으로 기록됨)
    3. 최초 발송일 기준으로 "다음날부터" 독촉 대상 (오늘이 최초 발송일 당일이면 skip)
    4. RFQ Activity에 그 공급사가 보낸 Received 메일(답장)이 하나도 없음
    5. 오늘 이미 그 공급사에게 독촉메일을 보낸 적이 없음 (하루 최대 1통)

조건을 모두 만족하면 독촉메일을 발송한다.

회신 마감일
-----------
기본값은 "최초 발송일 + 3일"이지만, 공급사(거래처)마다 마감 기간이 다를 수
있어서 아래 우선순위로 결정한다.
    1) RFQ_REPLY_DEADLINE_OVERRIDES (.env, JSON) 에 그 공급사명이 있으면 그 값
    2) Supplier 문서에 custom_rfq_reply_deadline_days 커스텀 필드가 있으면 그 값
       (ERPNext에 아직 이 필드가 없다면 자연스럽게 건너뛰고 3번으로 감)
    3) DEFAULT_REPLY_DEADLINE_DAYS (.env: RFQ_REPLY_DEADLINE_DAYS, 기본 3일)

마감일은 "언제까지 독촉을 계속 보낼지"를 정하는 데는 쓰지 않는다 — 답장이
없는 한 매일 독촉메일이 계속 나가되, 마감일을 기준으로 문구(어조)만
자동으로 단계별로 바뀐다(build_reminder_email 참고). 답장이 오면 그 즉시
독촉이 멈춘다.

독촉메일 문구
-------------
독촉메일 내용은 매번 똑같이 고정하지 않고 변수로 다룬다.
    - build_reminder_email()에 message_template={"subject": ..., "body": ...}
      을 직접 넘기면 그 내용을 그대로 사용 (호출부에서 매번 다르게 지정 가능)
    - 넘기지 않으면 .env의 RFQ_REMINDER_SUBJECT / RFQ_REMINDER_BODY 를 확인
    - 그것도 없으면, 마감일까지 남은 일수에 따라 3단계(여유/임박/마감경과)로
      자동 escalate되는 기본 문구를 사용

독촉메일도 RFQ의 Communication으로 기록되기 때문에, 회신이 계속 없으면
"오늘 이미 보냈는지"만 확인해서 하루 한 통씩 계속 나가게 된다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
import re
from email.utils import getaddresses

from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    erp_get,
    erp_get_document_email_communications,
    erp_get_one,
    erp_send_email,
)

RFQ_DOCTYPE = "Request for Quotation"

# 최초 발송일로부터 며칠 뒤부터 독촉을 시작할지 ("다음날부터" = 1)
REMINDER_START_OFFSET_DAYS = int(os.getenv("RFQ_REMINDER_START_OFFSET_DAYS", "1"))

# 회신 마감일 기본값 (공급사별 override가 없을 때 사용)
DEFAULT_REPLY_DEADLINE_DAYS = int(os.getenv("RFQ_REPLY_DEADLINE_DAYS", "3"))

REMINDER_SUBJECT_PREFIX = "[재발송][RFQ]"

# 과거 자동 독촉 메일은 ``[자동독촉][RFQ]`` 접두사를 사용했습니다.
# 이미 ERPNext Communication에 쌓인 이력도 독촉 횟수에 포함해야 같은 날
# 중복 발송과 단계 하향을 막을 수 있으므로 두 형식을 모두 인식합니다.
REMINDER_SUBJECT_PREFIXES = (REMINDER_SUBJECT_PREFIX, "[자동독촉][RFQ]")


def _load_deadline_overrides() -> dict:
    """.env의 RFQ_REPLY_DEADLINE_OVERRIDES(JSON 문자열)를 공급사별 마감일 딕셔너리로 로드.

    예: RFQ_REPLY_DEADLINE_OVERRIDES='{"대한안전산업": 5, "한빛보호구": 2}'
    형식이 잘못돼도(빈 값, JSON 파싱 에러 등) 조용히 빈 dict로 fallback해서
    이 파일이 죽는 일은 없게 함 — 마감일 계산은 항상 DEFAULT로도 동작 가능.
    """
    raw = os.getenv("RFQ_REPLY_DEADLINE_OVERRIDES", "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k): int(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


SUPPLIER_REPLY_DEADLINE_OVERRIDES = _load_deadline_overrides()


# ============================================================
# 유틸
# ============================================================

def _parse_datetime(value):
    """ERPNext가 돌려주는 날짜/시간 문자열을 datetime으로 파싱.

    "2026-09-01 11:51:51.123456", "2026-09-01 11:51:51", "2026-09-01" 등
    여러 형식을 다 받아냄. 파싱 실패하면 None (호출부가 알아서 무시하게).
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w\-.]+")


def _extract_emails(value) -> set:
    """"홍길동 <a@b.com>, c@d.com" 같은 문자열에서 이메일 주소만 소문자 set으로 추출.

    email.utils.getaddresses가 표준적인 "Name <addr>" 형식은 잘 처리하지만,
    표시 이름 자체에 @가 섞여 있는 등 비정형 형식은 못 뽑아내는 경우가 있어서
    (실제로 이 프로젝트 테스트 중 발견됨), 정규식 기반 fallback을 같이 돌려서
    보완한다. 두 방식의 결과를 합집합으로 합침 — 과다추출보다 누락이 훨씬
    치명적이기 때문(누락되면 답장을 못 알아채고 계속 독촉메일이 나감).
    """
    if not value:
        return set()
    text = str(value)
    addrs = {addr.strip().lower() for _, addr in getaddresses([text]) if addr}
    addrs |= {m.lower() for m in _EMAIL_RE.findall(text)}
    return addrs


def get_reply_deadline_days(supplier_name, supplier_doc=None) -> int:
    """이 공급사(거래처)에 적용할 회신 마감 기간(일수)을 결정."""
    override = SUPPLIER_REPLY_DEADLINE_OVERRIDES.get(supplier_name)
    if override:
        return int(override)

    if supplier_doc is None:
        try:
            supplier_doc = erp_get_one("Supplier", supplier_name)
        except ERPNextAPIError:
            supplier_doc = None

    if supplier_doc:
        custom_days = supplier_doc.get("custom_rfq_reply_deadline_days")
        if custom_days:
            return int(custom_days)

    return DEFAULT_REPLY_DEADLINE_DAYS


def get_rfq_suppliers(rfq: dict) -> list:
    """RFQ 문서의 suppliers 자식테이블에서 (공급사명, 이메일) 목록만 정리해서 뽑음."""
    rows = []
    for row in rfq.get("suppliers", []) or []:
        supplier = row.get("supplier")
        if not supplier:
            continue
        email = (row.get("email_id") or "").strip().lower() or None
        rows.append({"supplier": supplier, "email": email})
    return rows


def get_supplier_mail_state(communications: list, supplier_email: str) -> dict:
    """communications 목록에서 특정 공급사 이메일과 관련된 발신/수신 상태만 뽑아 요약.

    반환값:
        first_sent_at: 이 공급사에게 최초로 메일을 보낸 시각 (없으면 None)
        last_sent_at: 이 공급사에게 마지막으로 메일을 보낸 시각 (독촉 포함)
        reminder_count: 지금까지 이 공급사에게 보낸 독촉메일 개수(최초 RFQ 발송 제외)
        replied: 공급사가 답장을 한 적이 있는지
        last_received_at: 공급사가 마지막으로 답장한 시각
    """
    supplier_email = (supplier_email or "").strip().lower()

    sent = []
    received = []
    for c in communications:
        dt = _parse_datetime(c.get("communication_date") or c.get("creation"))
        if dt is None:
            continue
        kind = c.get("sent_or_received")
        if kind == "Sent":
            recipients = _extract_emails(c.get("recipients")) | _extract_emails(c.get("cc"))
            if supplier_email in recipients:
                sent.append((dt, c))
        elif kind == "Received":
            senders = _extract_emails(c.get("sender"))
            if supplier_email in senders:
                received.append((dt, c))

    sent.sort(key=lambda pair: pair[0])
    received.sort(key=lambda pair: pair[0])

    first_sent_at = sent[0][0] if sent else None
    last_sent_at = sent[-1][0] if sent else None

    # 첫 번째로 보낸 메일은 "최초 RFQ 발송"으로 보고, 그 이후에 우리가 보낸 것들만 독촉으로 셈.
    reminder_count = 0
    for dt, c in sent[1:]:
        subject = c.get("subject") or ""
        if subject.startswith(REMINDER_SUBJECT_PREFIXES):
            reminder_count += 1

    return {
        "first_sent_at": first_sent_at,
        "last_sent_at": last_sent_at,
        "reminder_count": reminder_count,
        "replied": bool(received),
        "last_received_at": received[-1][0] if received else None,
    }


# ============================================================
# 독촉메일 문구
# ============================================================

# 마감일까지 남은 일수(days_left)에 따라 자동으로 골라지는 기본 3단계 문구.
# days_left > 1        -> 0단계(여유): 정중한 리마인드
# 0 <= days_left <= 1  -> 1단계(임박): 마감이 다가온다는 알림
# days_left < 0        -> 2단계(경과): 마감이 지났다는 알림(최종 안내 톤)
REMINDER_STAGE_TEMPLATES = [
    {
        "subject": "{prefix} {rfq_name} 견적 회신 부탁드립니다",
        "body": (
            "안녕하세요, {supplier_name}님.<br><br>"
            "{sent_date}에 보내드린 견적요청(RFQ) {rfq_name} 건에 대해 "
            "아직 회신을 받지 못하여 다시 한번 안내드립니다.<br>"
            "회신 마감일은 {deadline_date}입니다. 확인 부탁드립니다.<br><br>"
            "감사합니다."
        ),
    },
    {
        "subject": "{prefix} {rfq_name} 회신 마감일이 얼마 남지 않았습니다",
        "body": (
            "안녕하세요, {supplier_name}님.<br><br>"
            "견적요청(RFQ) {rfq_name}의 회신 마감일이 {deadline_date}로 "
            "얼마 남지 않았습니다. 기한 내 회신 부탁드립니다.<br><br>"
            "감사합니다."
        ),
    },
    {
        "subject": "{prefix} {rfq_name} 회신 마감일이 지났습니다",
        "body": (
            "안녕하세요, {supplier_name}님.<br><br>"
            "견적요청(RFQ) {rfq_name}의 회신 마감일({deadline_date})이 지났으나 "
            "아직 회신을 받지 못했습니다. 빠른 회신 부탁드리며, 회신이 어려우신 "
            "경우 담당자에게 연락 부탁드립니다.<br><br>"
            "감사합니다."
        ),
    },
]


def _select_stage(days_left: int) -> int:
    if days_left > 1:
        return 0
    if days_left >= 0:
        return 1
    return 2


def _env_template_override():
    """.env로 넘겨준 독촉메일 문구가 있으면 최우선으로 사용 (코드 수정 없이 문구 교체용)."""
    subject = os.getenv("RFQ_REMINDER_SUBJECT")
    body = os.getenv("RFQ_REMINDER_BODY")
    if subject and body:
        return {"subject": subject, "body": body}
    return None


def build_reminder_email(
    rfq: dict,
    supplier: dict,
    *,
    now: datetime,
    deadline_date: datetime,
    reminder_count: int,
    message_template: dict | None = None,
):
    """독촉메일의 (subject, content)를 만들어서 반환.

    message_template 우선순위:
        1) 인자로 직접 넘긴 message_template (호출부에서 매번 다르게 지정 가능)
        2) .env의 RFQ_REMINDER_SUBJECT / RFQ_REMINDER_BODY
        3) 마감일까지 남은 기간에 따라 자동 선택되는 기본 3단계 문구
    """
    days_left = (deadline_date.date() - now.date()).days
    template = message_template or _env_template_override() or REMINDER_STAGE_TEMPLATES[_select_stage(days_left)]

    context = {
        "prefix": REMINDER_SUBJECT_PREFIX,
        "rfq_name": rfq.get("name"),
        "supplier_name": supplier.get("supplier"),
        "sent_date": now.strftime("%Y-%m-%d"),
        "deadline_date": deadline_date.strftime("%Y-%m-%d"),
        "days_left": days_left,
        "reminder_count": reminder_count + 1,
    }

    subject = template["subject"].format(**context)
    body = template["body"].format(**context)
    return subject, body


# ============================================================
# 메인 로직
# ============================================================

def process_rfq_reminder(rfq: dict, now: datetime | None = None) -> list:
    """RFQ 문서 하나에 대해, 대상 공급사별로 독촉이 필요한지 판단하고 필요하면 발송.

    반환값: 공급사별 처리 결과 리스트. 각 항목의 "action"은 다음 중 하나:
        sent, skipped_no_email, skipped_not_sent_yet, skipped_replied,
        skipped_not_due, skipped_already_reminded_today, error
    """
    now = now or datetime.now()
    rfq_name = rfq.get("name")
    results = []

    if int(rfq.get("docstatus") or 0) != 1:
        return results

    suppliers = get_rfq_suppliers(rfq)
    if not suppliers:
        return results

    try:
        communications = erp_get_document_email_communications(RFQ_DOCTYPE, rfq_name)
    except ERPNextAPIError as e:
        return [{"rfq": rfq_name, "supplier": None, "email": None, "action": "error", "detail": str(e)}]

    for supplier in suppliers:
        supplier_name = supplier["supplier"]
        email = supplier["email"]
        entry = {"rfq": rfq_name, "supplier": supplier_name, "email": email}

        if not email:
            entry["action"] = "skipped_no_email"
            results.append(entry)
            continue

        state = get_supplier_mail_state(communications, email)

        if not state["first_sent_at"]:
            entry["action"] = "skipped_not_sent_yet"
            results.append(entry)
            continue

        if state["replied"]:
            entry["action"] = "skipped_replied"
            entry["replied_at"] = state["last_received_at"]
            results.append(entry)
            continue

        # "다음날부터"는 정확히 24시간 경과가 아니라 달력 날짜 기준으로 판단.
        # 예: 9/1 23:50에 보냈어도 9/2가 되면(단 몇 분만 지나도) 독촉 대상.
        reminder_start_date = state["first_sent_at"].date() + timedelta(days=REMINDER_START_OFFSET_DAYS)
        if now.date() < reminder_start_date:
            entry["action"] = "skipped_not_due"
            entry["reminder_start_date"] = reminder_start_date
            results.append(entry)
            continue

        if state["last_sent_at"] and state["last_sent_at"].date() == now.date():
            entry["action"] = "skipped_already_reminded_today"
            results.append(entry)
            continue

        deadline_days = get_reply_deadline_days(supplier_name)
        deadline_date = state["first_sent_at"] + timedelta(days=deadline_days)

        subject, content = build_reminder_email(
            rfq,
            supplier,
            now=now,
            deadline_date=deadline_date,
            reminder_count=state["reminder_count"],
        )

        try:
            erp_send_email(
                doctype=RFQ_DOCTYPE,
                name=rfq_name,
                recipients=email,
                subject=subject,
                content=content,
            )
            entry["action"] = "sent"
            entry["reminder_count"] = state["reminder_count"] + 1
            entry["deadline_date"] = deadline_date
            entry["days_since_sent"] = (now - state["first_sent_at"]).days
        except ERPNextAPIError as e:
            entry["action"] = "error"
            entry["detail"] = str(e)

        results.append(entry)

    return results


def run_due_rfq_reminders(now: datetime | None = None) -> list:
    """Submit된 RFQ를 전부 훑어서, 독촉이 필요한 공급사에게 독촉메일을 보낸다.

    scheduler(jobs/rfq_reminder_job.py)가 매일 한 번씩 이 함수를 호출하는 것을 전제로 함.
    """
    now = now or datetime.now()

    rfqs = erp_get(RFQ_DOCTYPE, filters=[["docstatus", "=", 1]], fields=["name"]) or []

    all_results = []
    for row in rfqs:
        rfq_name = row["name"]
        try:
            rfq = erp_get_one(RFQ_DOCTYPE, rfq_name)
        except ERPNextAPIError as e:
            all_results.append({"rfq": rfq_name, "supplier": None, "email": None, "action": "error", "detail": str(e)})
            continue
        if not rfq:
            continue
        all_results.extend(process_rfq_reminder(rfq, now=now))

    return all_results


if __name__ == "__main__":
    summary = run_due_rfq_reminders()
    for row in summary:
        print(row)
    sent = [r for r in summary if r.get("action") == "sent"]
    print(f"\n총 {len(summary)}건 처리 / 독촉메일 발송 {len(sent)}건")
