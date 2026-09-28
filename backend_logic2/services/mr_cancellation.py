"""Preflight all linked documents before cancelling a purchasing request.

Submitted records are cancelled through ERPNext, never forcibly deleted.
External ERP changes are not transactional: a partial failure is surfaced and
retry skips documents already cancelled. Shared documents / existing POs need
manual handling, not cascading cancellation into someone else's purchase.
"""
from backend_logic2.integrations.erp_client import (
    erp_get, erp_get_one, erp_cancel, erp_discard_draft,
)


def _linked_names(doctype, child, field, names):
    if not names:
        return []
    found = []
    offset = 0
    while True:
        rows = erp_get(doctype, filters=[
            [child, field, "in", list(names)], [doctype, "docstatus", "!=", 2],
        ], fields=["name"], order_by="name asc", limit=100, start=offset) or []
        found.extend(row["name"] for row in rows)
        if len(rows) < 100:
            return list(dict.fromkeys(found))
        offset += 100


def cancel_linked_rfq_documents(case):
    values = (case.get("workflow_snapshot") or {}).get("values") or {}
    if case.get("status") in {"RUNNING", "QUEUED"}:
        raise ValueError("이 MR의 처리가 진행 중입니다. 완료 후 반려해 주세요.")
    if case.get("status") == "COMPLETED" or values.get("po_name"):
        raise ValueError("이미 발주된 MR은 이 화면에서 반려할 수 없습니다. PO 관리에서 확인해 주세요.")
    mr_name = case["mr_name"]
    if _linked_names("Purchase Order", "Purchase Order Item", "material_request", [mr_name]):
        raise ValueError("연결된 발주서가 있어 MR 반려를 중단했습니다. PO 관리에서 확인해 주세요.")

    names = [str(entry.get("rfq_name") or "").strip()
             for entry in values.get("rfq_rounds") or [] if isinstance(entry, dict)]
    names.append(str(values.get("rfq_name") or "").strip())
    # Include ERP-linked RFQs missing from the current snapshot / rebid history.
    names.extend(_linked_names("Request for Quotation", "Request for Quotation Item", "material_request", [mr_name]))
    names = list(dict.fromkeys(name for name in names if name))
    rfqs = []
    for name in names:
        doc = erp_get_one("Request for Quotation", name)
        if not doc or int(doc.get("docstatus") or 0) == 2:
            continue
        if not doc.get("items") or any(row.get("material_request") != mr_name for row in doc["items"]):
            raise ValueError(f"RFQ {name}에 다른 구매 건 또는 연결 미확인 품목이 있습니다. 수동 확인이 필요합니다.")
        rfqs.append(doc)

    sq_names = _linked_names("Supplier Quotation", "Supplier Quotation Item", "request_for_quotation", names)
    if _linked_names("Purchase Order", "Purchase Order Item", "supplier_quotation", sq_names):
        raise ValueError("견적에 연결된 발주서가 있어 MR 반려를 중단했습니다. PO 관리에서 확인해 주세요.")
    quotations = []
    for name in sq_names:
        doc = erp_get_one("Supplier Quotation", name)
        if not doc or int(doc.get("docstatus") or 0) == 2:
            continue
        if not doc.get("items") or any(row.get("request_for_quotation") not in names for row in doc["items"]):
            raise ValueError(f"견적 {name}이 다른 구매 건과 공유됩니다. 수동 확인이 필요합니다.")
        quotations.append(doc)

    # All ownership / PO checks precede the first write. ERP's own link checks
    # remain enabled, protecting documents created outside this app concurrently.
    plan = [("Supplier Quotation", doc) for doc in quotations] + [("Request for Quotation", doc) for doc in rfqs]
    for doctype, doc in plan:
        if int(doc.get("docstatus") or 0) == 0:
            erp_discard_draft(doctype, doc["name"])
        else:
            erp_cancel(doctype, doc["name"])
