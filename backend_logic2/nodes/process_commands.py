"""LangGraph Command wrappers connecting the purchasing nodes end to end.

흐름: MR 조회(이상치검사 없음) -> 대체품 확인 -> 비딩/카탈로그 판정 ->
공급사 확인(기존 있으면 신규탐색 여부 선택, 없으면 자동 신규탐색) ->
RFQ 대상 선택+발송 -> 견적 확인(지금/나중) -> 최종선정 -> PO 발송

기존 node 모듈들(find_substitute, decide_bidding, supplier_search,
register_candidate_suppliers, send_rfq, sq_evaluation, create_and_send_po)이
비즈니스 로직을 그대로 담당함. 이 모듈은 상태 공유, 다음 노드로 라우팅,
interrupt()로 사람 승인/입력 대기 지점을 만드는 역할만 함.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypedDict

from langgraph.graph import END
from langgraph.types import Command, interrupt


class PurchaseProcessState(TypedDict, total=False):
    entrypoint: str
    mr_name: str
    status: str
    substitute_results: dict[str, Any]
    bidding_results: dict[str, Any]
    bidding_items: list[str]
    existing_supplier_candidates: list[dict[str, Any]]
    items_without_existing: list[str]
    supplier_candidates: list[dict[str, Any]]
    supplier_registration_results: list[dict[str, Any]]
    selected_suppliers: list[str]
    send_rfq_email: bool
    submit_rfq: bool
    rfq_name: str
    quotation_ranking: list[dict[str, Any]]
    selected_supplier: str
    po_result: dict[str, Any]
    error: str


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


def _decision_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("decision") or value.get("action")
    return str(value or "").strip().lower()


def route_entrypoint_command(state: PurchaseProcessState) -> Command:
    """일반 시작 또는 특정 단계 복구용 entrypoint 라우팅."""
    entrypoint = state.get("entrypoint", "").strip()
    if entrypoint == "create_rfq":
        return Command(update={"entrypoint": "", "error": ""}, goto="create_rfq")
    if entrypoint == "select_rfq_targets":
        return Command(
            update={"entrypoint": "", "status": "awaiting_supplier_approval", "error": ""},
            goto="select_rfq_targets",
        )
    if entrypoint == "create_po":
        return Command(update={"entrypoint": "", "error": ""}, goto="create_po")
    return Command(update={"entrypoint": "", "status": "checking_substitutes"}, goto="check_substitutes")


def check_substitutes_command(state: PurchaseProcessState) -> Command:
    """[1단계] MR 조회 + 대체품 확인 (이상치검사는 생략)."""
    from backend_logic2.nodes.find_substitute import find_substitutes_for_mr

    mr_name = state["mr_name"]
    substitute_results = find_substitutes_for_mr(mr_name)
    if not substitute_results:
        return Command(
            update={"status": "human_review", "error": "MR을 찾을 수 없거나 품목이 없습니다."},
            goto=END,
        )

    any_substitutes = any(info.get("substitutes") for info in substitute_results.values())
    if not any_substitutes:
        return Command(
            update={"substitute_results": substitute_results, "status": "checking_bidding", "error": ""},
            goto="catalog_or_bidding_choice",
        )

    return Command(
        update={"substitute_results": substitute_results, "status": "awaiting_substitute_approval"},
        goto="substitute_approval",
    )


def substitute_approval_command(state: PurchaseProcessState) -> Command:
    """[1단계-대기] 대체품 있으면 여기서 멈춰서 approve(대체품 재고 사용) /
    reject(대체품 안 쓰고 신규구매 계속 진행) 물어봄."""
    answer = interrupt({
        "type": "substitute_approval",
        "mr_name": state["mr_name"],
        "substitute_results": state.get("substitute_results", {}),
        "allowed": ["approve", "reject"],
    })
    decision = _decision_value(answer)
    if decision not in ("approve", "reject"):
        return Command(
            update={"status": "awaiting_substitute_approval", "error": "decision은 approve 또는 reject여야 합니다."},
            goto="substitute_approval",
        )
    if decision == "approve":
        return Command(update={"status": "substitute_used_end"}, goto=END)

    return Command(
        update={"status": "checking_bidding", "error": ""},
        goto="catalog_or_bidding_choice",
    )


def catalog_or_bidding_choice_command(state: PurchaseProcessState) -> Command:
    """[2단계] decide_bidding으로 판정. 비딩 필요 품목 있으면 바로 진행,
    없으면(전부 카탈로그 추천) 사람에게 물어봄."""
    from backend_logic2.nodes.decide_bidding import decide_bidding

    mr_name = state["mr_name"]
    bidding_results = decide_bidding(mr_name)
    bidding_items = [code for code, info in bidding_results.items() if info["needs_bidding"]]

    if bidding_items:
        return Command(
            update={
                "bidding_results": bidding_results,
                "bidding_items": bidding_items,
                "status": "resolving_suppliers",
            },
            goto="resolve_suppliers_choice",
        )

    return Command(
        update={"bidding_results": bidding_results, "status": "awaiting_catalog_or_bidding_choice"},
        goto="catalog_or_bidding_interrupt",
    )


def catalog_or_bidding_interrupt_command(state: PurchaseProcessState) -> Command:
    """[2단계-대기] 카탈로그 추천됨 - 카탈로그로 할지 그래도 비딩할지 물어봄."""
    answer = interrupt({
        "type": "catalog_or_bidding_choice",
        "mr_name": state["mr_name"],
        "bidding_results": state.get("bidding_results", {}),
        "message": "전 품목이 카탈로그 방식이 추천됩니다.",
        "allowed": ["catalog", "bidding"],
    })
    choice = _decision_value(answer)
    if choice not in ("catalog", "bidding"):
        return Command(
            update={"status": "awaiting_catalog_or_bidding_choice", "error": "catalog 또는 bidding을 선택하세요."},
            goto="catalog_or_bidding_interrupt",
        )
    if choice == "catalog":
        return Command(update={"status": "catalog_purchase_required"}, goto=END)

    bidding_items = list(state.get("bidding_results", {}).keys())
    return Command(
        update={"bidding_items": bidding_items, "status": "resolving_suppliers", "error": ""},
        goto="resolve_suppliers_choice",
    )


def _search_new_suppliers(item_codes: list[str]) -> list[dict]:
    """item_code 목록에 대해 supplier_search로 신규 공급사 탐색, 이름기준 중복제거."""
    from backend_logic2.erp_client import erp_get_one
    from backend_logic2.nodes.supplier_search import supplier_search

    candidates_by_name: dict[str, dict] = {}
    for item_code in item_codes:
        item = erp_get_one("Item", item_code) or {}
        item_name = item.get("item_name") or item_code
        searched = supplier_search(item_name, target_count=10)
        for c in searched:
            name = str(c.get("name") or "").strip()
            if name and name not in candidates_by_name:
                candidates_by_name[name] = {**c, "name": name}
    return list(candidates_by_name.values())


def resolve_suppliers_command(state: PurchaseProcessState) -> Command:
    """[3단계] 비딩 대상 품목들의 기존(ERPNext) 공급사 확인.
    있으면 사람에게 물어봄, 없으면 자동으로 신규탐색."""
    from backend_logic2.erp_client import erp_get_one

    bidding_items = state.get("bidding_items", [])
    existing_candidates: dict[str, dict] = {}
    items_without_existing = []

    for item_code in bidding_items:
        item = erp_get_one("Item", item_code) or {}
        existing_names = [
            s.get("supplier") or s.get("name")
            for s in item.get("supplier_items", [])
        ]
        existing_names = [n for n in existing_names if n]

        if existing_names:
            for name in existing_names:
                supplier_doc = erp_get_one("Supplier", name) or {}
                existing_candidates[name] = {
                    "name": name,
                    "email": supplier_doc.get("email_id"),
                    "phone": supplier_doc.get("mobile_no") or supplier_doc.get("phone"),
                    "source": "erpnext",
                }
        else:
            items_without_existing.append(item_code)

    if existing_candidates:
        return Command(
            update={
                "existing_supplier_candidates": list(existing_candidates.values()),
                "items_without_existing": items_without_existing,
                "status": "awaiting_supplier_source_choice",
            },
            goto="supplier_source_choice",
        )

    candidates = _search_new_suppliers(bidding_items)
    if not candidates:
        return Command(
            update={"status": "human_review", "error": "공급사 후보를 확보하지 못했습니다."},
            goto=END,
        )
    return Command(
        update={
            "supplier_candidates": sorted(candidates, key=lambda row: row["name"]),
            "status": "awaiting_supplier_approval",
        },
        goto="select_rfq_targets",
    )


def supplier_source_choice_command(state: PurchaseProcessState) -> Command:
    """[3단계-대기] 기존 공급사 있음 - 신규탐색 추가할지 vs 기존만 쓸지 물어봄."""
    answer = interrupt({
        "type": "supplier_source_choice",
        "mr_name": state["mr_name"],
        "existing_suppliers": state.get("existing_supplier_candidates", []),
        "allowed": ["search", "existing_only"],
    })
    choice = _decision_value(answer)
    if choice not in ("search", "existing_only"):
        return Command(
            update={"status": "awaiting_supplier_source_choice", "error": "search 또는 existing_only를 선택하세요."},
            goto="supplier_source_choice",
        )

    existing = state.get("existing_supplier_candidates", [])
    candidates_by_name = {c["name"]: c for c in existing}

    if choice == "search":
        new_ones = _search_new_suppliers(state.get("items_without_existing") or state.get("bidding_items", []))
        for c in new_ones:
            if c["name"] not in candidates_by_name:
                candidates_by_name[c["name"]] = c

    return Command(
        update={
            "supplier_candidates": sorted(candidates_by_name.values(), key=lambda row: row["name"]),
            "status": "awaiting_supplier_approval",
            "error": "",
        },
        goto="select_rfq_targets",
    )


def select_rfq_targets_command(state: PurchaseProcessState) -> Command:
    """[4단계-대기] RFQ 보낼 대상 선택(전체승인 or 번호로 선택, 이메일 보완,
    후보 제외 가능), 선택 확정되면 Supplier로 등록."""
    raw_candidates = state.get("supplier_candidates", [])
    candidates = [
        dict(candidate) if isinstance(candidate, dict) else {"name": str(candidate), "registered": True}
        for candidate in raw_candidates
    ]
    names = [candidate.get("name") for candidate in candidates if candidate.get("name")]
    answer = interrupt({
        "type": "select_rfq_targets",
        "mr_name": state["mr_name"],
        "candidates": candidates,
        "missing_email": [row["name"] for row in candidates if not row.get("email")],
        "input_schema": {
            "suppliers": ["선택할 업체명"],
            "supplier_updates": [{"name": "업체명", "email": "contact@example.com"}],
            "dismiss": ["제외할 업체명"],
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
            goto="select_rfq_targets",
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
            goto="select_rfq_targets",
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
            goto="select_rfq_targets",
        )
    selected = [row["name"] for row in registrations]
    return Command(
        update={
            "selected_suppliers": selected,
            "supplier_candidates": candidates,
            "supplier_registration_results": registrations,
            "send_rfq_email": True,
            "submit_rfq": True,
            "status": "creating_rfq",
            "error": "",
        },
        goto="create_rfq",
    )


def create_rfq_command(state: PurchaseProcessState) -> Command:
    """[5단계] 선택된 공급사한테 RFQ 생성+발송."""
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
        return Command(update={"rfq_name": rfq["name"], "status": "rfq_draft_created"}, goto=END)

    return Command(
        update={"rfq_name": rfq["name"], "status": "awaiting_quotation_check", "error": ""},
        goto="check_quotations",
    )


def check_quotations_command(state: PurchaseProcessState) -> Command:
    """[6단계-대기] 지금 견적 확인할지 물어봄. 나중에 확인하려면 그냥 종료 -
    resume으로 이 노드에 다시 들어오면 재확인 가능."""
    answer = interrupt({
        "type": "check_quotations",
        "rfq_name": state["rfq_name"],
        "message": "제출된 견적을 지금 확인하시겠습니까?",
        "allowed": ["check", "later"],
    })
    choice = _decision_value(answer)
    if choice not in ("check", "later"):
        return Command(
            update={"status": "awaiting_quotation_check", "error": "check 또는 later를 선택하세요."},
            goto="check_quotations",
        )
    if choice == "later":
        return Command(update={"status": "awaiting_quotation_check"}, goto=END)

    from backend_logic2.nodes.sq_evaluation import evaluate_quotations

    result = evaluate_quotations(state["rfq_name"])
    if result.get("error") or result.get("message") or not result.get("ranking"):
        return Command(
            update={
                "quotation_ranking": [],
                "status": "awaiting_quotation_check",
                "error": result.get("message") or result.get("error") or "제출된 견적이 없습니다.",
            },
            goto=END,
        )

    return Command(
        update={"quotation_ranking": result["ranking"], "status": "awaiting_final_selection", "error": ""},
        goto="final_selection",
    )


def final_selection_command(state: PurchaseProcessState) -> Command:
    """[7단계-대기] 순위목록 보여주고 최종 공급사 선정 물어봄."""
    ranking = state.get("quotation_ranking", [])
    answer = interrupt({
        "type": "final_selection",
        "rfq_name": state["rfq_name"],
        "ranking": ranking,
    })
    supplier = answer.get("supplier") if isinstance(answer, dict) else str(answer or "").strip()
    valid_suppliers = {r.get("supplier") for r in ranking}
    if supplier not in valid_suppliers:
        return Command(
            update={"status": "awaiting_final_selection", "error": "순위 목록의 supplier를 선택하세요."},
            goto="final_selection",
        )

    return Command(
        update={"selected_supplier": supplier, "status": "creating_po", "error": ""},
        goto="create_po",
    )


def create_po_command(state: PurchaseProcessState) -> Command:
    """[7단계-마무리] PO 생성+발송. create_and_send_po가 에러시 sys.exit(1)을
    쓰므로(라이브러리 성격상 CLI 단독실행 전제로 짜여있어서), 그래프 전체가
    죽지 않도록 SystemExit을 잡아서 처리."""
    from backend_logic2.nodes.create_and_send_po import create_and_send_po

    try:
        result = create_and_send_po(state["rfq_name"], state["selected_supplier"], send_email=True)
    except SystemExit as exc:
        return Command(update={"status": "human_review", "error": f"PO 처리 중단(exit={exc.code})"}, goto=END)

    return Command(update={"po_result": result or {}, "status": "completed"}, goto=END)