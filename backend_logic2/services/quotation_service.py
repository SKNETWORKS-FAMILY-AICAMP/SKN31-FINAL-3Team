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


def validate_case_quotations(case: dict[str, Any]) -> list[dict[str, Any]]:
    """이 케이스의 견적들이 "순위에 들어갈 수 있는 상태인지"를 즉시 판정한다.

    RunPod 규격 평가나 LangGraph 실행 없이, 결정적 검증(수량 충족, 수량x단가
    = 금액, 공급가액/총액/세액 정합, 유효기간, 사업자번호, RFQ 품목 연결,
    납기 비교 가능 여부)만 다시 돌려서 견적별 차단 사유를 돌려준다.

    화면에서 "회신은 왔는데 순위에 없는 견적"이 (1) 아직 AI 평가가 안 끝난
    것인지 (2) 애초에 검증에서 탈락해 아무리 다시 분석해도 안 바뀌는
    것인지 구분하기 위한 조회용이다 - 예전에는 순위가 통째로 비었을 때만
    오류 문구에 사유가 붙어서, 일부만 제외된 경우 원인을 알 수 없었다.
    """
    from backend_logic2.nodes.quotation.quotation_filter.get_supplier_quotations import (
        get_reviewable_quotations_for_rfqs,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import (
        load_rfq_requirements,
        review_quotation,
    )
    # 순위 진입 가능 여부 판정은 ranker와 같은 규칙을 써야 화면과 실제
    # 결과가 어긋나지 않는다(규격 관련 이슈는 AI에 위임되어 차단 사유가
    # 아니라는 규칙 포함).
    from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
        _is_structurally_rankable,
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
        return []

    rfq = load_rfq_requirements(current_rfq or rfq_names[-1])
    quotations = get_reviewable_quotations_for_rfqs(rfq_names)
    known = set(rfq_names)

    items: list[dict[str, Any]] = []
    for quotation in quotations:
        review = review_quotation(quotation, rfq, known_rfq_names=known)
        rankable, evidence = _is_structurally_rankable(review)
        items.append(
            {
                "quotation_id": review.quotation_id,
                "supplier_name": review.supplier_name,
                "status": review.status.value if review.status else None,
                "rankable": rankable,
                "blocking_issues": [
                    {
                        "code": issue.code,
                        "message": issue.message,
                        "evidence": issue.evidence,
                    }
                    for issue in review.issues
                    if issue.severity == IssueSeverity.ERROR
                ],
                "evidence": evidence,
            }
        )
    return items


def prewarm_specification_analysis(case_id: str, rfq_name: str) -> None:
    """도착한 견적의 규격 평가를 미리 계산해 캐시에 넣어둔다(결과는 버린다)."""
    try:
        case = case_repository.get_case(case_id)
        if case is None:
            return
        stage = str(case.get("stage") or "")
        if stage not in _PREWARM_STAGES:
            return
        from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
            evaluate_quotations,
        )

        result = evaluate_quotations(rfq_name)
        error = result.get("error") if isinstance(result, dict) else None
        if error:
            LOGGER.info(
                "견적 규격 평가 미리 실행이 완료되지 않았습니다(다음 분석에서 재시도): "
                "case_id=%s rfq=%s error=%s",
                case_id,
                rfq_name,
                error,
            )
    except Exception:  # noqa: BLE001 - 미리 실행 실패가 웹훅 처리를 되돌리면 안 된다
        LOGGER.exception(
            "견적 규격 평가 미리 실행에 실패했습니다: case_id=%s rfq=%s",
            case_id,
            rfq_name,
        )


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
