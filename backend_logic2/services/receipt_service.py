"""Project ERPNext Purchase Receipt events into delivery and scorecard state."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_get, erp_get_one
from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import deliveries as delivery_repository
from backend_logic2.repositories import events as event_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.repositories import tasks as task_repository
from backend_logic2.workflow.process_commands import to_checkpoint_data


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _purchase_order_facts(po_name: str) -> dict[str, Any]:
    purchase_order = erp_get_one("Purchase Order", po_name)
    if purchase_order is None:
        raise LookupError(f"Purchase Order를 찾을 수 없습니다: {po_name}")
    items = purchase_order.get("items") or []
    dates = [str(item.get("schedule_date")) for item in items if item.get("schedule_date")]
    return {
        "supplier": purchase_order.get("supplier"),
        "ordered_qty": sum((_decimal(item.get("qty")) for item in items), Decimal("0")),
        "promised_delivery_date": max(dates) if dates else purchase_order.get("schedule_date"),
    }


def ensure_delivery_for_po(case_id: str, po_name: str) -> dict[str, Any]:
    facts = _purchase_order_facts(po_name)
    return delivery_repository.refresh_delivery(
        case_id=case_id,
        po_name=po_name,
        supplier=facts["supplier"],
        promised_delivery_date=facts["promised_delivery_date"],
        ordered_qty=facts["ordered_qty"],
    )


def register_purchase_order_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Handle PO cancellation without allowing its old graph to revive it."""

    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Purchase Order webhook document가 필요합니다.")
    po_name = str(document.get("name") or "").strip()
    if not po_name:
        raise ValueError("Purchase Order name이 필요합니다.")
    modified = document.get("modified") or "unknown"
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="PURCHASE_ORDER_CHANGED",
        external_id=po_name,
        dedupe_key=event_id or f"erpnext:purchase_order:{po_name}:{modified}",
        payload=payload,
    )
    if not created:
        return {"po_name": po_name}, False

    try:
        case = case_repository.get_case_by_po(po_name)
        if case is None:
            result = {"po_name": po_name, "matched": False}
        elif int(document.get("docstatus") or 0) == 2:
            reason = f"ERPNext Purchase Order {po_name}가 취소되었습니다."
            task_repository.cancel_pending_tasks(str(case["case_id"]), reason=reason)
            closed = case_repository.transition_case(
                str(case["case_id"]),
                status="CANCELLED",
                stage="CANCELLED",
                reason=reason,
                triggered_by="erpnext_purchase_order_webhook",
            )
            notification_repository.create_notification(
                case_id=str(case["case_id"]),
                recipient_id=case.get("assigned_user_id"),
                notification_type="PURCHASE_ORDER_CANCELLED",
                title="PO가 취소되었습니다",
                message=f"{po_name} · 관련 구매 작업과 대기 입력을 종료했습니다.",
                payload={"po_name": po_name, "mr_name": case.get("mr_name")},
            )
            result = {"po_name": po_name, "matched": True, "case": closed}
        else:
            result = {"po_name": po_name, "matched": True, "case": case}
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return result, True


def register_purchase_receipt_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Purchase Receipt webhook document가 필요합니다.")
    receipt_name = str(document.get("name") or "").strip()
    if not receipt_name:
        raise ValueError("Purchase Receipt name이 필요합니다.")
    modified = document.get("modified") or "unknown"
    dedupe_key = event_id or f"erpnext:purchase_receipt:{receipt_name}:{modified}"
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="PURCHASE_RECEIPT_CHANGED",
        external_id=receipt_name,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    if not created:
        return [], False

    try:
        if not document.get("items"):
            document = erp_get_one("Purchase Receipt", receipt_name) or document
        posting_date = document.get("posting_date")
        if not posting_date:
            raise ValueError("Purchase Receipt posting_date가 필요합니다.")
        docstatus = int(document.get("docstatus") or 0)
        quantities: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for item in document.get("items") or []:
            po_name = item.get("purchase_order") or document.get("purchase_order")
            if not po_name:
                continue
            accepted = max(
                Decimal("0"),
                _decimal(item.get("qty")) - _decimal(item.get("rejected_qty")),
            )
            quantities[str(po_name)] += accepted

        if not quantities:
            raise ValueError("Purchase Order와 연결된 입고 품목이 없습니다.")

        projections: list[dict[str, Any]] = []
        for po_name, accepted_qty in quantities.items():
            case = case_repository.get_case_by_po(po_name)
            previous_delivery = (
                delivery_repository.get_delivery_by_case(str(case["case_id"]))
                if case is not None
                else None
            )
            delivery_repository.record_purchase_receipt(
                receipt_name=receipt_name,
                po_name=po_name,
                posting_date=posting_date,
                docstatus=docstatus,
                accepted_qty=accepted_qty,
                payload=document,
            )
            if case is None:
                projections.append({"po_name": po_name, "matched": False})
                continue
            delivery = ensure_delivery_for_po(str(case["case_id"]), po_name)
            projections.append({"po_name": po_name, "matched": True, "delivery": delivery})
            delivery_payload = to_checkpoint_data(delivery)

            if (
                delivery["delivery_status"] == "FULL"
                and delivery["scorecard_status"] != "COMPLETED"
                and case["status"] not in {"CANCELLED", "REJECTED"}
            ):
                existing_scorecard_task = next(
                    (
                        task for task in task_repository.list_tasks(
                            case_id=str(case["case_id"]), status="PENDING"
                        )
                        if task.get("task_type") == "supplier_scorecard"
                    ),
                    None,
                )
                if existing_scorecard_task is None:
                    task_repository.replace_pending_task(
                        case_id=str(case["case_id"]),
                        task_type="supplier_scorecard",
                        audience="BUYER",
                        channel="BIDDINGFLOW",
                        title="입고 완료 · Supplier Scorecard를 작성해주세요",
                        description=f"{po_name} 전체 입고가 확인되었습니다.",
                        input_schema={
                            "type": "scorecard",
                            "fields": ["leadTime", "quality", "price", "service", "communication"],
                            "minimum": 1,
                            "maximum": 5,
                        },
                        payload={"po_name": po_name, "delivery": delivery_payload},
                    )
                case_repository.transition_case(
                    str(case["case_id"]),
                    status="WAITING_INPUT",
                    stage="SCORECARD",
                    reason=f"Purchase Receipt {receipt_name}로 전체 입고가 확인되었습니다.",
                )
                if existing_scorecard_task is None:
                    notification_repository.create_notification(
                        case_id=str(case["case_id"]),
                        recipient_id=case.get("assigned_user_id"),
                        notification_type="PURCHASE_RECEIPT_COMPLETED",
                        title="물품 도착이 확인되었습니다",
                        message=f"{po_name} · 실제 수령일 {delivery['full_receipt_date']} · Scorecard 작성 필요",
                        payload={"po_name": po_name, "delivery": delivery_payload},
                    )
            elif (
                delivery["delivery_status"] in {"PARTIAL", "NOT_RECEIVED"}
                and case["status"] not in {"CANCELLED", "REJECTED"}
            ):
                reversal_reason = (
                    f"Purchase Receipt {receipt_name} 취소/변경으로 전체 입고 상태가 "
                    f"{delivery['delivery_status']} 상태로 되돌아갔습니다."
                )
                task_repository.cancel_pending_tasks_by_type(
                    str(case["case_id"]),
                    task_type="supplier_scorecard",
                    reason=reversal_reason,
                )
                case_repository.transition_case(
                    str(case["case_id"]),
                    status="RUNNING",
                    stage="DELIVERY",
                    reason=(
                        f"Purchase Receipt {receipt_name} 부분 입고가 반영되었습니다."
                        if delivery["delivery_status"] == "PARTIAL"
                        else reversal_reason
                    ),
                )
                if previous_delivery and previous_delivery.get("delivery_status") == "FULL":
                    notification_repository.create_notification(
                        case_id=str(case["case_id"]),
                        recipient_id=case.get("assigned_user_id"),
                        notification_type="PURCHASE_RECEIPT_REVERSED",
                        title="입고 완료 상태가 취소되었습니다",
                        message=(
                            f"{po_name} · 현재 상태 {delivery['delivery_status']} · "
                            "Supplier Scorecard 입력이 다시 잠겼습니다."
                        ),
                        payload={"po_name": po_name, "delivery": delivery_payload},
                    )

    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return projections, True


def _invoice_allocations(document: dict[str, Any]) -> dict[str, tuple[Decimal, Decimal]]:
    """Allocate invoice total/outstanding amounts across linked Purchase Orders."""

    item_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for item in document.get("items") or []:
        po_name = item.get("purchase_order") or document.get("purchase_order")
        if not po_name:
            continue
        item_totals[str(po_name)] += _decimal(
            item.get("base_net_amount") or item.get("net_amount") or item.get("amount")
        )
    if not item_totals:
        return {}

    grand_total = _decimal(document.get("grand_total") or document.get("rounded_total"))
    outstanding = max(Decimal("0"), _decimal(document.get("outstanding_amount")))
    weight_total = sum(item_totals.values(), Decimal("0"))
    po_names = list(item_totals)
    allocations: dict[str, tuple[Decimal, Decimal]] = {}
    allocated_total = Decimal("0")
    allocated_outstanding = Decimal("0")
    for index, po_name in enumerate(po_names):
        if index == len(po_names) - 1:
            po_total = grand_total - allocated_total
            po_outstanding = outstanding - allocated_outstanding
        else:
            ratio = item_totals[po_name] / weight_total if weight_total > 0 else Decimal("0")
            po_total = (grand_total * ratio).quantize(Decimal("0.01"))
            po_outstanding = (outstanding * ratio).quantize(Decimal("0.01"))
            allocated_total += po_total
            allocated_outstanding += po_outstanding
        allocations[po_name] = (max(Decimal("0"), po_total), max(Decimal("0"), po_outstanding))
    return allocations


def _project_purchase_invoice_document(document: dict[str, Any]) -> list[dict[str, Any]]:
    invoice_name = str(document.get("name") or "").strip()
    posting_date = document.get("posting_date")
    if not invoice_name or not posting_date:
        raise ValueError("Purchase Invoice name과 posting_date가 필요합니다.")

    allocations = _invoice_allocations(document)
    if not allocations:
        raise ValueError("Purchase Order와 연결된 매입송장 품목이 없습니다.")

    projections: list[dict[str, Any]] = []
    for po_name, (grand_total, outstanding) in allocations.items():
        case = case_repository.get_case_by_po(po_name)
        previous = (
            delivery_repository.get_delivery_by_case(str(case["case_id"]))
            if case is not None
            else None
        )
        delivery_repository.record_purchase_invoice(
            invoice_name=invoice_name,
            po_name=po_name,
            posting_date=posting_date,
            docstatus=int(document.get("docstatus") or 0),
            invoice_status=str(document.get("status") or "") or None,
            grand_total=grand_total,
            outstanding_amount=outstanding,
            payload=document,
        )
        delivery = delivery_repository.refresh_financial_progress(po_name)
        if case is None or delivery is None:
            projections.append({"po_name": po_name, "matched": False})
            continue
        projections.append({"po_name": po_name, "matched": True, "delivery": delivery})

        became_paid = (
            delivery.get("payment_status") == "PAID"
            and (previous or {}).get("payment_status") != "PAID"
        )
        if became_paid:
            notification_repository.create_notification(
                case_id=str(case["case_id"]),
                recipient_id=case.get("assigned_user_id"),
                notification_type="PURCHASE_PAYMENT_COMPLETED",
                title="대금 결제가 완료되었습니다",
                message=(
                    f"{po_name} · {invoice_name} · "
                    f"결제액 {delivery.get('paid_amount') or 0}"
                ),
                payload={
                    "po_name": po_name,
                    "invoice_name": invoice_name,
                    "payment_status": "PAID",
                },
            )
    return projections


def register_purchase_invoice_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Project submitted/cancelled Purchase Invoices into PO payment progress."""

    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Purchase Invoice webhook document가 필요합니다.")
    invoice_name = str(document.get("name") or "").strip()
    if not invoice_name:
        raise ValueError("Purchase Invoice name이 필요합니다.")
    modified = document.get("modified") or "unknown"
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="PURCHASE_INVOICE_CHANGED",
        external_id=invoice_name,
        dedupe_key=event_id or f"erpnext:purchase_invoice:{invoice_name}:{modified}",
        payload=payload,
    )
    if not created:
        return [], False
    try:
        if not document.get("items"):
            document = erp_get_one("Purchase Invoice", invoice_name) or document
        projections = _project_purchase_invoice_document(document)
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return projections, True


def register_payment_entry_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Record Payment Entry references and refresh their Purchase Invoices."""

    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Payment Entry webhook document가 필요합니다.")
    payment_name = str(document.get("name") or "").strip()
    if not payment_name:
        raise ValueError("Payment Entry name이 필요합니다.")
    modified = document.get("modified") or "unknown"
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="PAYMENT_ENTRY_CHANGED",
        external_id=payment_name,
        dedupe_key=event_id or f"erpnext:payment_entry:{payment_name}:{modified}",
        payload=payload,
    )
    if not created:
        return [], False
    try:
        if not document.get("references"):
            document = erp_get_one("Payment Entry", payment_name) or document
        posting_date = document.get("posting_date")
        if not posting_date:
            raise ValueError("Payment Entry posting_date가 필요합니다.")
        invoice_names: list[str] = []
        for reference in document.get("references") or []:
            if reference.get("reference_doctype") != "Purchase Invoice":
                continue
            invoice_name = str(reference.get("reference_name") or "").strip()
            if not invoice_name:
                continue
            invoice_names.append(invoice_name)
            delivery_repository.record_payment_entry(
                payment_entry_name=payment_name,
                invoice_name=invoice_name,
                posting_date=posting_date,
                docstatus=int(document.get("docstatus") or 0),
                allocated_amount=_decimal(reference.get("allocated_amount")),
                payload=document,
            )
        if not invoice_names:
            raise ValueError("Purchase Invoice와 연결된 결제 내역이 없습니다.")

        projections: list[dict[str, Any]] = []
        for invoice_name in sorted(set(invoice_names)):
            invoice = erp_get_one("Purchase Invoice", invoice_name)
            if invoice:
                projections.extend(_project_purchase_invoice_document(invoice))
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return projections, True


def _parent_names(
    parent_doctype: str,
    child_doctype: str,
    field: str,
    value: str,
) -> list[str]:
    """Find parent documents through a child-table filter.

    This ERPNext instance permits child rows to be filtered but does not expose
    their ``parent`` column through the REST list API. Querying the parent
    DocType with Frappe's four-part child filter returns stable document names.
    """

    rows = erp_get(
        parent_doctype,
        filters=[[child_doctype, field, "=", value]],
        fields=["name"],
        order_by="modified desc",
        limit=500,
    ) or []
    return [str(row["name"]) for row in rows if row.get("name")]


def reconcile_purchase_documents(*, po_name: str | None = None) -> dict[str, int]:
    """Recover missed receipt/invoice/payment webhooks from authoritative ERP data."""

    deliveries = delivery_repository.list_deliveries_for_reconciliation()
    if po_name:
        deliveries = [row for row in deliveries if row.get("po_name") == po_name]
    counts = {"purchase_receipts": 0, "purchase_invoices": 0, "payment_entries": 0}
    for delivery in deliveries:
        current_po = str(delivery.get("po_name") or "").strip()
        if not current_po:
            continue

        try:
            receipt_names = _parent_names(
                "Purchase Receipt", "Purchase Receipt Item", "purchase_order", current_po
            )
        except ERPNextAPIError:
            receipt_names = []
        for receipt_name in receipt_names:
            document = erp_get_one("Purchase Receipt", receipt_name)
            if document:
                _, created = register_purchase_receipt_event(document)
                counts["purchase_receipts"] += int(created)

        try:
            invoice_names = _parent_names(
                "Purchase Invoice", "Purchase Invoice Item", "purchase_order", current_po
            )
        except ERPNextAPIError:
            invoice_names = delivery_repository.list_invoice_names_by_po(current_po)
        for invoice_name in invoice_names:
            document = erp_get_one("Purchase Invoice", invoice_name)
            if document:
                _, created = register_purchase_invoice_event(document)
                counts["purchase_invoices"] += int(created)

            try:
                payment_names = _parent_names(
                    "Payment Entry",
                    "Payment Entry Reference",
                    "reference_name",
                    invoice_name,
                )
            except ERPNextAPIError:
                payment_names = []
            for payment_name in payment_names:
                payment = erp_get_one("Payment Entry", payment_name)
                if payment:
                    _, created = register_payment_entry_event(payment)
                    counts["payment_entries"] += int(created)
    return counts
