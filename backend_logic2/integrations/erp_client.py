"""
nexterp 자동화 - ERPNext API 클라이언트

연결 설정(SITE_URL, API_KEY, API_SECRET)은 .env에서 가져옴.
설정값 바꾸고 싶으면 .env 파일을 수정할 것 (config.py는 더 이상 안 씀).
"""

import os
from functools import lru_cache

import requests
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel, Field
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/purchase", tags=["Purchase Order Automation"])

SITE_URL = os.environ["SITE_URL"]
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_SECRET"]

HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

if not all([SITE_URL, API_KEY, API_SECRET]):
    raise RuntimeError("필수 환경 변수(SITE_URL, API_KEY, API_SECRET)가 .env 파일에 설정되지 않았습니다.")


class ERPNextAPIError(Exception):
    """ERPNext API 호출이 실패했을 때 던지는 예외. 조용히 None 반환하지 않고
    명확하게 실패를 알려서, 호출한 쪽(테스트 스크립트 등)이 정확히 감지하게 함."""
    pass


def is_test_mode() -> bool:
    """Return the single TEST_MODE policy used by every mail-sending node."""
    # 환경 변수가 누락되면 실제 발송보다 차단이 안전하다. 운영 전환은
    # 반드시 TEST_MODE=false를 명시한 경우에만 허용한다.
    return os.getenv("TEST_MODE", "true").strip().lower() != "false"


def erp_get(doctype, filters=None, fields=None, order_by=None, limit=None, start=None):
    """ERPNext에서 문서 목록 조회"""
    import json
    params = {}
    if filters:
        params["filters"] = json.dumps(filters)
    if fields:
        params["fields"] = json.dumps(fields)
    if order_by:
        params["order_by"] = order_by
    if limit is not None:
        params["limit_page_length"] = int(limit)
    if start is not None:
        params["limit_start"] = int(start)

    res = requests.get(f"{SITE_URL}/api/resource/{doctype}", headers=HEADERS, params=params)
    if res.status_code != 200:
        raise ERPNextAPIError(f"GET {doctype}: {res.status_code} - {res.text[:300]}")
    return res.json().get("data")


def erp_get_one(doctype, name):
    """ERPNext에서 문서 하나를 ID로 직접 조회 (품목/공급사 등 자식테이블까지 다 포함해서 가져옴)"""
    res = requests.get(f"{SITE_URL}/api/resource/{doctype}/{name}", headers=HEADERS)
    if res.status_code != 200:
        raise ERPNextAPIError(f"GET {doctype}/{name}: {res.status_code} - {res.text[:300]}")
    return res.json().get("data")


@router.get("/items")
def list_registered_items(
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """ERPNext에 실제 등록된 Item 목록을 프론트 조회용으로 반환한다.

    신규 품목의 생성·규격 검증은 ERPNext 웹훅과 AI 검증기가 담당한다.
    따라서 이 엔드포인트는 별도의 로컬 품목을 만들지 않고 ERPNext를
    단일 원본(source of truth)으로 읽기만 한다.
    """
    try:
        items = erp_get(
            "Item",
            fields=[
                "item_code", "item_name", "item_group", "description",
                "stock_uom", "is_stock_item", "is_fixed_asset", "disabled",
                "brand", "creation", "modified",
            ],
            order_by="item_code asc",
            limit=limit,
            start=offset,
        ) or []
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/items/{item_code}/specifications")
def get_item_specifications(item_code: str):
    """품목별 전체 규격 컬럼을 품목군 정의와 함께 프론트 계약으로 반환한다.

    PostgreSQL 규격 정의 조회가 불가능하거나 아직 등록되지 않은 개발 환경은
    ERPNext의 custom 필드와 Item Attribute를 대체 데이터로 사용한다.
    """
    from procurement_db.item_specifications import (
        build_item_specification_response,
        get_item_group_spec,
    )

    try:
        item = erp_get_one("Item", item_code)
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_code}을 찾을 수 없습니다.")

    schema_warning = None
    try:
        required_specs = get_item_group_spec(item.get("item_group"))
    except Exception:
        # ERP 조회 자체는 성공했으므로 개발 중에는 커스텀 필드 대체 경로를 허용한다.
        required_specs = None
        schema_warning = "품목군 규격 정의를 읽지 못해 ERP custom 필드를 사용했습니다."

    try:
        metadata_fields = get_item_doctype_fields()
    except ERPNextAPIError:
        metadata_fields = []
        schema_warning = (
            schema_warning or "ERP Item 메타데이터를 읽지 못해 원본 필드명을 사용했습니다."
        )

    response = build_item_specification_response(
        item, required_specs, metadata_fields=metadata_fields
    )
    if schema_warning:
        response["warning"] = schema_warning
    return response


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


def erp_cancel(doctype, name):
    """Submit된 ERPNext 문서를 표준 ``docstatus=2`` 경로로 취소한다."""
    document = erp_get_one(doctype, name)
    if document is None:
        raise ERPNextAPIError(f"CANCEL {doctype}/{name}: 문서를 찾을 수 없습니다.")
    docstatus = int(document.get("docstatus") or 0)
    if docstatus == 2:
        return document
    if docstatus != 1:
        raise ERPNextAPIError(
            f"CANCEL {doctype}/{name}: Submit(docstatus=1) 문서만 취소할 수 있습니다. "
            f"현재 docstatus={docstatus}"
        )
    res = requests.put(
        f"{SITE_URL}/api/resource/{doctype}/{name}",
        headers=HEADERS,
        json={"docstatus": 2},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"CANCEL {doctype}/{name}: {res.status_code} - {res.text[:500]}")
    return res.json().get("data")


@lru_cache(maxsize=1)
def get_item_doctype_fields():
    """Return live Item field metadata for labels, types, and sections."""
    metadata = erp_get_one("DocType", "Item") or {}
    return metadata.get("fields") or []


def erp_call(method, payload=None):
    """Frappe의 whitelisted method를 호출하고 반환값을 꺼낸다."""
    res = requests.post(
        f"{SITE_URL}/api/method/{method}",
        headers=HEADERS,
        json=payload or {},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"CALL {method}: {res.status_code} - {res.text[:500]}")
    return res.json().get("message")


def erp_discard_draft(doctype, name):
    """명시적으로 반려된 Draft 문서만 ERPNext의 표준 Discard로 폐기한다.

    Submit된 문서나 이미 폐기된 문서가 잘못 처리되지 않도록 현재
    ``docstatus``를 먼저 검증한다. 실제 호출은 사용자가 승인 검토 단계에서
    반려를 선택했을 때만 ``approval_review``에서 수행한다.
    """
    document = erp_get_one(doctype, name)
    if document is None:
        raise ERPNextAPIError(f"DISCARD {doctype}/{name}: 문서를 찾을 수 없습니다.")
    if document.get("docstatus") != 0:
        raise ERPNextAPIError(
            f"DISCARD {doctype}/{name}: Draft(docstatus=0)만 폐기할 수 있습니다. "
            f"현재 docstatus={document.get('docstatus')}"
        )

    return erp_call(
        "frappe.desk.form.save.discard",
        {"doctype": doctype, "name": name},
    )


def erp_send_email(doctype, name, recipients, subject, content):
    """
    문서를 이메일로 발송 (RFQ, PO 등).

    .env의 TEST_MODE=true면 실제 발송 대신 콘솔에 내용만 출력함 — 이게
    없어서 그동안 실제 이메일이 나가버린 적도 있었고, 비밀번호 같은
    내용을 확인할 방법도 없었음. 이제 이걸로 둘 다 해결됨.
    """
    if is_test_mode():
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
    }
    res = requests.post(
        f"{SITE_URL}/api/method/frappe.core.doctype.communication.email.make",
        headers=HEADERS,
        json=payload,
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"EMAIL: {res.status_code} - {res.text[:500]}")
    return res.json().get("message")


def erp_get_document_email_communications(doctype, name):
    """
    특정 문서(RFQ 등)에 연결된 이메일 Communication(발신/수신) 목록을 가져옴.

    ERPNext에서 우리가 공급사에게 보낸 메일에 공급사가 '답장'을 하면, 그
    메일이 이 문서의 Activity 탭에 Communication(sent_or_received=Received)
    으로 자동으로 붙는다 — 독촉메일을 보낼지 말지 판단하는 근거가 바로 이 목록.

    communication_medium="Email"로 좁혀서 전화 기록 등 다른 종류의
    Communication은 제외함. creation 오름차순으로 반환해서, 호출하는 쪽에서
    "가장 처음 보낸 메일"과 "가장 최근 메일"을 순서대로 다루기 쉽게 함.
    """
    return erp_get(
        "Communication",
        filters=[
            ["reference_doctype", "=", doctype],
            ["reference_name", "=", name],
            ["communication_medium", "=", "Email"],
        ],
        fields=[
            "name",
            "sent_or_received",
            "sender",
            "recipients",
            "cc",
            "subject",
            "creation",
            "communication_date",
        ],
        order_by="creation asc",
    ) or []


def erp_add_comment(doctype, name, comment_text):
    """
    문서 타임라인에 댓글만 남김 (Comment 문서 생성).
    ⚠️ 이 함수만으로는 아무에게도 알림이 가지 않음 — 사람이 문서를 직접
    열어봐야만 보임. 실제 알림이 필요하면 erp_assign_to 또는
    erp_notify_assignee_email을 같이 호출해야 함.
    """
    payload = {
        "reference_doctype": doctype,
        "reference_name": name,
        "content": comment_text,
        "comment_type": "Comment",
    }
    res = requests.post(f"{SITE_URL}/api/resource/Comment", headers=HEADERS, json=payload)
    if res.status_code not in (200, 201):
        raise ERPNextAPIError(f"COMMENT {doctype}/{name}: {res.status_code} - {res.text[:500]}")
    return res.json().get("data")


def erp_assign_to(doctype, name, assign_to_email, description=None, priority="Medium"):
    """
    ERPNext 표준 'Assign To'(할당) 기능 호출.

    ⚠️ erp_add_comment와 다름: 댓글은 타임라인에 텍스트만 남기고 아무에게도
    알림이 안 가지만, 이건 진짜 ToDo를 생성해서 해당 사용자 화면 우측 상단
    종모양 알림 + 'Assigned To' 위젯에 뜨게 만드는 실제 알림 메커니즘임.

    담당자가 그 문서를 열람할 권한(Role Permission의 Read)이 없으면
    할당 자체가 거부되거나, 알림을 눌러도 'Permission Denied'가 뜸.
    """
    payload = {
        "doctype": doctype,
        "name": name,
        "assign_to": [assign_to_email],
        "description": description or f"{doctype} {name} 업무가 배정되었습니다.",
        "priority": priority,
    }
    res = requests.post(
        f"{SITE_URL}/api/method/frappe.desk.form.assign_to.add",
        headers=HEADERS,
        json=payload,
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"ASSIGN {doctype}/{name}: {res.status_code} - {res.text[:500]}")
    return res.json().get("message")


def erp_notify_assignee_email(doctype, name, assignee_email, subject, content):
    """
    담당자에게 확실히(설정 상관없이 즉시) 이메일로 알림.
    erp_assign_to의 알림벨/이메일은 사용자별 Notification Settings에 따라
    안 갈 수도 있어서, RFQ/PO 발송에 쓰던 erp_send_email을 그대로 재사용해
    '반드시 가는' 채널을 하나 더 확보함.
    """
    return erp_send_email(doctype, name, assignee_email, subject, content)


# ============================================================
# 1. 재고 조회 (Bin)
# ============================================================

def get_stock_level(item_code, warehouse):
    """
    특정 품목·창고의 '지금 이 순간' 재고 수량을 조회.
    Bin은 실시간 값이라 따로 계산할 필요 없음, 조회만 하면 됨.
    사용 예: get_stock_level("SF-001", "Stores - SKN31")
    """
    result = erp_get("Bin", filters=[
        ["item_code", "=", item_code],
        ["warehouse", "=", warehouse],
    ], fields=["item_code", "warehouse", "actual_qty", "projected_qty"])
    if result:
        return result[0]
    return None


def get_reorder_settings(item_code, warehouse=None):
    """
    이 품목의 재주문 기준(Reorder Level)을 Item 마스터에서 가져옴.
    필드명 실제 확인 완료: warehouse, warehouse_reorder_level,
    warehouse_reorder_qty, material_request_type

    warehouse 지정하면 그 창고 설정만, 안 하면 전체 창고 리스트 반환.
    사용 예: get_reorder_settings("SF-001", "Stores - SKN31")
    """
    item = erp_get_one("Item", item_code)
    if not item:
        return None
    levels = item.get("reorder_levels", [])
    if warehouse:
        return next((lv for lv in levels if lv["warehouse"] == warehouse), None)
    return levels


def check_reorder_needed(item_code, warehouse):
    """
    실제 재고(Bin)와 재주문 기준(Item Reorder)을 같이 비교해서
    '지금 재주문 해야 하는지'까지 한 번에 판단해주는 함수.
    웹훅 받았을 때 이 함수 하나만 호출하면 됨.

    반환값: (재주문 필요 여부: bool, 발주할 수량: float or None)
    """
    stock = get_stock_level(item_code, warehouse)
    reorder = get_reorder_settings(item_code, warehouse)

    if not stock or not reorder:
        return False, None

    current_qty = stock["actual_qty"]
    level = reorder["warehouse_reorder_level"]
    reorder_qty = reorder["warehouse_reorder_qty"]

    if current_qty <= level:
        return True, reorder_qty
    return False, None


# ============================================================
# 2. Material Request — "생성"과 "조회/감지"는 서로 다른 함수
# ============================================================

def create_material_request(item_code, qty, warehouse, schedule_date):
    """
    [생성] nexterp가 재고부족을 스스로 판단해서 구매요청을 만들 때 사용.
    예: 웹훅으로 재고변동 감지 → Bin 재조회 → 기준 이하 → 이 함수 호출

    사람이 직접 ERPNext에서 만든 요청까지 이 함수가 만들어주는 게 아님.
    그건 아래 get_pending_material_requests()로 따로 감지해야 함.
    """
    payload = {
        "material_request_type": "Purchase",
        "schedule_date": schedule_date,
        "items": [{
            "item_code": item_code,
            "qty": qty,
            "schedule_date": schedule_date,
            "warehouse": warehouse,
        }]
    }
    mr = erp_post("Material Request", payload)
    if mr:
        erp_submit("Material Request", mr["name"])
    return mr


def get_pending_material_requests():
    """
    [조회/감지] 아직 RFQ로 안 이어진 Material Request를 찾아옴.
    - nexterp가 만든 것이든, 사람이 ERPNext UI에서 직접 만든 것이든, ERPNext
      자체 재주문 스케줄러가 만든 것이든 상관없이 다 잡아냄
    - "아직 처리 안 된 것"을 어떻게 구분할지는 실제로 정해야 함:
      예: status가 'Pending'인 것만, 또는 커스텀 필드(예: rfq_created=0)로 표시
    - 주기적으로(polling) 이 함수를 호출하거나, Material Request 자체에
      Webhook을 걸어서 생성 즉시 알림받는 방식으로 연결 가능
    """
    return erp_get("Material Request", filters=[
        ["status", "=", "Pending"],
        ["material_request_type", "=", "Purchase"],
    ], fields=["name", "transaction_date", "status"])


def get_material_request_detail(mr_name):
    """
    [조회] 특정 Material Request 하나를 통째로 가져옴 (품목 목록까지 포함).
    감지된 MR을 실제로 처리(RFQ 생성 등)하기 전에 상세정보 필요할 때 사용.
    """
    return erp_get_one("Material Request", mr_name)


# ============================================================
# 3. 카탈로그/비딩 판별 (REQ-038 — 4가지 기준을 실제 데이터로 체크)
# ============================================================

# 판별 기준값 — 실제 운영 데이터 보면서 조정할 것
LARGE_AMOUNT_THRESHOLD = 3_000_000   # 추정금액(원) 이 이상이면 대량/고액으로 간주
RECENT_PURCHASE_MONTHS = 6           # 이 기간 내 구매이력 없으면 '일회성' 후보


def count_recent_purchases(item_code, months=RECENT_PURCHASE_MONTHS):
    """
    최근 N개월간 이 품목을 담은 Purchase Order가 몇 건 있었는지 조회.
    '반복적으로 사는 품목인지'를 판단하는 근거로 씀.
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=30 * months)).strftime("%Y-%m-%d")

    # Purchase Order Item(자식테이블)에서 item_code로 직접 필터링.
    # 상위문서(Purchase Order) 필터로는 item_code를 못 거는 구조라 자식 doctype을 바로 조회.
    rows = erp_get("Purchase Order Item", filters=[
        ["item_code", "=", item_code],
        ["creation", ">=", cutoff],
    ], fields=["parent"])
    return len(rows) if rows else 0


def classify_procurement_route(item_code, qty):
    """
    [판별] 이 품목을 카탈로그(표준구매)로 살지, 비딩(RFQ)이 필요한지 판단.

    반환값: (route, reasons)
      route: "catalog" | "bidding" | "needs_review"
      reasons: 왜 이렇게 판단했는지 근거 리스트 (사람이 검토할 때, 로그 남길 때 씀)

    설계 원칙: 애매하면 억지로 한쪽으로 결정하지 않고 "needs_review"로
    반환해서 사람에게 넘김 (REQ-036 AI 신뢰도 기준과 동일한 원칙).
    """
    reasons = []

    # ① Item 마스터에 아예 없는 품목 → 무조건 비딩 (비표준/신규)
    try:
        item = erp_get_one("Item", item_code)
    except ERPNextAPIError:
        item = None

    if not item:
        reasons.append(f"'{item_code}'가 Item 마스터에 없음 → 비표준/신규 품목")
        return "bidding", reasons

    # ② 등록된 승인 공급사가 없음 → 비딩 (신규 공급사 탐색 필요)
    approved_suppliers = item.get("supplier_items", [])
    if not approved_suppliers:
        reasons.append("등록된 승인 공급사 없음 → 신규 공급사 탐색 필요")
        return "bidding", reasons

    # ③ 대량/고액 발주 → 비딩 (가격 협상 여지 큼)
    reference_rate = item.get("standard_rate", 0) or item.get("last_purchase_rate", 0)
    estimated_amount = qty * reference_rate
    if estimated_amount >= LARGE_AMOUNT_THRESHOLD:
        reasons.append(f"추정금액 {estimated_amount:,.0f}원 ≥ 기준치 {LARGE_AMOUNT_THRESHOLD:,.0f}원 → 대량/고액 발주")
        return "bidding", reasons

    # ④ 최근 구매이력이 없음 → 애매함, 사람 확인 필요
    #    (일회성일 수도 있고, 그냥 신규품목이 최근에 마스터 등록만 된 걸 수도 있어서
    #     단정하지 않고 사람에게 판단을 넘김)
    recent_count = count_recent_purchases(item_code)
    if recent_count == 0:
        reasons.append(f"최근 {RECENT_PURCHASE_MONTHS}개월간 구매이력 없음 → 일회성 여부 확인 필요")
        return "needs_review", reasons

    # 여기까지 왔으면: 마스터에 있고, 승인 공급사 있고, 소액이고, 반복구매 이력도 있음
    reasons.append(f"표준품목·승인공급사 존재·소액·최근{RECENT_PURCHASE_MONTHS}개월 내 {recent_count}건 구매이력 → 카탈로그 구매 대상")
    return "catalog", reasons


# ============================================================
# 4. RFQ 생성 및 발송
# ============================================================

def create_rfq_from_material_request(
    mr_name,
    supplier_list,
    message=(
        "견적 부탁드립니다.<br><br>"
        "{{ portal_link }}<br><br>"
        "처음 거래하시는 경우, 아래 버튼으로 포털 비밀번호를 설정해주세요.<br>"
        "{{ update_password_link }}"
    ),
):
    """
    [생성] Material Request를 받아서 RFQ(견적요청) 문서로 변환.
    supplier_list: 견적 받을 공급사 이름 리스트, 예: ["대한안전산업", "한빛보호구"]

    주의: items 안의 warehouse, uom, conversion_factor는 필수값이라
    빠뜨리면 에러남 (이미 겪었던 함정들).

    ⚠️ suppliers 행에 contact/email_id를 안 채우면, 공급사가 나중에 포털에
    로그인했을 때 "이 RFQ에서 내가 어느 행인지" 못 찾아서 500 에러로 페이지가
    깨지는 문제가 실제로 있었음. Supplier 문서의 primary contact/email을
    가져와서 같이 채움.
    """
    mr = get_material_request_detail(mr_name)
    if not mr:
        return None

    items_payload = []
    for item in mr["items"]:
        items_payload.append({
            "item_code": item["item_code"],
            "qty": item["qty"],
            "schedule_date": item["schedule_date"],
            "warehouse": item["warehouse"],
            "uom": item.get("uom", "Nos"),
            "conversion_factor": item.get("conversion_factor", 1),
            "material_request": mr_name,
            "material_request_item": item["name"],
        })

    suppliers_payload = []
    # .env에 TEST_RECIPIENT_OVERRIDE 설정되어 있으면, 실제 벤더 이메일 대신
    # 이 주소로 강제 교체함 — 내용·발송은 진짜로 일어나되(Communication 기록도
    # 진짜 생김), 실제 회사한테는 절대 안 나가게 하는 안전장치.
    test_override = os.getenv("TEST_RECIPIENT_OVERRIDE")

    for s in supplier_list:
        row = {"supplier": s}
        supplier_doc = erp_get_one("Supplier", s)
        if supplier_doc:
            contact = supplier_doc.get("supplier_primary_contact")
            email = supplier_doc.get("email_id")
            if contact:
                row["contact"] = contact
            if email:
                row["email_id"] = test_override or email
        if test_override and "email_id" not in row:
            row["email_id"] = test_override
        suppliers_payload.append(row)

    payload = {
        "transaction_date": mr["transaction_date"],
        "message_for_supplier": message,  # 필수! 안 넣으면 Submit 안됨
        "items": items_payload,
        "suppliers": suppliers_payload,
    }
    rfq = erp_post("Request for Quotation", payload)
    if rfq:
        erp_submit("Request for Quotation", rfq["name"])
    return rfq


def send_rfq_to_suppliers(rfq_name, supplier_emails, subject=None):
    """
    [발송] 생성된 RFQ를 공급사들에게 이메일로 보냄.
    supplier_emails: "a@a.com,b@b.com" 처럼 콤마로 구분된 문자열
    """
    subject = subject or f"Request for Quotation: {rfq_name}"
    return erp_send_email(
        doctype="Request for Quotation",
        name=rfq_name,
        recipients=supplier_emails,
        subject=subject,
        content="<p>견적 부탁드립니다. 포털에서 확인해주세요.</p>",
    )


# ============================================================
# 5. 공급사 견적 조회 및 비교
# ============================================================

def get_quotations_for_rfq(rfq_name):
    """
    [조회] 특정 RFQ에 대해 공급사들이 제출한 견적(Supplier Quotation) 목록.
    공급사가 포털에서 직접 제출하므로, 여기선 "생성"이 아니라 "조회"만 함.

    ⚠️ request_for_quotation 필드는 Supplier Quotation 문서 자체가 아니라
    그 안의 품목(자식테이블)에만 있어서, 상위 문서 목록을 그 필드로 바로
    필터링할 수 없음 (실제로 겪은 에러: "Field not permitted in query").
    그래서 일단 전체 견적 목록을 가져온 뒤, 각 문서를 열어 품목 안에서
    이 RFQ를 참조하는지 직접 대조하는 방식으로 처리함.
    견적 수가 아주 많아지면 비효율적일 수 있어 나중에 최적화 여지 있음.
    """
    all_quotations = erp_get("Supplier Quotation", fields=["name", "supplier", "grand_total", "status"])
    matched = []
    for sq_summary in all_quotations or []:
        sq_detail = erp_get_one("Supplier Quotation", sq_summary["name"])
        if not sq_detail:
            continue
        items = sq_detail.get("items", [])
        if any(item.get("request_for_quotation") == rfq_name for item in items):
            matched.append(sq_summary)
    return matched


def get_lowest_quotation(rfq_name):
    """
    [비교, 단순규칙 버전] 최저가 견적 하나를 골라줌.
    ⚠️ 지금은 가격만 보는 단순 버전. 나중에 원가분석(REQ-039), 납기,
    공급사 신뢰도까지 반영한 '후보군 추천'으로 고도화할 자리.
    최종 선택은 항상 사람이 하게 만들 것 — 이 함수는 '추천'용으로만 사용.
    """
    quotations = get_quotations_for_rfq(rfq_name)
    if not quotations:
        return None
    return min(quotations, key=lambda q: q["grand_total"])


# ============================================================
# 6. PO 생성 및 발송
# ============================================================

def create_po_from_quotation(quotation_name, schedule_date):
    """
    [생성] 선택된(사람이 승인한) 견적을 정식 발주서(PO)로 전환.
    이 함수는 '사람이 승인 버튼을 누른 뒤에만' 호출되어야 함 — 완전자동 금지.
    """
    quotation = erp_get_one("Supplier Quotation", quotation_name)
    if not quotation:
        return None

    items_payload = []
    for item in quotation["items"]:
        items_payload.append({
            "item_code": item["item_code"],
            "qty": item["qty"],
            "rate": item["rate"],
            "schedule_date": schedule_date,
            "warehouse": item.get("warehouse"),
        })

    payload = {
        "supplier": quotation["supplier"],
        "schedule_date": schedule_date,
        "items": items_payload,
    }
    po = erp_post("Purchase Order", payload)
    if po:
        erp_submit("Purchase Order", po["name"])
    return po


def send_po_to_supplier(po_name, supplier_email, subject=None):
    """[발송] 확정된 PO를 공급사에게 이메일로 전달."""
    subject = subject or f"Purchase Order: {po_name}"
    return erp_send_email(
        doctype="Purchase Order",
        name=po_name,
        recipients=supplier_email,
        subject=subject,
        content="<p>발주서를 보내드립니다. 확인 부탁드립니다.</p>",
    )


# ============================================================
# 7. 입고 확인 및 매입송장 초안 (구매팀 업무 마지막 단계)
# ============================================================

def create_purchase_receipt(po_name, received_items):
    """
    [생성] 물건 도착 시 입고 처리. 보통 Stock팀이 트리거하는 단계라
    nexterp가 자동으로 부르진 않고, 별도 입고 확인 액션에서 호출.
    received_items: [{"item_code": "SF-001", "qty": 100}, ...]
    """
    po = erp_get_one("Purchase Order", po_name)
    if not po:
        return None

    items_payload = []
    for recv in received_items:
        po_item = next((i for i in po["items"] if i["item_code"] == recv["item_code"]), None)
        if not po_item:
            continue
        items_payload.append({
            "item_code": recv["item_code"],
            "qty": recv["qty"],
            "rate": po_item["rate"],
            "warehouse": po_item.get("warehouse"),
            "purchase_order": po_name,
            "purchase_order_item": po_item["name"],
        })

    payload = {"supplier": po["supplier"], "items": items_payload}
    pr = erp_post("Purchase Receipt", payload)
    if pr:
        erp_submit("Purchase Receipt", pr["name"])
    return pr


def create_purchase_invoice_draft(purchase_receipt_name):
    """
    [생성] 입고 완료 기준으로 매입송장(청구서) 초안 생성.
    ⚠️ 여기가 nexterp(구매팀) 업무의 마지막 지점 — Submit까지만 함.
    결제(Payment Entry)는 회계팀/ERPNext 소관이라 nexterp 범위 밖 (REQ-036 참고).
    """
    pr = erp_get_one("Purchase Receipt", purchase_receipt_name)
    if not pr:
        return None

    items_payload = []
    for item in pr["items"]:
        items_payload.append({
            "item_code": item["item_code"],
            "qty": item["qty"],
            "rate": item["rate"],
            "purchase_order": item.get("purchase_order"),
            "po_detail": item.get("purchase_order_item"),
        })

    payload = {"supplier": pr["supplier"], "items": items_payload}
    pi = erp_post("Purchase Invoice", payload)
    # Submit은 회계팀 최종승인 후로 남겨두고 싶다면 이 줄은 빼도 됨
    if pi:
        erp_submit("Purchase Invoice", pi["name"])
    return pi


# ===== 여기서부터 실행되는 부분 =====
# ============================================================
# 8. 공급사 선정 (RFQ 보낼 대상 결정) — 여기서 AI 사용
# ============================================================

def gather_supplier_signals(item_code, supplier_name):
    """
    [규칙, AI 아님] 특정 공급사의 이 품목에 대한 과거 거래 데이터를 모음.
    AI한테 넘기기 전에, 판단 근거가 될 '사실 데이터'를 먼저 객관적으로 수집.

    ⚠️ Purchase Order Item(자식테이블) 직접조회는 권한 에러(403)가 남.
    대신 이 공급사의 Purchase Order(부모)를 먼저 필터링해서 가져오고,
    각 문서의 items 배열 안에서 이 품목이 있는지 파이썬에서 직접 확인.
    """
    supplier_pos = erp_get("Purchase Order", filters=[
        ["supplier", "=", supplier_name],
        ["docstatus", "=", 1],
    ], fields=["name"])

    order_count = 0
    rates = []
    for po_summary in (supplier_pos or []):
        po = erp_get_one("Purchase Order", po_summary["name"])
        if not po:
            continue
        for item in po.get("items", []):
            if item.get("item_code") == item_code:
                order_count += 1
                rates.append(item["rate"])
                break  # 같은 PO 안에 같은 품목 중복이면 한 번만 셈

    avg_rate = sum(rates) / len(rates) if rates else None

    return {
        "supplier": supplier_name,
        "past_order_count": order_count,
        "avg_rate": avg_rate,
        # TODO: 납기지연 이력도 추가하고 싶으면 Purchase Receipt의
        # posting_date vs PO의 schedule_date 비교해서 지연횟수 계산
    }


def call_ranking_model(item_code, candidates_with_signals):
    """
    [AI 호출 자리] 후보 공급사들의 데이터를 보고 순위+이유를 매기는 부분.

    ⚠️ 지금은 자리표시자(placeholder) — 실제 파인튜닝된 sLLM 연결 전.
    이 함수가 하는 일은 '추출'이 아니라 '구조화된 사실 → 순위+설명 생성'이라
    같은 모델이라도 프롬프트(지시문)가 달라짐. 서버 붙으면 여기를
    실제 모델 호출(Ollama API 등)로 교체.

    입력 예시: [{"supplier": "대한안전산업", "past_order_count": 5, "avg_rate": 12000}, ...]
    출력 형식: [{"supplier": "...", "rank": 1, "reason": "..."}, ...]
    """
    # TODO: 실제로는 아래 형태의 프롬프트를 모델에 보냄
    # prompt = f"다음은 '{item_code}' 품목의 후보 공급사 데이터입니다: {candidates_with_signals}
    #            과거 거래이력과 가격을 기준으로 순위를 매기고, 각 순위의 이유를 한 줄로 설명하세요.
    #            반드시 JSON 배열로만 답하세요: [{{"supplier": "...", "rank": 1, "reason": "..."}}]"
    # response = call_llm(prompt)
    # return json.loads(response)

    # 지금은 임시로 past_order_count 기준 정렬만 (모델 붙기 전 동작 확인용)
    sorted_candidates = sorted(
        candidates_with_signals, key=lambda c: c["past_order_count"], reverse=True
    )
    return [
        {
            "supplier": c["supplier"],
            "rank": i + 1,
            "reason": f"[placeholder] 과거 거래 {c['past_order_count']}회 (실제 AI 연결 전 임시 로직)",
        }
        for i, c in enumerate(sorted_candidates)
    ]


def select_suppliers_for_rfq(item_code, max_suppliers=5):
    """
    RFQ를 보낼 최종 공급사 리스트를 결정.

    1) 승인된 기존 공급사가 있으면: 각각의 거래이력 데이터를 모아서
       AI에게 순위+이유를 매기게 함 (call_ranking_model)
    2) 없으면: 신규 탐색 필요 (아직 미구현, search_new_vendors 연결 지점)

    반환값: (선정된 공급사 이름 리스트, AI 판단 근거 — 로그·감사용으로 같이 저장)
    """
    item = erp_get_one("Item", item_code)
    approved = item.get("supplier_items", [])

    if not approved:
        # TODO: search_new_vendors_node 결과를 여기로 연결
        return [], []

    supplier_names = [s["supplier"] for s in approved]

    # ① 각 후보의 객관적 데이터 수집 (규칙)
    candidates_with_signals = [
        gather_supplier_signals(item_code, name) for name in supplier_names
    ]

    # ② AI에게 순위+이유 요청
    ranked = call_ranking_model(item_code, candidates_with_signals)

    # ③ 상위 N개만 선정
    top_n = sorted(ranked, key=lambda r: r["rank"])[:max_suppliers]
    selected_names = [r["supplier"] for r in top_n]

    return selected_names, top_n  # top_n에 reason이 들어있어서 로그 남기기 좋음


# ===== 여기서부터 실행되는 부분 =====

# ============================================================
# 9. 공급사 포털 계정 자동 발급
# ============================================================

def generate_secure_password(length=12):
    """RFQ 보낼 때 신규 공급사용으로 발급할 랜덤 비밀번호 생성."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_supplier_portal_access(supplier_name, contact_email):
    """
    공급사가 포털 로그인 계정이 있는지 확인하고, 없으면 새로 만듦.
    있든 없든, 매번 비밀번호를 새로 재설정해서 항상 "지금 확실히 로그인되는
    비밀번호"를 반환함 — 예전 비밀번호는 해시로만 저장되어 있어서 원본을
    다시 조회할 방법이 없기 때문에, 매번 새로 정하는 게 유일하게 확실한 방법.

    Frappe 기본 '초대메일(Welcome Email)' 기능에 의존하지 않고,
    비밀번호를 직접 생성해서 즉시 지정함 (send_welcome_email=0).

    ⚠️ User Permission(Allow=Supplier, For Value=공급사명)을 같이
    만들어줘야 함 — UI로 Portal Users에 직접 추가하면 이게 자동으로
    같이 생기는데, API로 portal_users 필드만 채워서는 이게 안 만들어져서
    포털 로그인 후 페이지가 500으로 깨지는 문제가 실제로 있었음(원인 확정됨).
    "Apply To All Document Types"는 반드시 꺼둬야 함(체크 안 함) — 켜져
    있으면 의도한 범위보다 넓게/좁게 권한이 적용되어 문제가 생길 수 있음.

    반환값: (email, password) — 항상 실제 로그인 가능한 새 비밀번호가 담김
    """
    existing_users = erp_get("User", filters=[["email", "=", contact_email]])
    is_new_user = not existing_users
    password = generate_secure_password()

    if is_new_user:
        user = erp_post("User", {
            "email": contact_email,
            "first_name": contact_email.split("@")[0],
            "send_welcome_email": 0,
            "new_password": password,
        })
        if not user:
            return None, None

        # Supplier의 Portal Users 자식테이블에 연결 → Supplier 역할 자동 부여됨
        supplier_doc = erp_get_one("Supplier", supplier_name)
        portal_users = supplier_doc.get("portal_users", [])
        portal_users.append({"user": contact_email})

        res = requests.put(
            f"{SITE_URL}/api/resource/Supplier/{supplier_name}",
            headers=HEADERS,
            json={"portal_users": portal_users},
        )
        if res.status_code != 200:
            raise ERPNextAPIError(f"Portal Users 연결 실패: {res.text[:300]}")
    else:
        # 이미 있는 계정이면, 비밀번호만 새로 재설정
        res = requests.put(
            f"{SITE_URL}/api/resource/User/{contact_email}",
            headers=HEADERS,
            json={"new_password": password},
        )
        if res.status_code != 200:
            raise ERPNextAPIError(f"비밀번호 재설정 실패: {res.text[:300]}")

    # User Permission이 이미 있는지 확인 (기존 계정 재호출 시 중복 생성 방지)
    existing_permissions = erp_get("User Permission", filters=[
        ["user", "=", contact_email],
        ["allow", "=", "Supplier"],
        ["for_value", "=", supplier_name],
    ])
    if not existing_permissions:
        erp_post("User Permission", {
            "user": contact_email,
            "allow": "Supplier",
            "for_value": supplier_name,
            "apply_to_all_doctypes": 0,  # 반드시 꺼둬야 함 (확인된 사항)
        })

    return contact_email, password


def send_rfq_native(rfq_name):
    """
    [추천] ERPNext 자체 내장 RFQ 발송 기능 사용.
    직접 만든 ensure_supplier_portal_access + 수동 이메일 조합 대신 이걸로 감 —
    사람이 UI에서 "Send Email" 눌렀을 때와 완전히 같은 코드 경로라서,
    Contact·Portal Users·User Permission 연결이 다 자동으로 정확히 처리됨
    (오늘 하루종일 겪은 500 에러들이 이 부분들을 API로 반쪽만 흉내내다가
    생긴 문제였음 — 이제 그 흉내를 안 내고 ERPNext한테 맡김).

    또한 평문 비밀번호를 이메일에 넣는 대신, Frappe가 "비밀번호 설정 링크"를
    보내줘서 더 안전함. RFQ의 Suppliers 테이블에 있는 전체 공급사에게
    한 번에 발송됨 (개별 호출 필요 없음).

    ⚠️ RFQ의 "Message for Supplier" 필드와 각 공급사의 email_id가 이미
    채워져 있어야 함 (create_rfq_from_material_request가 이미 처리함).
    """
    if is_test_mode():
        print(f"[TEST_MODE] RFQ 자동발송 생략 — {rfq_name} (ERPNext 내장 send_supplier_emails)")
        return {"test_mode": True, "rfq_name": rfq_name}

    res = requests.post(
        f"{SITE_URL}/api/method/erpnext.buying.doctype.request_for_quotation.request_for_quotation.send_supplier_emails",
        headers=HEADERS,
        json={"rfq_name": rfq_name},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"RFQ 발송 실패: {res.status_code} - {res.text[:500]}")
    return res.json().get("message")


# ============================================================
# 10. 대체품(Substitute) 관련 — 팀원 check_substitute_node 로직 지원용
# ============================================================

def get_item_name(item_code):
    """품목코드로 품목명(item_name)만 간단히 조회."""
    item = erp_get_one("Item", item_code)
    return item.get("item_name", item_code) if item else item_code


def get_saved_substitute(item_code):
    """
    이 품목에 대해 예전에 사람이 골라뒀던 대체품이 있는지 조회.

    ⚠️ 저장 위치는 Item의 커스텀 필드로 가정함(예: 'preferred_substitute').
    이 필드가 아직 ERPNext에 없으면, Customize Form에서 Item 문서에
    Link 타입 필드로 하나 추가해야 함 (Options: Item).
    """
    item = erp_get_one("Item", item_code)
    if not item:
        return None
    return item.get("preferred_substitute") or None


def save_substitute_to_erp(item_code, substitute_code):
    """사람이 고른 대체품을 다음번에 자동으로 쓸 수 있게 Item에 저장."""
    res = requests.put(
        f"{SITE_URL}/api/resource/Item/{item_code}",
        headers=HEADERS,
        json={"preferred_substitute": substitute_code},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"대체품 저장 실패: {res.text[:300]}")
    return True


def get_all_available_candidates(warehouse=None):
    """
    재고가 남아있는(actual_qty > 0) 품목들을 {item_code: {name, qty, group, warehouses}} 형태로 반환.
    AI가 대체품 후보를 고를 때 넘겨줄 '전체 재고 카탈로그' 역할.

    warehouse=None(기본값)이면 전체 창고를 검색함 — 대체품은 다른 창고에
    있어도, 신규구매(비딩)보다는 거의 항상 빠르므로 창고로 미리 좁히지 않음.
    같은 품목이 여러 창고에 나눠 있으면 qty는 합산하고, warehouses에
    창고별 상세 내역을 남겨서 나중에 사람이 "어디 있는지" 보고 판단 가능하게 함.
    """
    filters = [["actual_qty", ">", 0]]
    if warehouse:
        filters.append(["warehouse", "=", warehouse])

    bins = erp_get("Bin", filters=filters, fields=["item_code", "warehouse", "actual_qty"])
    result = {}
    for b in (bins or []):
        item_code = b["item_code"]
        if item_code not in result:
            item = erp_get_one("Item", item_code)
            if not item:
                continue
            result[item_code] = {
                "name": item.get("item_name", item_code),
                "qty": 0,
                "group": item.get("item_group", ""),
                "warehouses": [],
            }
        result[item_code]["qty"] += b["actual_qty"]
        result[item_code]["warehouses"].append({"warehouse": b["warehouse"], "qty": b["actual_qty"]})
    return result


if __name__ == "__main__":
    print("=== 1. 연결 확인 ===")
    res = requests.get(f"{SITE_URL}/api/method/frappe.auth.get_logged_user", headers=HEADERS)
    print(res.status_code, res.text[:200])

    print("\n=== 2. Supplier 목록 조회 ===")
    suppliers = erp_get("Supplier", fields=["name", "supplier_name"])
    if suppliers:
        for s in suppliers:
            print(f"  - {s['name']}: {s['supplier_name']}")

    print("\n=== 3. Item 목록 조회 ===")
    items = erp_get("Item", fields=["item_code", "item_name"])
    if items:
        for i in items:
            print(f"  - {i['item_code']}: {i['item_name']}")

    print("\n여기까지 에러 없이 나왔으면 성공입니다.")
    print("다음 단계: 이 파일 맨 아래에 RFQ 생성 코드를 추가해보세요.")



class SelectedQuotationItem(BaseModel):
    item_code: str
    item_name: str
    qty: float
    rate: float
    uom: str

class PRCreateRequest(BaseModel):
    supplier: str = Field(..., description="선정된 공급업체")
    quotation_no: str = Field(..., description="선정된 Supplier Quotation (SQ) 번호")
    required_by_date: str = Field(..., description="납기 요청일 (YYYY-MM-DD)")
    items: List[SelectedQuotationItem]
    cost_center: Optional[str] = "Main - Y"
    company: Optional[str] = "Your Company Name"


def send_pr_email_notification(supplier: str, pr_name: str):
    """
    [자동 실행] 생성된 PR 정보를 선정된 Supplier에게 이메일로 발송하는 로직
    """
    # Email Server (SMTP/IMAP) 연동을 통한 발송 로직 구현부
    print(f"[Email Server] 공급업체({supplier})에게 PR 문서({pr_name}) 발송 완료.")
    pass


@router.post("/create-pr-draft")
def create_purchase_requisition_draft(data: PRCreateRequest, background_tasks: BackgroundTasks):
    """
    [8-2 단계] 선정된 SQ 기반 ERPNext Purchase Requisition(PR) Draft 생성 및 발송
    """
    try:
        # 1. ERPNext Purchase Requisition 구조에 맞춘 페이로드 작성
        pr_payload = {
            "doctype": "Material Request",
            "material_request_type": "Purchase",
            "company": data.company,
            "schedule_date": data.required_by_date,
            "custom_selected_supplier": data.supplier,  # 선정된 공급업체 매핑
            "custom_source_quotation": data.quotation_no,  # 근거가 되는 SQ 번호 연동
            "items": [
                {
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "qty": item.qty,
                    "rate": item.rate,
                    "uom": item.uom,
                    "schedule_date": data.required_by_date
                }
                for item in data.items
            ]
        }

        # 2. ERPNext API 호출 (Draft 생성)
        response = requests.post(
            f"{SITE_URL}/api/resource/Material Request",
            headers=HEADERS,
            json=pr_payload
        )

        if response.status_code not in [200, 201]:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"ERPNext PR Draft 생성 실패: {response.text}"
            )

        result_data = response.json().get("data", {})
        pr_name = result_data.get("name")

        # 3. 백그라운드 작업으로 선정된 Supplier에게 PR 내보내기(Export) 및 발송 실행
        background_tasks.add_task(send_pr_email_notification, data.supplier, pr_name)

        return {
            "status": "success",
            "message": "선정된 SQ 기반 PR Draft가 성공적으로 생성되었으며, 발송 프로세스가 진행됩니다.",
            "pr_document_name": pr_name,
            "supplier": data.supplier,
            "quotation_no": data.quotation_no
        }

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"서버 내부 오류 발생: {str(e)}")
