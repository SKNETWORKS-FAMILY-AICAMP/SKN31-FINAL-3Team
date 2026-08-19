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
    if os.getenv("TEST_MODE", "false").lower() == "true":
        print(f"수신자: {recipients}, 내용: {content}")

    else:
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
 
    Frappe 기본 '초대메일(Welcome Email)' 기능에 의존하지 않고,
    비밀번호를 직접 생성해서 즉시 지정함 (send_welcome_email=0).
    이렇게 하는 이유: 초대메일도 결국 SMTP를 타는데, 이미 이메일
    설정으로 여러 번 고생했어서, 대신 RFQ 보내는 이메일에 로그인
    정보를 직접 실어보내는 방식이 더 확실함.
 
    반환값: (email, password)
      - 이미 계정이 있었으면 password는 None (기존 비번은 알 수 없음,
        새로 알려줄 필요 없다는 뜻)
      - 새로 만들었으면 password에 실제 발급된 비밀번호가 담김
    """
    existing_users = erp_get("User", filters=[["email", "=", contact_email]])
    if existing_users:
        return contact_email, None
 
    password = generate_secure_password()
    user = erp_post("User", {
        "email": contact_email,
        "first_name": contact_email.split("@")[0],
        "send_welcome_email": 0,
        "new_password": password,
    })
    if not user:
        return None, None
 
    # Supplier의 Portal Users 자식테이블에 연결 → Supplier 역할 자동 부여됨
    # ⚠️ 'portal_users' 필드명은 실제 확인 필요. 아래처럼 한 번 찍어보고
    # 다르면 맞는 이름으로 바꿀 것 (예전에 reorder_levels도 이렇게 확인했음):
    #   supplier_doc = erp_get_one("Supplier", supplier_name)
    #   print(supplier_doc.keys())
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
 
    return contact_email, password
 
 
def send_rfq_with_portal_access(rfq_name, supplier_name, contact_email):
    """
    RFQ 보내기 전에 포털 계정부터 확인/발급하고, 로그인 정보를
    RFQ 이메일 본문에 같이 실어서 보냄 (신규 공급사인 경우에만).
    """
    email, password = ensure_supplier_portal_access(supplier_name, contact_email)
    if email is None:
        raise ERPNextAPIError(f"'{supplier_name}' 포털 계정 발급 실패")
 
    if password:
        # 신규 계정 — 로그인 정보 포함
        content = (
            f"<p>견적 부탁드립니다. 아래 계정으로 포털에 로그인해 확인해주세요.</p>"
            f"<p>로그인: {email}<br>임시 비밀번호: {password}</p>"
            f"<p>포털: {SITE_URL}/rfq/{rfq_name}</p>"
        )
    else:
        # 기존 계정 — 로그인 정보 불필요
        content = f"<p>견적 부탁드립니다. 포털에서 확인해주세요.</p><p>{SITE_URL}/rfq/{rfq_name}</p>"
 
    return erp_send_email(
        doctype="Request for Quotation",
        name=rfq_name,
        recipients=contact_email,
        subject=f"Request for Quotation: {rfq_name}",
        content=content,
    )
 
 
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