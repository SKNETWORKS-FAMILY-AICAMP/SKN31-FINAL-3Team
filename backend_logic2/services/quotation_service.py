"""Synchronize ERPNext Supplier Quotations with the procurement read model.

LangGraph owns workflow execution state.  This module deliberately stores live
quotation responses in ``procurement_case.quotation_snapshot`` instead of
rewriting a graph checkpoint.  Both polling and webhooks therefore feed the
same projection without moving a human approval step automatically.
"""

from __future__ import annotations

import logging
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


LOGGER = logging.getLogger(__name__)

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


def classify_intake_failure(exc: BaseException) -> str | None:
    """견적서 자동 읽기 실패를 화면 안내용 종류로 나눈다.

    - parse: 모델 출력 JSON/스키마를 읽지 못함(ValueError·pydantic 검증 오류)
    - arithmetic: 읽긴 했지만 금액 계산이 맞지 않아 ERP에 등록할 수 없음
      (대부분 숫자를 잘못 읽은 경우라 원본 확인이 필요하다)
    - extraction: RunPod/네트워크 등으로 추출 자체가 끝나지 않음
    - None: 중복 제출·RFQ 대상 아님 같은 등록 규칙 위반 - 읽기 실패가 아니므로
      기록하지 않는다.
    """
    from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
        QuotationArithmeticValidationError,
        SupplierQuotationRegistrationError,
    )

    if isinstance(exc, QuotationArithmeticValidationError):
        return "arithmetic"
    if isinstance(exc, SupplierQuotationRegistrationError):
        return None
    if isinstance(exc, ValueError):
        return "parse"
    return "extraction"


def record_intake_failure(
    exc: BaseException,
    *,
    rfq_name: str,
    supplier_id: str | None,
    supplier_name: str | None,
    source_filename: str | None,
    file_id: str | None,
    communication_name: str | None,
    kind: str | None = None,
) -> None:
    """실패 기록은 보조 채널 - 기록이 실패해도 원래 처리를 막지 않는다."""
    failure_kind = kind or classify_intake_failure(exc)
    if failure_kind is None:
        return
    try:
        from backend_logic2.repositories import quotation_intake_failures

        quotation_intake_failures.record_failure(
            rfq_name=rfq_name,
            failure_kind=failure_kind,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            source_filename=source_filename,
            file_id=file_id,
            communication_name=communication_name,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    except Exception:  # noqa: BLE001
        LOGGER.warning("견적 읽기 실패 기록에 실패했습니다: rfq=%s", rfq_name, exc_info=True)


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
                record_intake_failure(
                    exc,
                    rfq_name=rfq_name,
                    supplier_id=supplier_id,
                    supplier_name=supplier_name,
                    source_filename=filename,
                    file_id=file_id,
                    communication_name=communication_name,
                )

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
        # 재시도하지 않는다: 검증 스키마 버그처럼 "몇 번을 다시 시도해도 똑같이
        # 실패하는" 원인이면 무한 재시도는 같은 에러만 반복해서 쌓을 뿐이다.
        # DISCARDED로 남겨 begin_event()가 다시 claim하지 않게 하고, 에러
        # 내용은 last_error에 그대로 남겨 나중에 확인할 수 있게 한다.
        event_repository.discard_event(str(event["event_id"]), str(exc))
        raise
    if failures:
        # register_supplier_quotation()이 실패해도 위 for 루프는 예외를 던지지
        # 않고 failures에만 담아 여기까지 정상적으로 내려온다. 예전엔 이 경우에도
        # complete_event()를 불러서 dedupe_key가 PROCESSED로 영구 고정돼버렸다 -
        # SQ가 실제로는 안 만들어졌는데도 그 이메일/첨부 조합은 두 번 다시
        # 재시도되지 않는(항상 "duplicate") 버그였다. 그렇다고 무한 재시도(FAILED)로
        # 두면, 원인이 정말 일시적인 게 아니라 코드 버그일 때 같은 실패가 폴링
        # 주기마다 영원히 반복된다. 그래서 한 번 실패하면 DISCARDED로 종결하고
        # (재시도 없음), 원인은 last_error에 남겨서 필요하면 수동으로 확인한다.
        event_repository.discard_event(
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


# 견적 회신이 도착할 때마다 규격 평가(RunPod)를 미리 돌려두는 단계.
# 예전에는 사람이 '회신 새로 확인'(= check_quotations 재개)을 누르는 순간에야
# 모든 견적의 규격 평가가 한꺼번에 시작돼서, 회신이 많으면 그 자리에서 몇
# 분을 기다려야 했다. evaluate_quotations()는 quotation_specification_cache에
# 지문(fingerprint)으로 캐시를 남기고 캐시가 맞는 견적은 건너뛰므로, 도착
# 시점에 미리 한 번 돌려두면 나중 분석은 캐시 히트로 즉시 끝난다.
#
# 여기서는 순위 결과를 쓰지 않는다 - 목적은 캐시를 채워두는 것뿐이고,
# LangGraph 상태/대기 작업/케이스 상태는 건드리지 않는다(사람이 최종
# 선정을 시작하는 시점은 그대로 유지). 실패해도 웹훅 처리는 이미 끝난
# 뒤이므로 로깅만 하고 삼킨다.
_PREWARM_STAGES = {"QUOTATION_COLLECTION", "SUPPLIER_SELECTION"}


def validate_case_quotations(case: dict[str, Any]) -> dict[str, Any]:
    """이 케이스 견적들이 순위에서 어떤 대우를 받는지 즉시 판정한다.

    RunPod 규격 평가나 LangGraph 실행 없이, 결정적 검증을 다시 돌려 견적별로
    - kind: candidate(순위 대상) / parse_failed(견적서 읽기 실패) / rfq_mismatch
    - penalties: 수량 미달·만료·금액 불일치 등 총점에서 빠질 페널티
    - spec_evaluated: AI 규격 평가가 캐시에 있는지
    를 돌려준다. 또한 이메일 첨부를 아예 읽지 못해 Supplier Quotation이
    만들어지지 않은 건(intake_failures)도 함께 돌려줘서, 화면에서 그 협력사가
    '미회신'이 아니라 '견적서 읽기 실패 - 원본 확인'으로 보이게 한다.
    """
    from backend_logic2.nodes.quotation.quotation_filter.get_supplier_quotations import (
        get_reviewable_quotations_for_rfqs,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import (
        load_rfq_requirements,
        review_quotation,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
        classify_review,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import (
        IssueSeverity,
    )

    values = _workflow_values(case)
    current_rfq = str(values.get("rfq_name") or "").strip()
    rfq_names: list[str] = []
    for entry in values.get("rfq_rounds") or []:
        if isinstance(entry, dict):
            name = str(entry.get("rfq_name") or "").strip()
            if name:
                rfq_names.append(name)
    if current_rfq:
        rfq_names.append(current_rfq)
    rfq_names = list(dict.fromkeys(rfq_names))
    if not rfq_names:
        return {"items": [], "intake_failures": []}

    rfq = load_rfq_requirements(current_rfq or rfq_names[-1])
    quotations = get_reviewable_quotations_for_rfqs(rfq_names)
    known = set(rfq_names)
    reviews = [
        review_quotation(quotation, rfq, known_rfq_names=known)
        for quotation in quotations
    ]

    # 규격 평가(RunPod) 결과가 캐시에 있는지 확인한다.
    spec_evaluated: dict[str, bool] = {}
    evaluator_available = True
    evaluation_source: str | None = None
    try:
        from backend_logic2.nodes.quotation.quotation_filter.quotation_spec_evaluator import (
            build_quotation_spec_evaluator,
            specification_evaluation_fingerprint,
        )
        from backend_logic2.repositories.quotation_specification_cache import load_matching

        evaluator = build_quotation_spec_evaluator()
        evaluation_source = evaluator.model_name
        fingerprints = {
            review.quotation_id: specification_evaluation_fingerprint(
                rfq, review.quotation, evaluator
            )
            for review in reviews
            if review.quotation is not None
        }
        cached: dict[str, Any] = {}
        for name in rfq_names:
            try:
                cached.update(load_matching(name, fingerprints, evaluator.model_name))
            except Exception:  # noqa: BLE001
                LOGGER.warning("규격 평가 캐시 조회 실패: rfq=%s", name)
        spec_evaluated = {
            quotation_id: quotation_id in cached for quotation_id in fingerprints
        }
    except Exception:  # noqa: BLE001
        evaluator_available = False
        LOGGER.warning("규격 평가기를 만들 수 없어 평가 여부를 확인하지 못했습니다.", exc_info=True)

    items: list[dict[str, Any]] = []
    responded_suppliers: set[str] = set()
    for review in reviews:
        classified = classify_review(review)
        if review.quotation is not None:
            responded_suppliers.add(str(review.quotation.supplier_id or "").strip())
            responded_suppliers.add(str(review.quotation.supplier_name or "").strip())
        items.append(
            {
                "quotation_id": review.quotation_id,
                "supplier_name": review.supplier_name,
                "status": review.status.value if review.status else None,
                "kind": classified["kind"],
                # 하위 호환: 순위 대상이면 true
                "rankable": classified["kind"] == "candidate",
                "penalties": classified["penalties"],
                "penalty_points": float(sum(item["points"] for item in classified["penalties"])),
                "requires_confirmation": classified["requires_confirmation"],
                "warnings": classified["warnings"],
                "blocking_issues": [
                    {
                        "code": issue.code,
                        "message": issue.message,
                        "evidence": issue.evidence,
                    }
                    for issue in review.issues
                    if issue.severity == IssueSeverity.ERROR
                ],
                "evidence": classified["evidence"],
                "spec_evaluated": (
                    spec_evaluated.get(review.quotation_id) if evaluator_available else None
                ),
                "evaluation_source": evaluation_source,
            }
        )

    intake_failures: list[dict[str, Any]] = []
    try:
        from backend_logic2.repositories import quotation_intake_failures

        for row in quotation_intake_failures.list_failures(rfq_names):
            supplier_keys = {
                str(row.get("supplier_id") or "").strip(),
                str(row.get("supplier_name") or "").strip(),
            } - {""}
            # 같은 협력사가 나중에 제대로 된 견적을 다시 보냈으면 안내하지 않는다.
            if supplier_keys & responded_suppliers:
                continue
            intake_failures.append({
                "rfq_name": row.get("rfq_name"),
                "supplier_id": row.get("supplier_id"),
                "supplier_name": row.get("supplier_name"),
                "source_filename": row.get("source_filename"),
                "failure_kind": row.get("failure_kind"),
                "error": row.get("error"),
                "received_at": row.get("created_at"),
            })
    except Exception:  # noqa: BLE001
        LOGGER.warning("견적 읽기 실패 기록 조회 실패", exc_info=True)

    return {"items": items, "intake_failures": intake_failures}


_LIVE_RANKING_LOCKS: dict[str, Lock] = {}
_LIVE_RANKING_LOCKS_GUARD = Lock()


def _live_ranking_lock(case_id: str) -> Lock:
    with _LIVE_RANKING_LOCKS_GUARD:
        return _LIVE_RANKING_LOCKS.setdefault(case_id, Lock())


def build_live_ranking_payload(
    result: dict[str, Any],
    *,
    rfq_name: str,
    rfq_names: list[str],
    notices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """evaluate_quotations 결과에서 화면에 필요한 부분만 뽑아 저장 형태로 만든다."""
    from datetime import datetime, timezone

    return {
        "rfq_name": rfq_name,
        "rfq_names": list(rfq_names),
        "ranking": list(result.get("ranking") or []),
        "excluded": list(result.get("excluded") or []),
        "parse_failed": list(result.get("parse_failed") or []),
        "competition_count": int(result.get("competition_count") or 0),
        "single_bid": bool(result.get("single_bid")),
        "specification_evaluation": dict(result.get("specification_evaluation") or {}),
        "message": result.get("message") or "",
        "error": result.get("error") or "",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "notices": dict(notices or {}),
    }


def save_live_ranking_from_result(
    case_id: str | None,
    result: dict[str, Any],
    *,
    rfq_name: str,
    rfq_names: list[str],
) -> None:
    """그래프(견적 확인/최종 선정)에서 이미 계산한 결과를 실시간 순위에도 반영한다."""
    if not case_id:
        return
    try:
        case = case_repository.get_case(str(case_id)) or {}
        previous = case.get("live_quotation_ranking") or {}
        notices = previous.get("notices") if isinstance(previous, dict) else None
        case_repository.save_live_quotation_ranking(
            str(case_id),
            build_live_ranking_payload(
                result, rfq_name=rfq_name, rfq_names=rfq_names, notices=notices
            ),
        )
    except Exception:  # noqa: BLE001 - 읽기 모델 저장 실패가 그래프를 멈추면 안 된다
        LOGGER.exception("실시간 견적 순위 저장에 실패했습니다: case_id=%s", case_id)


def _ranking_notice_message(case: dict[str, Any], payload: dict[str, Any], reason: str) -> tuple[str, str]:
    label = case.get("mr_name") or payload.get("rfq_name") or ""
    reason_text = "견적 마감 시각이 지났습니다" if reason == "deadline" else "모든 협력사가 회신했습니다"
    ranking = payload.get("ranking") or []
    parse_failed = payload.get("parse_failed") or []
    if ranking:
        top = ranking[0]
        supplier = top.get("supplier_name") or top.get("supplier") or "-"
        score = top.get("overall_score")
        score_text = f" (종합 {float(score):.1f}점)" if isinstance(score, (int, float)) else ""
        if payload.get("single_bid"):
            detail = f"단독 응찰 {supplier}{score_text} - 수용 또는 재비딩을 결정해 주세요"
        else:
            detail = f"추천 1순위 {supplier}{score_text} · 비교 대상 {len(ranking)}건"
        if parse_failed:
            detail += f" · 읽지 못한 견적 {len(parse_failed)}건"
        return "견적 순위가 준비되었습니다", f"{label} · {reason_text} · {detail}"
    if parse_failed:
        return (
            "견적서를 읽지 못했습니다",
            f"{label} · {reason_text} · 회신 견적 {len(parse_failed)}건 모두 파싱 실패 - 원본 파일을 확인해 주세요",
        )
    return "비교할 견적이 없습니다", f"{label} · {reason_text} · 순위에 오른 견적이 없습니다 - 마감 연장이나 재비딩을 검토해 주세요"


def refresh_live_ranking(
    case_id: str,
    rfq_name: str | None = None,
    *,
    notify_reason: str | None = None,
) -> dict[str, Any] | None:
    """모든 차수 견적으로 순위를 다시 계산해 케이스의 실시간 순위에 저장한다.

    견적이 도착할 때마다(웹훅) 호출된다. 규격 평가는 지문 캐시를 쓰므로
    이미 평가된 견적은 RunPod을 다시 부르지 않는다. 그래프는 건드리지
    않는다 - 최종 선정(견적 Submit)은 여전히 사람이 시작한다.

    전원 회신이면 같은 RFQ 차수에 대해 한 번만 담당자에게 알림을 보낸다.
    (마감 경과는 화면이 마감 시각으로 직접 판단하므로 따로 알리지 않는다.)
    notify_reason으로 알림 사유를 강제로 지정할 수도 있다.
    """
    try:
        with _live_ranking_lock(str(case_id)):
            return _refresh_live_ranking_locked(str(case_id), rfq_name, notify_reason)
    except Exception:  # noqa: BLE001 - 순위 갱신 실패가 웹훅/잡을 실패시키면 안 된다
        LOGGER.exception("실시간 견적 순위 갱신에 실패했습니다: case_id=%s rfq=%s", case_id, rfq_name)
        return None


def _refresh_live_ranking_locked(
    case_id: str,
    rfq_name: str | None,
    notify_reason: str | None,
) -> dict[str, Any] | None:
    from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
        evaluate_quotations_for_rfqs,
    )
    from backend_logic2.policies.repository import for_case
    from backend_logic2.policies.runtime import policy_scope
    from backend_logic2.policies.schema import CompanyPolicy
    from backend_logic2.workflow.process_commands import _rfq_round_map, _rfq_round_names

    case = case_repository.get_case(case_id)
    if case is None:
        return None
    if str(case.get("stage") or "") not in _PREWARM_STAGES:
        return None
    values = _workflow_values(case)
    current_rfq = str(values.get("rfq_name") or rfq_name or "").strip()
    if not current_rfq:
        return None
    round_state = {**values, "rfq_name": current_rfq}
    rfq_names = _rfq_round_names(round_state)

    # 그래프 노드와 같은 규칙(케이스에 고정된 회사 정책 버전)으로 계산한다.
    snapshot = for_case(case_id)
    with policy_scope(CompanyPolicy.model_validate(snapshot["policy"])):
        result = evaluate_quotations_for_rfqs(
            rfq_names,
            current_rfq_name=current_rfq,
            round_by_rfq=_rfq_round_map(round_state),
        )

    previous = case.get("live_quotation_ranking") or {}
    notices = dict(previous.get("notices") or {}) if isinstance(previous, dict) else {}
    payload = build_live_ranking_payload(
        result, rfq_name=current_rfq, rfq_names=rfq_names, notices=notices
    )

    reason = notify_reason
    if reason is None:
        quotation_snapshot = case.get("quotation_snapshot") or {}
        recipients = int(quotation_snapshot.get("recipient_count") or 0)
        responded = int(quotation_snapshot.get("responded_count") or 0)
        if recipients > 0 and responded >= recipients:
            reason = "all_responded"
    should_notify = bool(reason) and notices.get(reason) != current_rfq
    if should_notify:
        payload["notices"][reason] = current_rfq

    case_repository.save_live_quotation_ranking(case_id, payload)

    if should_notify:
        title, message = _ranking_notice_message(case, payload, str(reason))
        ranking = payload.get("ranking") or []
        notification_repository.create_notification(
            case_id=case_id,
            recipient_id=case.get("assigned_user_id"),
            notification_type="QUOTATION_RANKING_READY",
            title=title,
            message=message,
            payload={
                "mr_name": case.get("mr_name"),
                "rfq_name": current_rfq,
                "stage": case.get("stage"),
                "reason": reason,
                "competition_count": payload.get("competition_count"),
                "single_bid": payload.get("single_bid"),
                "parse_failed_count": len(payload.get("parse_failed") or []),
                "top_supplier": (ranking[0].get("supplier_name") or ranking[0].get("supplier")) if ranking else None,
                "top_score": ranking[0].get("overall_score") if ranking else None,
            },
        )
    return payload


def prewarm_specification_analysis(case_id: str, rfq_name: str) -> None:
    """호환용 - 예전 이름. 이제는 실시간 순위까지 함께 계산해 저장한다."""
    refresh_live_ranking(case_id, rfq_name)


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
