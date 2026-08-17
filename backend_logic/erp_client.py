import requests
import os
from dotenv import load_dotenv

#환경변수 로드
load_dotenv()

#연결 설정
SITE_URL = os.getenv('SITE_URL')
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')


HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Content-Type": "application/json",
}



class ERPNextAPIError(Exception):
    """ERPNext API 호출이 실패했을 때 던지는 예외. 조용히 None 반환하지 않고
    명확하게 실패를 알려서, 호출한 쪽(테스트 스크립트 등)이 정확히 감지하게 함."""
    pass


def erp_get(doctype, filters=None, fields=None):
    """ERPNext에서 문서 목록 조회"""
    import json
    params = {}
    if filters:
        params["filters"] = json.dumps(filters)
    if fields:
        params["fields"] = json.dumps(fields)

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


def erp_post(doctype, payload):
    """ERPNext에 새 문서 생성 (Draft 상태로 생성됨)"""
    payload = dict(payload)
    payload["doctype"] = doctype
    res = requests.post(f"{SITE_URL}/api/resource/{doctype}", headers=HEADERS, json=payload)
    if res.status_code not in (200, 201):
        raise ERPNextAPIError(f"POST {doctype}: {res.status_code} - {res.text[:500]}")
    return res.json().get("data")


def erp_submit(doctype, name):
    """Draft 문서를 Submit(확정)"""
    res = requests.put(
        f"{SITE_URL}/api/resource/{doctype}/{name}",
        headers=HEADERS,
        json={"docstatus": 1},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"SUBMIT {doctype}/{name}: {res.status_code} - {res.text[:500]}")
    return res.json().get("data")


def erp_send_email(doctype, name, recipients, subject, content):
    """문서를 이메일로 발송 (RFQ, PO 등)"""
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
# 3. 카탈로그/비딩 판별 (지금은 자리만 잡아둔 상태, AI모델 연결 전)
# ============================================================

def is_bidding_required(mr_item):
    """
    [판별] 이 Material Request 품목이 카탈로그로 살 수 있는 표준품인지,
    비딩이 필요한 품목(비표준/대량/일회성/신규탐색)인지 판단.

    ⚠️ 지금은 매우 단순한 규칙만 넣어둔 자리표시자(placeholder)임.
    실제로는:
      1) 수량이 기준치 이상이면 → True (대량발주)
      2) item_code가 표준품목 리스트에 없으면 → True (비표준)
      3) 애매하면 → 추출모델에게 mr_item 설명 텍스트를 보내서 판단 보조
    REQ-038, REQ-036(AI 신뢰도 기준)과 연동되는 지점.
    """
    LARGE_QTY_THRESHOLD = 500  # 예시값, 실제 기준으로 교체
    if mr_item.get("qty", 0) >= LARGE_QTY_THRESHOLD:
        return True
    # TODO: 표준품목 카탈로그 리스트와 대조하는 로직 추가
    # TODO: 애매한 경우 추출모델 호출해서 판단 보조
    return False


# ============================================================
# 4. RFQ 생성 및 발송
# ============================================================

def create_rfq_from_material_request(mr_name, supplier_list, message="견적 부탁드립니다."):
    """
    [생성] Material Request를 받아서 RFQ(견적요청) 문서로 변환.
    supplier_list: 견적 받을 공급사 이름 리스트, 예: ["대한안전산업", "한빛보호구"]

    주의: items 안의 warehouse, uom, conversion_factor는 필수값이라
    빠뜨리면 에러남 (이미 겪었던 함정들).
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

    payload = {
        "transaction_date": mr["transaction_date"],
        "message_for_supplier": message,  # 필수! 안 넣으면 Submit 안됨
        "items": items_payload,
        "suppliers": [{"supplier": s} for s in supplier_list],
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