"""
nexterp 자동화 - ERPNext API 클라이언트

연결 설정(SITE_URL, API_KEY, API_SECRET)은 .env에서 가져옴.
설정값 바꾸고 싶으면 .env 파일을 수정할 것 (config.py는 더 이상 안 씀).

⚠️ 이 파일에는 ERPNext REST API를 감싼 "진짜 원초적인 공통 함수"만 있음
(GET/POST/SUBMIT/이메일발송). 재고조회·대체품·MR·RFQ 같은 도메인 로직은
여기 안 두고, 각자 모듈 파일에서 이 함수들을 가져다 써서 구현할 것.
RFQ 발송 이후 단계(견적비교·PO·Invoice)는 아직 구현 안 함 — 지금은 MVP
단계라 여기까지만.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

SITE_URL = os.environ["SITE_URL"]
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_SECRET"]

HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Content-Type": "application/json",
}


class ERPNextAPIError(Exception):
    """ERPNext API 호출이 실패했을 때 던지는 예외. 조용히 None 반환하지 않고
    명확하게 실패를 알려서, 호출한 쪽(테스트 스크립트 등)이 정확히 감지하게 함."""
    pass


def erp_get(doctype, filters=None, fields=None, order_by=None, limit=None):
    """ERPNext에서 문서 목록 조회. order_by 예: 'creation desc', limit은 최대 건수"""
    import json
    params = {}
    if filters:
        params["filters"] = json.dumps(filters)
    if fields:
        params["fields"] = json.dumps(fields)
    if order_by:
        params["order_by"] = order_by
    if limit:
        params["limit_page_length"] = limit

    res = requests.get(f"{SITE_URL}/api/resource/{doctype}", headers=HEADERS, params=params)
    if res.status_code != 200:
        raise ERPNextAPIError(f"GET {doctype}: {res.status_code} - {res.text[:300]}")
    return res.json().get("data")


def erp_get_one(doctype, name):
    """ERPNext에서 문서 하나를 ID로 직접 조회 (자식테이블까지 다 포함해서 가져옴)"""
    res = requests.get(f"{SITE_URL}/api/resource/{doctype}/{name}", headers=HEADERS)
    if res.status_code != 200:
        raise ERPNextAPIError(f"GET {doctype}/{name}: {res.status_code} - {res.text[:300]}")
    return res.json().get("data")


def erp_post(doctype, payload):
    """ERPNext에 새 문서 생성 (Draft 상태로 생성됨)"""
    payload = dict(payload)
    payload["doctype"] = doctype
    res = requests.post(f"{SITE_URL}/api/resource/{doctype}", headers=HEADERS, json=payload)
    if res.status_code not in (200, 201):
        raise ERPNextAPIError(f"POST {doctype}: {res.status_code} - {res.text[:500]}")
    return res.json().get("data")


def erp_submit(doctype, name, max_retries=3):
    """
    Draft 문서를 Submit(확정).

    DB 데드락(QueryDeadlockError)이 뜨면 잠깐 대기 후 재시도함 — 백그라운드
    작업(Contact 생성 등)이 아직 끝나기 전에 Submit이 몰리면 간헐적으로
    발생하는 문제라(실제로 겪음), 몇 번 재시도하면 대부분 해결됨.
    다른 종류의 에러는 재시도 안 하고 바로 실패 처리함.
    """
    import time

    for attempt in range(max_retries):
        res = requests.put(
            f"{SITE_URL}/api/resource/{doctype}/{name}",
            headers=HEADERS,
            json={"docstatus": 1},
        )
        if res.status_code == 200:
            return res.json().get("data")

        is_deadlock = "QueryDeadlockError" in res.text
        if is_deadlock and attempt < max_retries - 1:
            wait = 2 * (attempt + 1)
            print(f"[erp_submit] DB 충돌 감지 ({doctype}/{name}), {wait}초 대기 후 재시도 ({attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue

        raise ERPNextAPIError(f"SUBMIT {doctype}/{name}: {res.status_code} - {res.text[:500]}")


def erp_send_email(doctype, name, recipients, subject, content):
    """
    문서를 이메일로 발송.

    .env의 TEST_MODE=true면 실제 발송 대신 콘솔에 내용만 출력함 — 이게
    없어서 그동안 실제 이메일이 나가버린 적도 있었고, 비밀번호 같은
    내용을 확인할 방법도 없었음. 이제 이걸로 둘 다 해결됨.
    """
    # ⚠️ 기본값을 "true"(=발송 생략)로 둠 — .env 로딩 실패시 안전한 쪽으로 fallback
    if os.getenv("TEST_MODE", "true").lower() != "false":
        print(f"\n[TEST_MODE] 실제 발송 생략 — {doctype}/{name}")
        print(f"  수신자: {recipients}")
        print(f"  제목: {subject}")
        print(f"  내용: {content}\n")
        return {"test_mode": True, "would_send_to": recipients}

    payload = {
        "doctype": doctype,
        "name": name,
        "recipients": recipients,
        "subject": subject,
        "content": content,
        "send_email": 1,
        "attach_document_print": 1, 
        "print_format": "Standard"
    }
    res = requests.post(
        f"{SITE_URL}/api/method/frappe.core.doctype.communication.email.make",
        headers=HEADERS,
        json=payload,
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"EMAIL: {res.status_code} - {res.text[:500]}")
    return res.json().get("message")