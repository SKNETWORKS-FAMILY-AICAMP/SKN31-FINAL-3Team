"""Synchronize ERPNext Supplier Quotations with the procurement read model.

LangGraph owns workflow execution state.  This module deliberately stores live
quotation responses in ``procurement_case.quotation_snapshot`` instead of
rewriting a graph checkpoint.  Both polling and webhooks therefore feed the
same projection without moving a human approval step automatically.
"""

from __future__ import annotations

import re
from email.utils import getaddresses
from threading import Lock
from typing import Any

from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    erp_download_file,
    erp_get_communication_attachments,
    erp_get_rfq_received_communications,
    erp_get_one,
)
from backend_logic2.nodes.quotation.quotation_filter.get_supplier_quotations import get_quotations_for_rfq
from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
    QuotationParser,
    classify_source,
    extract_quotation_bytes,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    register_supplier_quotation,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import (
    load_rfq_requirements,
)
from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import events as event_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.workflow.process_commands import to_checkpoint_data


_EMAIL_EXTRACTION_LOCK = Lock()


def _document(payload: dict[str, Any]) -> dict[str, Any]:
    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Supplier Quotation webhook document가 필요합니다.")
    return document


def _email_addresses(value: Any) -> set[str]:
    return {
        address.strip().casefold()
        for _, address in getaddresses([str(value or "")])
        if address.strip()
    }


def _resolve_reply_supplier(
    rfq: dict[str, Any],
    sender: Any,
) -> tuple[str, str]:
    """RFQ 수신 대상과 회신 From 주소를 대조해 한 공급사를 확정한다."""
    sender_addresses = _email_addresses(sender)
    candidates: list[tuple[str, str, set[str]]] = []
    for row in rfq.get("suppliers") or []:
        supplier_id = str(row.get("supplier") or "").strip()
        if not supplier_id:
            continue
        supplier_doc = erp_get_one("Supplier", supplier_id) or {}
        supplier_name = str(
            row.get("supplier_name")
            or supplier_doc.get("supplier_name")
            or supplier_id
        ).strip()
        addresses = _email_addresses(row.get("email_id"))
        addresses.update(_email_addresses(supplier_doc.get("email_id")))
        candidates.append((supplier_id, supplier_name, addresses))

    matched = [candidate for candidate in candidates if candidate[2] & sender_addresses]
    if len(matched) == 1:
        return matched[0][0], matched[0][1]
    if not matched and len(candidates) == 1:
        # Communication 자체가 이 RFQ에 연결돼 있고 공급사가 한 곳뿐이면
        # ERP에 이메일이 누락된 경우에도 안전하게 유일 후보를 사용할 수 있다.
        return candidates[0][0], candidates[0][1]
    if matched:
        raise ValueError(f"회신 주소가 RFQ의 여러 공급사와 중복됩니다: {sorted(sender_addresses)}")
    raise ValueError(f"회신 주소를 RFQ 공급사와 연결할 수 없습니다: {sorted(sender_addresses)}")


def _communication_and_attachments(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Communication 또는 그 첨부 File 웹훅을 동일한 입력으로 정규화한다."""
    document = _document(payload)
    if document.get("attached_to_doctype") == "Communication":
        communication_name = str(document.get("attached_to_name") or "").strip()
        if not communication_name:
            raise ValueError("File.attached_to_name에 Communication 이름이 필요합니다.")
        communication = erp_get_one("Communication", communication_name) or {}
        if not communication:
            raise LookupError(f"Communication을 찾을 수 없습니다: {communication_name}")
        return communication, [document]

    communication = document
    communication_name = str(communication.get("name") or "").strip()
    if not communication_name:
        raise ValueError("Communication name이 필요합니다.")
    return communication, (
        communication.get("attachments")
        or erp_get_communication_attachments(communication_name)
    )


def register_quotation_email_event(
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    model_parser: QuotationParser | None = None,
) -> tuple[dict[str, Any], bool]:
    """RFQ 회신 첨부를 메모리에서 추출해 Supplier Quotation Draft로 등록한다."""
    communication, attachments = _communication_and_attachments(payload)
    communication_name = str(communication.get("name") or "").strip()
    if str(communication.get("sent_or_received") or "").casefold() != "received":
        return {"status": "ignored", "reason": "수신 이메일이 아닙니다."}, False
    if str(communication.get("communication_medium") or "Email").casefold() != "email":
        return {"status": "ignored", "reason": "이메일 Communication이 아닙니다."}, False
    if communication.get("reference_doctype") != "Request for Quotation":
        return {"status": "ignored", "reason": "RFQ에 연결된 이메일이 아닙니다."}, False

    rfq_name = str(communication.get("reference_name") or "").strip()
    if not rfq_name:
        raise ValueError("Communication.reference_name에 RFQ 이름이 필요합니다.")
    attachment_identity = ",".join(sorted(
        str(row.get("content_hash") or row.get("name") or row.get("file_url") or "")
        for row in attachments
    )) or "no-attachment"
    event, claimed = event_repository.begin_event(
        source="ERPNEXT",
        event_type="RFQ_QUOTATION_EMAIL_RECEIVED",
        external_id=communication_name,
        dedupe_key=f"erpnext:rfq_reply:{communication_name}:{attachment_identity}",
        payload=payload,
    )
    if not claimed:
        return {"status": "duplicate", "communication": communication_name}, False

    try:
        if not attachments:
            result = {
                "status": "awaiting_attachment",
                "communication": communication_name,
                "rfq_name": rfq_name,
                "registrations": [],
            }
            event_repository.complete_event(str(event["event_id"]))
            return result, True

        rfq = erp_get_one("Request for Quotation", rfq_name) or {}
        if not rfq:
            raise LookupError(f"Request for Quotation을 찾을 수 없습니다: {rfq_name}")
        supplier_id, supplier_name = _resolve_reply_supplier(
            rfq,
            communication.get("sender"),
        )
        rfq_requirements = load_rfq_requirements(rfq_name).model_dump(mode="json")
        registrations: list[dict[str, Any]] = []
        extraction_jobs: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for attachment in attachments:
            file_id = str(attachment.get("name") or "").strip()
            filename = str(attachment.get("file_name") or file_id).strip()
            if not file_id or not filename:
                continue
            try:
                classify_source(filename)
            except ValueError:
                continue
            try:
                downloaded = erp_download_file(
                    file_id,
                    expected_attached_to_doctype="Communication",
                )
                fallback_id = "EMAIL-" + re.sub(
                    r"[^A-Za-z0-9_-]+",
                    "-",
                    f"{communication_name}-{file_id}",
                ).strip("-")
                from backend_logic2.services import runpod_quotation_jobs
                if model_parser is None and runpod_quotation_jobs.webhook_mode():
                    extraction_jobs.append(runpod_quotation_jobs.enqueue(
                        downloaded["content"], filename, rfq_name,
                        supplier_name=supplier_name, supplier_id=supplier_id,
                        fallback_quotation_id=fallback_id, rfq_requirements=rfq_requirements,
                        message_id=communication_name, content_type=downloaded.get("content_type"),
                    ))
                    continue
                with _EMAIL_EXTRACTION_LOCK:
                    quotation = extract_quotation_bytes(
                        downloaded["content"],
                        filename,
                        rfq_name,
                        supplier_name=supplier_name,
                        supplier_id=supplier_id,
                        fallback_quotation_id=fallback_id,
                        rfq_requirements=rfq_requirements,
                        model_parser=model_parser,
                        message_id=communication_name,
                        content_type=downloaded.get("content_type"),
                    )
                registrations.append(register_supplier_quotation(quotation))
            except Exception as exc:
                failures.append({
                    "file_id": file_id,
                    "filename": filename,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        if not registrations and not extraction_jobs:
            if failures:
                raise RuntimeError(f"견적 첨부 추출 실패: {failures}")
            result = {
                "status": "ignored",
                "reason": "지원되는 견적 첨부파일이 없습니다.",
                "communication": communication_name,
                "rfq_name": rfq_name,
                "registrations": [],
            }
            event_repository.complete_event(str(event["event_id"]))
            return result, True

        case = case_repository.get_case_by_rfq(rfq_name)
        projection = None
        if case is not None:
            updated, changed = refresh_case_quotations(
                case,
                rfq_name=rfq_name,
                notify=True,
            )
            projection = {
                "case_id": str(updated["case_id"]),
                "changed": changed,
            }
        result = {
            "status": ("queued" if extraction_jobs else "processed") if not failures else "partially_processed",
            "communication": communication_name,
            "rfq_name": rfq_name,
            "supplier": supplier_id,
            "registrations": registrations,
            "extraction_jobs": extraction_jobs,
            "failures": failures,
            "projection": projection,
        }
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    if failures:
        # register_supplier_quotation()이 실패해도 위 for 루프는 예외를 던지지
        # 않고 failures에만 담아 여기까지 정상적으로 내려온다. 그런데 그동안은
        # 이 경우에도 complete_event()를 불러서 dedupe_key가 PROCESSED로
        # 영구 고정돼버렸다 - SQ가 실제로는 안 만들어졌는데도 그 이메일/첨부
        # 조합은 두 번 다시 재시도되지 않는(항상 "duplicate") 버그였다.
        # FAILED로 남겨야 begin_event()의 재시도 분기(status='FAILED')가
        # 다음 재시도(재조정 폴링 또는 재전송된 웹훅) 때 다시 claim해준다.
        event_repository.fail_event(
            str(event["event_id"]),
            f"일부 첨부 등록 실패: {failures}",
        )
    else:
        event_repository.complete_event(str(event["event_id"]))
    return result, True


def _rfq_names(document: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for key in ("request_for_quotation", "rfq_name"):
        value = str(document.get(key) or "").strip()
        if value:
            names.add(value)
    for item in document.get("items") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("request_for_quotation") or "").strip()
        if value:
            names.add(value)
    return sorted(names)


def _workflow_values(case: dict[str, Any]) -> dict[str, Any]:
    snapshot = case.get("workflow_snapshot") or {}
    values = snapshot.get("values") if isinstance(snapshot, dict) else {}
    return values if isinstance(values, dict) else {}


def _supplier_names(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(row.get("supplier") or row.get("supplier_name") or "").strip()
        for row in rows
        if str(row.get("supplier") or row.get("supplier_name") or "").strip()
    }


def build_quotation_snapshot(case: dict[str, Any], rfq_name: str) -> dict[str, Any]:
    """Read active SQ documents and calculate response rate from actual recipients."""

    quotations = [
        to_checkpoint_data(row)
        for row in get_quotations_for_rfq(rfq_name)
    ]
    values = _workflow_values(case)
    recipients = {
        str(value).strip()
        for value in values.get("selected_suppliers") or []
        if str(value).strip()
    }
    responders = _supplier_names(quotations)
    counted_responders = responders & recipients if recipients else responders
    recipient_count = len(recipients)
    responded_count = len(counted_responders)
    response_rate = (
        min(100, round(responded_count / recipient_count * 100))
        if recipient_count
        else 0
    )
    from backend_logic2.services.price_evaluation import build_price_evaluations
    item_code = case.get("item_code") or (case.get("summary") or {}).get("item_code")
    return {
        "price_evaluations": build_price_evaluations(quotations, item_code, rfq_name),
        "rfq_name": rfq_name,
        "recipient_suppliers": sorted(recipients),
        "responded_suppliers": sorted(counted_responders),
        "recipient_count": recipient_count,
        "responded_count": responded_count,
        "response_rate": response_rate,
        "quotations": quotations,
    }


def refresh_case_quotations(
    case: dict[str, Any],
    *,
    rfq_name: str | None = None,
    notify: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Refresh one case and publish an SSE-backed notification when it changed."""

    values = _workflow_values(case)
    resolved_rfq = str(rfq_name or values.get("rfq_name") or "").strip()
    if not resolved_rfq:
        raise ValueError("구매 작업에 연결된 Request for Quotation이 없습니다.")

    # ⚠️ 재비딩한 케이스는 지난 라운드의 RFQ도 ERPNext에 그대로 남아있어서
    # (rfq_rounds 이력), 그 지난 RFQ 앞으로 뒤늦게 들어온 Supplier
    # Quotation 웹훅이 이 함수를 다시 호출할 수 있다. 그때 여기서 그냥
    # 진행하면 quotation_snapshot(협력사 선정 화면에 보이는 "현재
    # 라운드" 견적회신율/마감정보)이 지난 라운드 데이터로 덮어써진다 -
    # 반드시 지금 진행 중인 라운드(values.rfq_name)일 때만 read model을
    # 갱신하고, 지난 라운드 조회는 build_quotation_snapshot을 직접 호출해
    # 그 결과만 반환하는 별도 API 경로(차수 팝업)로 처리한다.
    current_rfq = str(values.get("rfq_name") or "").strip()
    if current_rfq and resolved_rfq != current_rfq:
        return case, False

    previous = case.get("quotation_snapshot") or {}
    previous = previous if isinstance(previous, dict) else {}
    current = build_quotation_snapshot(case, resolved_rfq)
    updated, changed = case_repository.update_quotation_snapshot(
        str(case["case_id"]), current
    )
    if not changed or not notify:
        return updated, changed

    previous_names = _supplier_names(previous.get("quotations") or [])
    current_names = _supplier_names(current["quotations"])
    new_names = sorted(current_names - previous_names)
    removed_names = sorted(previous_names - current_names)
    if new_names:
        title = "신규 견적 회신이 도착했습니다"
        detail = f"신규 회신 {', '.join(new_names)}"
        notification_type = "SUPPLIER_QUOTATION_RECEIVED"
    elif removed_names:
        title = "견적 회신 상태가 변경되었습니다"
        detail = f"취소·삭제 {', '.join(removed_names)}"
        notification_type = "SUPPLIER_QUOTATION_REMOVED"
    elif previous:
        title = "협력사 견적 내용이 변경되었습니다"
        detail = "금액·납기 등 최신 내용을 반영했습니다"
        notification_type = "SUPPLIER_QUOTATION_UPDATED"
    else:
        # Empty initial baseline is useful for future comparisons but is not a
        # user-visible event.
        return updated, changed

    notification_repository.create_notification(
        case_id=str(case["case_id"]),
        recipient_id=case.get("assigned_user_id"),
        notification_type=notification_type,
        title=title,
        message=(
            f"{case.get('mr_name') or resolved_rfq} · {detail} · "
            f"회신 {current['responded_count']}/{current['recipient_count']}건"
        ),
        payload={
            "mr_name": case.get("mr_name"),
            "rfq_name": resolved_rfq,
            "stage": case.get("stage"),
            "responded_count": current["responded_count"],
            "recipient_count": current["recipient_count"],
            "response_rate": current["response_rate"],
            "new_suppliers": new_names,
            "removed_suppliers": removed_names,
        },
    )
    return updated, changed


def register_supplier_quotation_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Process one idempotent SQ create/update/submit/cancel/delete event."""

    document = _document(payload)
    quotation_name = str(document.get("name") or "").strip()
    if not quotation_name:
        raise ValueError("Supplier Quotation name이 필요합니다.")
    modified = document.get("modified") or document.get("creation") or "unknown"
    event_kind = str(payload.get("event") or payload.get("method") or "changed")
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="SUPPLIER_QUOTATION_CHANGED",
        external_id=quotation_name,
        dedupe_key=(
            event_id
            or f"erpnext:supplier_quotation:{quotation_name}:{modified}:{event_kind}"
        ),
        payload=payload,
    )
    if not created:
        return [], False

    try:
        rfq_names = _rfq_names(document)
        if not rfq_names and event_kind != "on_trash":
            current = erp_get_one("Supplier Quotation", quotation_name) or {}
            rfq_names = _rfq_names(current)
        if not rfq_names and event_kind == "on_trash":
            previous_case = case_repository.get_case_by_supplier_quotation(
                quotation_name
            )
            if previous_case is not None:
                previous_rfq = str(
                    _workflow_values(previous_case).get("rfq_name") or ""
                ).strip()
                if previous_rfq:
                    rfq_names = [previous_rfq]
        if not rfq_names:
            raise ValueError(
                f"Supplier Quotation {quotation_name}에서 RFQ 연결 정보를 찾지 못했습니다."
            )

        projections: list[dict[str, Any]] = []
        for rfq_name in rfq_names:
            case = case_repository.get_case_by_rfq(rfq_name)
            if case is None:
                projections.append({"rfq_name": rfq_name, "matched": False})
                continue
            updated, changed = refresh_case_quotations(
                case,
                rfq_name=rfq_name,
                notify=True,
            )
            projections.append(
                {
                    "rfq_name": rfq_name,
                    "matched": True,
                    "changed": changed,
                    "case_id": str(updated["case_id"]),
                    "quotation_snapshot": updated.get("quotation_snapshot") or {},
                }
            )
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return projections, True


def reconcile_supplier_quotations(*, notify: bool = True) -> dict[str, int]:
    """API 중단 중 누락된 이메일 회신과 Supplier Quotation을 함께 복구한다."""

    counts = {
        "cases": 0,
        "changed": 0,
        "failed": 0,
        "email_replies": 0,
        "email_failures": 0,
    }
    for case in case_repository.list_cases_for_quotation_reconciliation():
        counts["cases"] += 1
        values = _workflow_values(case)
        rfq_name = str(values.get("rfq_name") or "").strip()
        if rfq_name:
            try:
                communications = erp_get_rfq_received_communications(rfq_name)
            except ERPNextAPIError:
                counts["email_failures"] += 1
                communications = []
            for communication in communications:
                try:
                    _, processed = register_quotation_email_event({
                        "event": "reconcile",
                        "doc": communication,
                    })
                    if processed:
                        counts["email_replies"] += 1
                except (ERPNextAPIError, LookupError, RuntimeError, ValueError):
                    counts["email_failures"] += 1
        try:
            _, changed = refresh_case_quotations(case, notify=notify)
            if changed:
                counts["changed"] += 1
        except (ERPNextAPIError, LookupError, ValueError):
            counts["failed"] += 1
    return counts
