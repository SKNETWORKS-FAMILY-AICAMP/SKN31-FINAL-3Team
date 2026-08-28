"""LangGraph Command wrappers connecting the purchasing nodes end to end.

The existing node modules remain the owners of business logic.  This module
only keeps shared state, routes to the next node, and creates explicit human
approval/deadline pauses with ``interrupt()``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END
from langgraph.types import Command, interrupt


class PurchaseProcessState(TypedDict, total=False):
    entrypoint: str
    mr_name: str
    status: str
    anomaly_result: dict[str, Any]
    substitute_results: dict[str, Any]
    approval_result: dict[str, Any]
    bidding_results: dict[str, Any]
    supplier_candidates: list[dict[str, Any]]
    supplier_search_results: dict[str, Any]
    supplier_registration_results: list[dict[str, Any]]
    selected_suppliers: list[str]
    send_rfq_email: bool
    submit_rfq: bool
    rfq_name: str
    quotation_deadline: str
    quotation_manifest: dict[str, Any]
    quotation_result: dict[str, Any]
    selected_supplier: str
    send_po_email: bool
    po_result: dict[str, Any]
    error: str
    pending_items: list[dict[str, Any]]


def to_checkpoint_data(value: Any) -> Any:
    """Convert domain models into stable JSON-like checkpoint values."""
    if hasattr(value, "model_dump"):
        return to_checkpoint_data(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): to_checkpoint_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_checkpoint_data(item) for item in value]
    if isinstance(value, Enum):
        return to_checkpoint_data(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def route_entrypoint_command(state: PurchaseProcessState) -> Command:
    """Route a normal start or resume directly from an explicitly selected stage."""
    entrypoint = state.get("entrypoint", "").strip()
    if entrypoint == "create_rfq":
        return Command(update={"entrypoint": "", "error": ""}, goto="create_rfq")
    if entrypoint == "create_po":
        return Command(update={"entrypoint": "", "error": ""}, goto="create_po")
    if entrypoint == "supplier_approval":
        return Command(
            update={"entrypoint": "", "status": "awaiting_supplier_approval", "error": ""},
            goto="supplier_approval",
        )
    return Command(
        update={"entrypoint": "", "status": "inspecting_mr"},
        goto="inspect_mr",
    )


def check_item_approval_command(state: PurchaseProcessState) -> Command:
    """Legacy guard for disabled Items; new Item approval happens before MR creation."""
    from backend_logic2.erp_client import erp_get_one

    mr = erp_get_one("Material Request", state["mr_name"])
    if not mr:
        return Command(
            update={"status": "human_review", "error": "Material Request를 찾을 수 없습니다."},
            goto=END,
        )
    pending: list[dict[str, Any]] = []
    for row in mr.get("items", []):
        item_code = row.get("item_code")
        item = erp_get_one("Item", item_code) if item_code else None
        if item and int(item.get("disabled") or 0) == 1:
            pending.append({
                "item_code": item_code,
                "item_name": item.get("item_name") or item_code,
                "description": item.get("description"),
            })
    if pending:
        return Command(
            update={"pending_items": pending, "status": "awaiting_item_approval"},
            goto="item_approval",
        )
    return Command(update={"pending_items": [], "status": "inspecting_mr"}, goto="inspect_mr")


def item_approval_command(state: PurchaseProcessState) -> Command:
    answer = interrupt({
        "type": "new_item_approval",
        "mr_name": state["mr_name"],
        "items": state.get("pending_items", []),
        "allowed": ["approve", "reject"],
    })
    decision = _decision_value(answer)
    if decision == "reject":
        return Command(update={"status": "item_approval_rejected"}, goto=END)
    if decision != "approve":
        return Command(
            update={"status": "awaiting_item_approval", "error": "decision은 approve 또는 reject여야 합니다."},
            goto="item_approval",
        )

    from backend_logic2.nodes.item_validation import approve_item_request

    approved = []
    for item in state.get("pending_items", []):
        approved.append(approve_item_request(item["item_code"]))
    return Command(
        update={"pending_items": [], "status": "inspecting_mr", "error": ""},
        goto="inspect_mr",
    )


def _decision_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("decision") or value.get("action")
    return str(value or "").strip().lower()


def inspect_mr_command(state: PurchaseProcessState) -> Command:
    """Run deterministic MR anomaly detection and route to substitute review."""
    from backend_logic2.nodes.anomaly_detection import detect_material_request_anomalies

    mr_name = state["mr_name"]
    anomaly = detect_material_request_anomalies(mr_name)
    if anomaly.get("has_anomaly"):
        return Command(
            update={"anomaly_result": anomaly, "status": "awaiting_mr_approval"},
            goto="mr_approval",
        )
    return Command(
        update={"anomaly_result": anomaly, "status": "checking_substitutes"},
        goto="find_substitutes",
    )


def find_substitutes_command(state: PurchaseProcessState) -> Command:
    from backend_logic2.nodes.find_substitute import find_substitutes_for_mr

    substitutes = find_substitutes_for_mr(state["mr_name"])
    return Command(
        update={"substitute_results": substitutes, "status": "awaiting_mr_approval"},
        goto="mr_approval",
    )


def mr_approval_command(state: PurchaseProcessState) -> Command:
    """Pause before the ERP MR Submit/Discard mutation."""
    answer = interrupt({
        "type": "mr_approval",
        "mr_name": state["mr_name"],
        "anomaly_result": state.get("anomaly_result", {}),
        "substitute_results": state.get("substitute_results", {}),
        "allowed": ["approve", "reject"],
    })
    decision = _decision_value(answer)
    if decision not in {"approve", "reject"}:
        return Command(
            update={"status": "awaiting_mr_approval", "error": "decision은 approve 또는 reject여야 합니다."},
            goto="mr_approval",
        )
    rejection_reason = answer.get("reason") if isinstance(answer, dict) else None
    if decision == "reject" and not str(rejection_reason or "").strip():
        return Command(
            update={"status": "awaiting_mr_approval", "error": "반려 시 reason이 필요합니다."},
            goto="mr_approval",
        )

    from backend_logic2.nodes.approval_review import review_material_request

    review = review_material_request(
        state["mr_name"],
        anomaly_result=state.get("anomaly_result"),
        substitute_results=state.get("substitute_results"),
        decision=decision,
        rejection_reason=rejection_reason,
    )
    if review["decision"] == "rejected":
        return Command(update={"approval_result": review, "status": "rejected"}, goto=END)
    return Command(
        update={"approval_result": review, "status": "deciding_bidding", "error": ""},
        goto="decide_bidding",
    )


def decide_bidding_command(state: PurchaseProcessState) -> Command:
    from backend_logic2.nodes.decide_bidding import decide_bidding

    results = decide_bidding(state["mr_name"])
    bidding_items = [code for code, row in results.items() if row.get("needs_bidding")]
    if not bidding_items:
        return Command(
            update={"bidding_results": results, "status": "catalog_purchase_required"},
            goto=END,
        )
    return Command(
        update={"bidding_results": results, "status": "resolving_suppliers"},
        goto="resolve_suppliers",
    )


def resolve_suppliers_command(state: PurchaseProcessState) -> Command:
    """Collect candidates without creating Supplier documents before human approval."""
    from backend_logic2.erp_client import erp_get_one
    from backend_logic2.nodes.supplier_search import supplier_search

    mr = erp_get_one("Material Request", state["mr_name"])
    if not mr:
        return Command(
            update={"status": "human_review", "error": "Material Request를 찾을 수 없습니다."},
            goto=END,
        )

    bidding = state.get("bidding_results", {})
    candidates_by_name: dict[str, dict[str, Any]] = {}
    search_results: dict[str, Any] = {}
    for row in mr.get("items", []):
        item_code = row.get("item_code")
        if not item_code or not bidding.get(item_code, {}).get("needs_bidding"):
            continue
        item = erp_get_one("Item", item_code) or {}
        existing = [
            supplier.get("supplier") or supplier.get("name")
            for supplier in item.get("supplier_items", [])
        ]
        existing = [name for name in existing if name]
        if existing:
            existing_candidates = []
            for name in existing:
                supplier_doc = erp_get_one("Supplier", name) or {}
                candidate = {
                    "name": name,
                    "email": supplier_doc.get("email_id"),
                    "phone": supplier_doc.get("mobile_no") or supplier_doc.get("phone"),
                    "source": "erpnext",
                    "registered": True,
                    "item_codes": [item_code],
                }
                candidates_by_name[name] = candidate
                existing_candidates.append(candidate)
            search_results[item_code] = {
                "source": "existing",
                "candidates": existing_candidates,
            }
            continue

        searched = supplier_search(item.get("item_name") or item_code, target_count=10)
        normalized = []
        for candidate in searched:
            name = str(candidate.get("name") or "").strip()
            if not name:
                continue
            record = dict(candidate)
            record.update({"name": name, "registered": False, "item_codes": [item_code]})
            if name in candidates_by_name:
                item_codes = candidates_by_name[name].setdefault("item_codes", [])
                if item_code not in item_codes:
                    item_codes.append(item_code)
            else:
                candidates_by_name[name] = record
            normalized.append(record)
        search_results[item_code] = {
            "source": "search",
            "candidates": normalized,
        }

    candidates = list(candidates_by_name.values())
    if not candidates:
        return Command(
            update={
                "supplier_search_results": search_results,
                "status": "human_review",
                "error": "RFQ를 보낼 공급사를 확보하지 못했습니다.",
            },
            goto=END,
        )
    return Command(
        update={
            "supplier_candidates": sorted(candidates, key=lambda row: row["name"]),
            "supplier_search_results": search_results,
            "status": "awaiting_supplier_approval",
        },
        goto="supplier_approval",
    )


def supplier_approval_command(state: PurchaseProcessState) -> Command:
    """Enrich/select candidates, then register only the selected suppliers."""
    raw_candidates = state.get("supplier_candidates", [])
    candidates = [
        dict(candidate) if isinstance(candidate, dict) else {"name": str(candidate), "registered": True}
        for candidate in raw_candidates
    ]
    names = [candidate.get("name") for candidate in candidates if candidate.get("name")]
    answer = interrupt({
        "type": "supplier_approval",
        "mr_name": state["mr_name"],
        "candidates": candidates,
        "missing_email": [row["name"] for row in candidates if not row.get("email")],
        "input_schema": {
            "suppliers": ["선택할 업체명"],
            "supplier_updates": [{"name": "업체명", "email": "contact@example.com"}],
            "dismiss": ["제외할 업체명"],
        },
        "options": {
            "default": "RFQ 생성·Submit·메일 발송",
            "send_email=false": "RFQ 생성·Submit, 메일 발송 안 함",
            "submit=false": "RFQ Draft만 생성, Submit·메일 발송 안 함",
        },
    })
    if not isinstance(answer, dict):
        answer = {"action": _decision_value(answer)}

    updates = answer.get("supplier_updates") or []
    if isinstance(updates, dict):
        updates = [{"name": name, **(value if isinstance(value, dict) else {"email": value})}
                   for name, value in updates.items()]
    updates_by_name = {
        str(update.get("name") or "").strip(): update
        for update in updates
        if isinstance(update, dict) and str(update.get("name") or "").strip()
    }
    for candidate in candidates:
        if candidate.get("name") in updates_by_name:
            candidate.update(updates_by_name[candidate["name"]])

    dismissed = {str(name).strip() for name in answer.get("dismiss", []) if str(name).strip()}
    if isinstance(answer, dict):
        if _decision_value(answer) == "approve_all":
            selected = [name for name in names if name not in dismissed]
        else:
            selected = answer.get("suppliers", [])
    selected = list(dict.fromkeys(str(name).strip() for name in selected if str(name).strip()))
    invalid = sorted((set(selected) - set(names)) | (set(selected) & dismissed))
    if not selected or invalid:
        return Command(
            update={
                "supplier_candidates": candidates,
                "status": "awaiting_supplier_approval",
                "error": f"올바른 공급사를 선택하세요. invalid={invalid}",
            },
            goto="supplier_approval",
        )
    selected_candidates = [row for row in candidates if row.get("name") in selected]
    missing_email = [row["name"] for row in selected_candidates if not str(row.get("email") or "").strip()]
    if missing_email:
        return Command(
            update={
                "supplier_candidates": candidates,
                "status": "awaiting_supplier_approval",
                "error": f"이메일을 입력하거나 dismiss 하세요: {missing_email}",
            },
            goto="supplier_approval",
        )

    from backend_logic2.nodes.register_candidate_suppliers import register_candidate_suppliers

    registrations = register_candidate_suppliers(selected_candidates)
    failed = [row for row in registrations if row.get("status") == "failed"]
    if failed:
        return Command(
            update={
                "supplier_candidates": candidates,
                "supplier_registration_results": registrations,
                "status": "awaiting_supplier_approval",
                "error": f"Supplier 등록 실패: {failed}",
            },
            goto="supplier_approval",
        )
    selected = [row["name"] for row in registrations]
    send_email = bool(answer.get("send_email", True)) if isinstance(answer, dict) else True
    submit_rfq = bool(answer.get("submit", True)) if isinstance(answer, dict) else True
    if not submit_rfq:
        send_email = False
    return Command(
        update={
            "selected_suppliers": selected,
            "supplier_candidates": candidates,
            "supplier_registration_results": registrations,
            "send_rfq_email": send_email,
            "submit_rfq": submit_rfq,
            "status": "creating_rfq",
            "error": "",
        },
        goto="create_rfq",
    )


def create_rfq_command(state: PurchaseProcessState) -> Command:
    from backend_logic2.nodes.send_rfq import create_and_send_rfq

    rfq = create_and_send_rfq(
        state["mr_name"],
        state["selected_suppliers"],
        send_email=state.get("send_rfq_email", True),
        submit=state.get("submit_rfq", True),
    )
    if not rfq or not rfq.get("name"):
        return Command(
            update={"status": "human_review", "error": "RFQ 생성 또는 발송에 실패했습니다."},
            goto=END,
        )
    if not state.get("submit_rfq", True):
        return Command(
            update={"rfq_name": rfq["name"], "status": "rfq_draft_created"},
            goto=END,
        )
    return Command(
        update={"rfq_name": rfq["name"], "status": "waiting_for_quotations"},
        goto="quotation_deadline",
    )


def quotation_deadline_command(state: PurchaseProcessState) -> Command:
    """Pause across the multi-day quotation collection period."""
    answer = interrupt({
        "type": "quotation_deadline",
        "rfq_name": state["rfq_name"],
        "quotation_deadline": state.get("quotation_deadline"),
        "instructions": "JSON manifest를 쓰거나 quotation-file과 quotation-supplier로 재개하세요.",
        "example": {
            "action": "process",
            "manifest": {"quotations": [{"path": "C:/quotes/vendor.png", "supplier_name": "공급사명"}]},
            "top_k": 3,
        },
    })
    if not isinstance(answer, dict) or _decision_value(answer) != "process":
        return Command(
            update={"status": "waiting_for_quotations", "error": "action=process가 필요합니다."},
            goto="quotation_deadline",
        )

    if answer.get("manifest_path"):
        from backend_logic2.nodes.quotation_filter.quotation_models import load_json

        manifest_path = Path(answer["manifest_path"])
        if manifest_path.suffix.lower() != ".json":
            return Command(
                update={
                    "status": "waiting_for_quotations",
                    "error": "manifest_path에는 JSON manifest만 지정할 수 있습니다. "
                             "이미지는 --quotation-file과 --quotation-supplier로 입력하세요.",
                },
                goto="quotation_deadline",
            )
        try:
            manifest = load_json(manifest_path)
        except (OSError, UnicodeError, ValueError) as exc:
            return Command(
                update={
                    "status": "waiting_for_quotations",
                    "error": f"견적 manifest를 읽을 수 없습니다: {exc}",
                },
                goto="quotation_deadline",
            )
    else:
        manifest = answer.get("manifest") or {"quotations": []}

    if not isinstance(manifest, dict) or not isinstance(manifest.get("quotations", []), list):
        return Command(
            update={
                "status": "waiting_for_quotations",
                "error": "manifest는 quotations 배열을 가진 JSON 객체여야 합니다.",
            },
            goto="quotation_deadline",
        )
    missing_supplier = [
        source.get("path", "(경로 없음)")
        for source in manifest.get("quotations", [])
        if source.get("channel") != "portal"
        and source.get("path")
        and not str(source.get("supplier_name") or source.get("supplier_id") or "").strip()
    ]
    if missing_supplier:
        return Command(
            update={
                "status": "waiting_for_quotations",
                "error": f"외부 견적의 공급사명이 필요합니다: {missing_supplier}",
            },
            goto="quotation_deadline",
        )

    from backend_logic2.nodes.quotation_filter.quotation_pipeline import run_pipeline
    from backend_logic2.nodes.quotation_filter.quotation_reviewer import load_rfq_requirements

    rfq = load_rfq_requirements(state["rfq_name"])
    result = to_checkpoint_data(
        run_pipeline(manifest, rfq, top_k=int(answer.get("top_k") or 3))
    )
    return Command(
        update={
            "quotation_manifest": manifest,
            "quotation_result": result,
            "status": "awaiting_final_selection",
            "error": "",
        },
        goto="final_selection",
    )


def final_selection_command(state: PurchaseProcessState) -> Command:
    ranking = state.get("quotation_result", {}).get("ranking")
    if hasattr(ranking, "model_dump"):
        ranking_payload = ranking.model_dump(mode="json")
    else:
        ranking_payload = ranking or {}
    recommended = ranking_payload.get("recommended", [])
    if not recommended:
        return Command(
            update={"status": "human_review", "error": "선정 가능한 검토 통과 견적이 없습니다."},
            goto=END,
        )

    answer = interrupt({
        "type": "final_supplier_selection",
        "rfq_name": state["rfq_name"],
        "recommended": recommended,
        "warning": "선택하면 Purchase Order 생성·제출 단계로 진행합니다.",
    })
    supplier = answer.get("supplier") if isinstance(answer, dict) else str(answer or "").strip()
    allowed = {
        row.get("supplier_id") or row.get("supplier_name")
        for row in recommended
        if row.get("supplier_id") or row.get("supplier_name")
    }
    if supplier not in allowed:
        return Command(
            update={"status": "awaiting_final_selection", "error": "추천 목록의 supplier를 선택하세요."},
            goto="final_selection",
        )
    send_po_email = bool(answer.get("send_email", True)) if isinstance(answer, dict) else True
    return Command(
        update={
            "selected_supplier": supplier,
            "send_po_email": send_po_email,
            "status": "creating_po",
            "error": "",
        },
        goto="create_po",
    )


def create_po_command(state: PurchaseProcessState) -> Command:
    from backend_logic2.nodes.create_and_send_po import create_and_send_po

    try:
        result = create_and_send_po(
            state["rfq_name"],
            state["selected_supplier"],
            send_email=state.get("send_po_email", True),
        )
    except SystemExit as exc:
        return Command(
            update={"status": "human_review", "error": f"PO 처리 중단(exit={exc.code})"},
            goto=END,
        )
    return Command(
        update={"po_result": result or {}, "status": "completed"},
        goto=END,
    )
