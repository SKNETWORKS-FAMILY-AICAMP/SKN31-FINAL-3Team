"""LangGraph Command wrappers connecting the purchasing nodes end to end.

주의: 처음부터 다시 짜는 중 - 아래 2단계까지 구현됨.
  [1단계] check_mr_item + substitute_selection - 대체품 확인, 있으면
    사람이 목록에서 고르거나 신규구매 선택 (HITL)
  [2단계] decide_bidding_choice - 비딩/카탈로그 자동판정 (decide_bidding.py
    기존 규칙 그대로 신뢰, 사람 개입 없음)

나머지 단계(공급사탐색, RFQ, 견적, PO)는 이어서 추가할 예정.
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
    selected_substitute: str
    bidding_results: dict[str, Any]
    bidding_items: list[str]
    existing_supplier_candidates: list[dict[str, Any]]
    supplier_candidates: list[dict[str, Any]]
    supplier_registration_results: list[dict[str, Any]]
    selected_suppliers: list[str]
    rfq_name: str
    quotation_ranking: list[dict[str, Any]]
    selected_supplier: str
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
    """일반 시작 라우팅. 나중에 단계 추가되면 entrypoint별 분기 추가."""
    return Command(update={"entrypoint": "", "status": "checking_mr_item"}, goto="check_mr_item")


def check_mr_item_command(state: PurchaseProcessState) -> Command:
    """[1단계] MR 품목별로 대체품 존재여부 확인.
    있으면 substitute_selection(사람이 고름)으로, 없으면 바로 비딩판정으로."""
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.nodes.mr.find_substitute import find_substitutes_for_mr

    mr_name = state["mr_name"]
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return Command(
            update={"status": "human_review", "error": "MR을 찾을 수 없습니다."},
            goto=END,
        )

    substitute_results = find_substitutes_for_mr(mr_name)
    any_substitutes = any(info.get("substitutes") for info in substitute_results.values())

    if any_substitutes:
        return Command(
            update={"substitute_results": substitute_results, "status": "awaiting_substitute_selection"},
            goto="substitute_selection",
        )

    return Command(
        update={"substitute_results": substitute_results, "status": "checking_bidding"},
        goto="decide_bidding_choice",
    )


def substitute_selection_command(state: PurchaseProcessState) -> Command:
    """[1단계-대기] 대체품 목록을 보여주고, 사람이 하나 선택하거나
    'new_purchase'로 신규구매 진행을 선택하게 함."""
    substitute_results = state.get("substitute_results", {})

    all_substitutes = []
    for item_code, info in substitute_results.items():
        for sub in info.get("substitutes", []):
            all_substitutes.append({**sub, "original_item_code": item_code})

    answer = interrupt({
        "type": "substitute_selection",
        "mr_name": state["mr_name"],
        "substitute_results": substitute_results,
        "instructions": "대체품 중 하나를 item_code로 선택하거나, "
                         "'new_purchase'로 신규구매를 진행하세요.",
        "allowed_item_codes": [s["item_code"] for s in all_substitutes],
    })

    choice = answer.get("item_code") if isinstance(answer, dict) else str(answer or "").strip()
    if _decision_value(answer) == "new_purchase" or choice == "new_purchase":
        return Command(
            update={"status": "checking_bidding", "error": ""},
            goto="decide_bidding_choice",
        )

    valid_codes = {s["item_code"] for s in all_substitutes}
    if choice not in valid_codes:
        return Command(
            update={
                "status": "awaiting_substitute_selection",
                "error": "유효한 대체품 item_code 또는 'new_purchase'를 선택하세요.",
            },
            goto="substitute_selection",
        )

    return Command(
        update={"status": "substitute_selected", "selected_substitute": choice, "error": ""},
        goto=END,
    )


def decide_bidding_choice_command(state: PurchaseProcessState) -> Command:
    """[2단계] decide_bidding.py 기존 규칙(금액/수량/신규거래/구매주기)
    그대로 신뢰해서 완전 자동판정 (사람 개입 없음)."""
    from backend_logic2.nodes.mr.decide_bidding import decide_bidding

    mr_name = state["mr_name"]
    bidding_results = decide_bidding(mr_name)
    bidding_items = [code for code, info in bidding_results.items() if info["needs_bidding"]]

    if not bidding_items:
        return Command(
            update={"bidding_results": bidding_results, "status": "catalog_purchase_required"},
            goto=END,
        )

    return Command(
        update={
            "bidding_results": bidding_results,
            "bidding_items": bidding_items,
            "status": "resolving_suppliers",
        },
        goto="resolve_suppliers_choice",
    )


def resolve_suppliers_choice_command(state: PurchaseProcessState) -> Command:
    """[3단계] 비딩 대상 품목들의 기존(ERPNext) 공급사 확인, 완전 자동분기.
    실제 판정 로직(최소경쟁업체수, 1년경과 여부)은 resolve_supplier_pool.py로
    분리됨 - 이 함수는 그 결과를 받아서 그래프 라우팅만 담당."""
    from backend_logic2.nodes.supplier.resolve_supplier_pool import resolve_supplier_pool

    bidding_items = state.get("bidding_items", [])
    result = resolve_supplier_pool(bidding_items)

    print(f"\n[공급사풀 판정]")
    for line in result["log_lines"]:
        print(line)
    print(f"  -> 최종판정: {'신규탐색 필요' if result['needs_search'] else '기존 공급사만 사용'}\n")

    return Command(
        update={
            "existing_supplier_candidates": result["existing_candidates"],
            "status": "resolving_supplier_pool",
        },
        goto="search_new_suppliers" if result["needs_search"] else "select_rfq_targets",
    )


def _search_new_suppliers(item_codes: list[str]) -> list[dict]:
    """item_code 목록에 대해 supplier_search로 신규 공급사 탐색, 이름기준 중복제거."""
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.nodes.supplier.supplier_search import supplier_search

    candidates_by_name: dict[str, dict] = {}
    for item_code in item_codes:
        item = erp_get_one("Item", item_code) or {}
        item_name = item.get("item_name") or item_code
        print(f"  [{item_code}] '{item_name}' 신규 공급사 탐색 중...")
        searched = supplier_search(item_name, target_count=10)
        for c in searched:
            name = str(c.get("name") or "").strip()
            if name and name not in candidates_by_name:
                candidates_by_name[name] = {**c, "name": name}
        print(f"    -> {len(searched)}건 발견")
    return list(candidates_by_name.values())


def search_new_suppliers_command(state: PurchaseProcessState) -> Command:
    """[4단계] 신규 공급사 탐색 실행, 기존 후보와 합침 (사람 개입 없음,
    합친 결과를 다음 단계에서 사람이 검토함)."""
    bidding_items = state.get("bidding_items", [])
    existing = state.get("existing_supplier_candidates", [])
    candidates_by_name = {c["name"]: c for c in existing}

    print(f"\n[신규 공급사 탐색] 대상 품목 {len(bidding_items)}건")
    new_ones = _search_new_suppliers(bidding_items)
    for c in new_ones:
        if c["name"] not in candidates_by_name:
            candidates_by_name[c["name"]] = c

    print(f"[신규 공급사 탐색 완료] 기존{len(existing)}건 + 신규탐색 결과 합쳐서 총 {len(candidates_by_name)}건\n")

    return Command(
        update={
            "supplier_candidates": sorted(candidates_by_name.values(), key=lambda row: row["name"]),
            "status": "awaiting_supplier_approval",
        },
        goto="select_rfq_targets",
    )


def select_rfq_targets_command(state: PurchaseProcessState) -> Command:
    """[5단계-대기] RFQ 보낼 대상 선택. existing_pool_sufficient로 바로
    온 경우엔 supplier_candidates가 아직 안 채워져 있을 수 있어서,
    그럴 땐 existing_supplier_candidates를 그대로 씀."""
    raw_candidates = state.get("supplier_candidates") or state.get("existing_supplier_candidates", [])
    candidates = [
        dict(candidate) if isinstance(candidate, dict) else {"name": str(candidate), "registered": True}
        for candidate in raw_candidates
    ]
    names = [candidate.get("name") for candidate in candidates if candidate.get("name")]

    if not candidates:
        return Command(
            update={"status": "human_review", "error": "공급사 후보를 확보하지 못했습니다."},
            goto=END,
        )

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

    from backend_logic2.nodes.supplier.register_candidate_suppliers import register_candidate_suppliers

    print(f"\n[공급사 등록] {len(selected_candidates)}건 등록 시도: {[c['name'] for c in selected_candidates]}")
    registrations = register_candidate_suppliers(selected_candidates)
    failed = [row for row in registrations if row.get("status") == "failed"]
    if failed:
        print(f"  등록 실패: {failed}")
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
    print(f"  등록 완료: {selected}\n")

    return Command(
        update={
            "selected_suppliers": selected,
            "supplier_candidates": candidates,
            "supplier_registration_results": registrations,
            "status": "creating_rfq",
            "error": "",
        },
        goto="create_rfq",
    )


def create_rfq_command(state: PurchaseProcessState) -> Command:
    """[6단계] 선택된 공급사한테 RFQ 생성+발송.

    ⚠️ TEST_MODE 안전장치: create_and_send_rfq()는 Submit되는 순간
    ERPNext 자체 로직(Suppliers 하위테이블의 send_email 체크박스)이
    이메일 발송을 트리거하는 구조라, 파이썬 쪽에서 TEST_MODE를 확인 안
    하고 그냥 send_email=True로 넘기면 ERPNext가 실제로 메일을 보내버릴
    수 있음. 여기서 is_test_mode()로 명시적으로 확인해서, TEST_MODE=true면
    무조건 send_email=False로 강제함 - 이 값이 사람 입력이나 다른 로직으로
    덮어써질 여지를 아예 없앰."""
    from backend_logic2.integrations.erp_client import is_test_mode
    from backend_logic2.nodes.rfq.send_rfq import create_and_send_rfq

    test_mode = is_test_mode()
    send_email = not test_mode  # TEST_MODE=true면 무조건 False, 예외 없음

    print(f"\n[RFQ 생성] '{state['mr_name']}' -> 대상: {state['selected_suppliers']}")
    print(f"  환경: {'TEST_MODE (실제 이메일 발송 안 함)' if test_mode else '운영 모드 (실제 이메일 발송됨)'}")

    rfq = create_and_send_rfq(
        state["mr_name"],
        state["selected_suppliers"],
        send_email=send_email,
        submit=True,
    )

    if not rfq or not rfq.get("name"):
        print("  -> RFQ 생성/발송 실패")
        return Command(
            update={"status": "human_review", "error": "RFQ 생성 또는 발송에 실패했습니다."},
            goto=END,
        )

    print(f"  -> RFQ 생성 완료: {rfq['name']}"
          f" (이메일 {'발송 안 함, TEST_MODE' if test_mode else '실제 발송됨'})\n")

    return Command(
        update={
            "rfq_name": rfq["name"],
            "status": "awaiting_quotation_check",
        },
        goto="check_quotations",
    )


def check_quotations_command(state: PurchaseProcessState) -> Command:
    """[7단계-대기] 견적 상태를 확인/재확인하는 단계. 여러 번 반복 가능
    (더 들어올 수도 있는 견적을 기다리며 몇 번이고 조회 가능) - 최종선정
    (final_selection)으로 넘어가는 건 "finalize"를 명시적으로 선택했을
    때만.

    ⚠️ "지금 조회만 함(check)"과 "최종선정 단계로 넘어감(finalize)"을
    분리하지 않고 같은 걸로 취급했던 버그를 고침 - 예전엔 견적을 조회하는
    순간 자동으로 final_selection으로 넘어가버려서, 나중에 견적이 더
    들어와도 재조회할 방법이 없었음.

    ⚠️ END로 보내면 LangGraph가 그 thread를 완전히 끝났다고 처리해서
    resume이 안 먹히는 문제가 있어서, "나중에" 선택이든 재확인이든
    항상 자기 자신(check_quotations)으로 되돌아감."""
    answer = interrupt({
        "type": "check_quotations",
        "rfq_name": state["rfq_name"],
        "message": "제출된 견적을 확인하시겠습니까? "
                    "(check: 지금 조회만 하고 계속 대기 / later: 그냥 대기 / "
                    "finalize: 지금까지 견적으로 최종선정 단계로 진행)",
        "allowed": ["check", "later", "finalize"],
    })
    choice = _decision_value(answer)
    if choice not in ("check", "later", "finalize"):
        return Command(
            update={"status": "awaiting_quotation_check", "error": "check, later, finalize 중 선택하세요."},
            goto="check_quotations",
        )
    if choice == "later":
        return Command(
            update={"status": "awaiting_quotation_check", "error": ""},
            goto="check_quotations",
        )

    # check와 finalize 둘 다 일단 지금 시점 견적을 조회함
    from backend_logic2.nodes.quotation.sq_evaluation import evaluate_quotations, print_evaluation

    result = evaluate_quotations(state["rfq_name"])
    print_evaluation(result)

    if result.get("error") or result.get("message") or not result.get("ranking"):
        return Command(
            update={
                "quotation_ranking": [],
                "status": "awaiting_quotation_check",
                "error": result.get("message") or result.get("error") or "제출된 견적이 없습니다. 나중에 다시 확인하세요.",
            },
            goto="check_quotations",
        )

    if choice == "check":
        # 조회는 했지만 아직 확정은 아님 - 결과만 갱신하고 계속 대기상태 유지
        return Command(
            update={"quotation_ranking": result["ranking"], "status": "awaiting_quotation_check", "error": ""},
            goto="check_quotations",
        )

    return Command(
        update={"quotation_ranking": result["ranking"], "status": "awaiting_final_selection", "error": ""},
        goto="final_selection",
    )


def final_selection_command(state: PurchaseProcessState) -> Command:
    """[8단계-대기] 순위목록 보여주고 최종 공급사 선정 물어봄."""
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

    # 다음 단계(PO 생성+발송)는 아직 미구현 - 임시로 여기서 멈춤
    return Command(
        update={"selected_supplier": supplier, "status": "supplier_selected_awaiting_po", "error": ""},
        goto=END,
    )