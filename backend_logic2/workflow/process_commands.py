"""LangGraph Command wrappers connecting the purchasing nodes end to end.

전체 9단계 구현 완료(2026-08-31):
  [1단계] check_mr_item + substitute_selection - 대체품 확인, 있으면
    사람이 목록에서 고르거나 신규구매 선택 (HITL)
  [2단계] decide_bidding_choice - 비딩/카탈로그 자동판정 (decide_bidding.py
    기존 규칙 그대로 신뢰, 사람 개입 없음)
  [3단계] resolve_suppliers_choice - 기존 공급사 풀 판정, 신규탐색 필요여부 자동분기
  [4단계] search_new_suppliers - 신규 공급사 탐색(3소스 병렬)
  [5단계] select_rfq_targets - RFQ 보낼 공급사 선택/등록 (HITL)
  [6단계] create_rfq - RFQ 생성+발송
  [7단계] check_quotations - 견적 확인(반복 가능) (HITL)
  [8단계] final_selection - 최종 공급사 선정 (HITL)
  [9단계] create_po - 선정 견적을 PO로 전환 + 발송

⚠️ 사람이 수동으로 이어받아야 하는 종료 분기:
  - human_review(각 단계 실패/후보없음): 담당자 직접 확인 필요
  - substitute_selected(대체품 선택됨): 원본 Draft MR에 표준 사유를 남긴 뒤
    Discard하고 PostgreSQL Case는 CANCELLED로 보존함.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END
from langgraph.types import Command, interrupt


class PurchaseProcessState(TypedDict, total=False):
    entrypoint: str
    mr_name: str
    case_id: str
    status: str
    substitute_results: dict[str, Any]
    selected_substitute: str
    bidding_results: dict[str, Any]
    bidding_items: list[str]
    direct_purchase: bool
    direct_purchase_items: dict[str, dict[str, Any]]
    existing_supplier_candidates: list[dict[str, Any]]
    supplier_candidates: list[dict[str, Any]]
    supplier_registration_results: list[dict[str, Any]]
    selected_suppliers: list[str]
    quotation_deadline: str
    rfq_name: str
    quotation_ranking: list[dict[str, Any]]
    requested_supplier: str
    selected_supplier: str
    po_name: str
    cancellation_reason: str
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
    if isinstance(value, UUID):
        return str(value)
    return value


def _decision_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("decision") or value.get("action")
    return str(value or "").strip().lower()


def route_entrypoint_command(state: PurchaseProcessState) -> Command:
    """
    일반 시작 라우팅. 나중에 단계 추가되면 entrypoint별 분기 추가.

    2026-08-31 추가: 그래프 맨 처음(START 다음) 노드라 MR당 딱 1번만
    실행됨(재개/resume은 interrupt된 노드에서 바로 이어감, 여기로
    다시 안 옴) - 그래서 케이스 생성을 여기 한 곳에서만 함. 이후 모든
    노드는 state["case_id"]로 이 케이스를 계속 재사용(process_graph.py의
    공용 로깅 wrapper가 매 노드 상태전이를 자동으로 case_status_history에
    남김).
    """
    from backend_logic2.nodes.supplier.tools.case_logging import create_case

    case_id = state.get("case_id") or create_case(mr_name=state.get("mr_name"), status="started")
    if state.get("entrypoint") == "bidding_recheck":
        return Command(
            update={"entrypoint": "", "case_id": case_id, "status": "checking_bidding"},
            goto="decide_bidding_choice",
        )
    return Command(update={"entrypoint": "", "case_id": case_id, "status": "checking_mr_item"}, goto="check_mr_item")


def check_mr_item_command(state: PurchaseProcessState) -> Command:
    """[1단계] MR 품목별로 대체품 존재여부 확인.
    있으면 substitute_selection(사람이 고름)으로, 없으면 바로 비딩판정으로."""
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.nodes.mr.find_substitute import (
        find_substitutes_for_mr,
        notify_requester_of_substitutes,
    )

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
        # 요청부서한테 ERPNext 댓글+할당(알림)으로 바로 안내(2026-09-01
        # 추가) - substitute_selection의 interrupt()는 우리 CLI로만 답할
        # 수 있는데, 실제로 이 결정을 내리는 사람은 요청부서라 ERPNext
        # 안에서 바로 알려주고 답장받을 수 있게 함(substitute_reply_
        # watcher.py가 그 답장을 읽어서 대신 resume 호출).
        notify_requester_of_substitutes(mr, substitute_results)
        return Command(
            update={"substitute_results": substitute_results, "status": "awaiting_substitute_selection"},
            goto="substitute_selection",
        )

    # 구매 담당자의 시작 승인은 곧 신규 구매 경로의 MR Submit이다. RFQ
    # child row가 Material Request를 참조하려면 이 시점 이후 docstatus=1이
    # 보장되어야 한다.
    _submit_mr_for_purchase(mr_name)
    return Command(
        update={"substitute_results": substitute_results, "status": "checking_bidding"},
        goto="decide_bidding_choice",
    )


def substitute_selection_command(state: PurchaseProcessState) -> Command:
    """[1단계-대기] 대체품 목록을 보여주고, 사람이 하나 선택하거나
    'new_purchase'로 신규구매 진행을 선택하게 함."""
    from backend_logic2.nodes.mr.find_substitute import flatten_substitute_candidates

    substitute_results = state.get("substitute_results", {})
    all_substitutes = flatten_substitute_candidates(substitute_results)

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
        _submit_mr_for_purchase(state["mr_name"])
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

    _apply_substitute_selected_to_mr(state["mr_name"], choice)

    return Command(
        update={"status": "substitute_selected", "selected_substitute": choice, "error": ""},
        goto=END,
    )


def _apply_substitute_selected_to_mr(mr_name: str, item_code: str) -> None:
    """요청자가 대체품을 선택하면 원본 Draft MR을 즉시 폐기한다.

    이 단계까지 MR은 의도적으로 Draft를 유지한다. 표준 Discard가 실패한
    경우 그래프도 실패로 남겨 ERP와 BiddingFlow 상태가 어긋나지 않게 한다.
    """
    from backend_logic2.nodes.mr.reject_material_request import reject_material_request

    reject_material_request(
        mr_name,
        f"대체품({item_code}) 사용이 확정되어 원본 MR을 종료합니다.",
        reason_code="SUBSTITUTE_SELECTED",
    )


def _submit_mr_for_purchase(mr_name: str) -> dict:
    """구매 담당자 승인 뒤 Draft MR을 한 번만 Submit한다."""
    from backend_logic2.integrations.erp_client import erp_get_one, erp_submit

    material_request = erp_get_one("Material Request", mr_name)
    if not material_request:
        raise ValueError(f"Material Request를 찾을 수 없습니다: {mr_name}")
    docstatus = int(material_request.get("docstatus") or 0)
    if docstatus == 1:
        return material_request
    if docstatus != 0:
        raise ValueError(f"구매 진행할 수 없는 MR 상태입니다: {mr_name} (docstatus={docstatus})")
    return erp_submit("Material Request", mr_name)


def _cancel_urgent_mr_without_supplier(
    mr_name: str,
    bidding_results: dict[str, Any],
) -> str | None:
    """긴급 구매인데 연결 공급사가 전혀 없으면 표준 사유로 MR을 취소한다."""
    urgent_items = [
        item_code
        for item_code, info in bidding_results.items()
        if any(str(reason).startswith("긴급발주") for reason in info.get("reasons", []))
    ]
    if not urgent_items:
        return None

    from backend_logic2.nodes.mr.reject_material_request import reject_material_request

    # 품목 마스터의 공급사 연결만으로는 즉시 구매할 가격 근거가 없다.
    # 실제 Submit된 최근 PO에서 협력사와 확정단가를 함께 얻은 경우에만
    # 카탈로그식 직접구매가 가능하다.
    if all(bidding_results[item_code].get("direct_supplier") for item_code in urgent_items):
        return None

    reason = (
        "긴급 구매 요청이지만 최근 거래한 협력사가 없어 "
        "즉시 구매를 진행할 수 없습니다. 협력사 정보를 확인한 뒤 MR을 다시 요청해주세요. "
        f"대상 품목: {', '.join(urgent_items)}"
    )
    reject_material_request(
        mr_name,
        reason,
        reason_code="URGENT_NO_SUPPLIER",
    )
    return reason


def decide_bidding_choice_command(state: PurchaseProcessState) -> Command:
    """[2단계] decide_bidding.py 기존 규칙(금액/수량/신규거래/구매주기)
    그대로 신뢰해서 완전 자동판정 (사람 개입 없음)."""
    from backend_logic2.nodes.mr.decide_bidding import decide_bidding

    mr_name = state["mr_name"]
    bidding_results = decide_bidding(mr_name)
    bidding_items = [code for code, info in bidding_results.items() if info["needs_bidding"]]

    if not bidding_items:
        cancellation_reason = _cancel_urgent_mr_without_supplier(mr_name, bidding_results)
        if cancellation_reason:
            return Command(
                update={
                    "bidding_results": bidding_results,
                    "status": "urgent_no_supplier_cancelled",
                    "cancellation_reason": cancellation_reason,
                    "error": "",
                },
                goto=END,
            )

        direct_purchase_items = {
            code: {
                "supplier": info.get("direct_supplier"),
                "rate": info.get("last_rate"),
                "reference_po": info.get("reference_po"),
                "reference_date": info.get("reference_date"),
                "reason": (info.get("reasons") or [""])[0],
            }
            for code, info in bidding_results.items()
        }
        direct_suppliers = {
            str(item.get("supplier") or "").strip()
            for item in direct_purchase_items.values()
            if str(item.get("supplier") or "").strip()
        }
        missing_purchase_basis = [
            code
            for code, item in direct_purchase_items.items()
            if not item.get("supplier") or not item.get("rate")
        ]
        if missing_purchase_basis or len(direct_suppliers) != 1:
            error = (
                "비딩 불필요 판정은 완료됐지만 직접구매에 필요한 최근 협력사와 "
                "확정단가를 하나로 결정할 수 없습니다. "
                f"확인 품목: {', '.join(missing_purchase_basis or direct_purchase_items.keys())}"
            )
            return Command(
                update={
                    "bidding_results": bidding_results,
                    "direct_purchase_items": direct_purchase_items,
                    "status": "human_review",
                    "error": error,
                },
                goto=END,
            )

        # 비딩을 생략해도 PO는 법적 효력이 있으므로 즉시 생성하지 않는다.
        # 최근 확정 거래를 구매 근거로 저장하고 구매 담당자의 최종 승인을
        # 받은 뒤 direct PO 생성 노드로 진행한다.
        return Command(
            update={
                "bidding_results": bidding_results,
                "direct_purchase": True,
                "direct_purchase_items": direct_purchase_items,
                "selected_supplier": next(iter(direct_suppliers)),
                "status": "awaiting_po_approval",
                "error": "",
            },
            goto="po_approval",
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
    result = resolve_supplier_pool(bidding_items, case_id=state.get("case_id"))

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


def _search_new_suppliers(item_codes: list[str], case_id: str = None) -> list[dict]:
    """item_code 목록에 대해 supplier_search로 신규 공급사 탐색, 이름기준 중복제거."""
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.nodes.supplier.supplier_search import supplier_search

    candidates_by_name: dict[str, dict] = {}
    for item_code in item_codes:
        item = erp_get_one("Item", item_code) or {}
        item_name = item.get("item_name") or item_code
        print(f"  [{item_code}] '{item_name}' 신규 공급사 탐색 중...")
        searched = supplier_search(item_name, target_count=10, case_id=case_id)
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
    new_ones = _search_new_suppliers(bidding_items, case_id=state.get("case_id"))
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
    # 검색 결과가 0건이거나 목록에 없는 회사를 직접 입력한 경우에도 같은
    # HITL 폼에서 신규 Supplier 등록과 RFQ 발송을 계속할 수 있다.
    existing_names = {
        str(candidate.get("name") or "").strip() for candidate in candidates
    }
    for name, update in updates_by_name.items():
        if name not in existing_names:
            candidates.append(
                {
                    "name": name,
                    "email": str(update.get("email") or "").strip(),
                    "registered": False,
                    "source": "manual",
                }
            )
            existing_names.add(name)

    names = [candidate.get("name") for candidate in candidates if candidate.get("name")]

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
    registrations = register_candidate_suppliers(selected_candidates, case_id=state.get("case_id"))
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
            "quotation_deadline": str(answer.get("quotation_deadline") or "").strip(),
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
        # 실패 노드를 END로 커밋하면 snapshot.next가 비어 실제 재시도가
        # 불가능하다. 예외로 남겨 직전 RFQ 대상 선택 체크포인트에서 다시
        # 응답할 수 있게 한다.
        raise RuntimeError("RFQ 생성 또는 발송에 실패했습니다.")

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
    from backend_logic2.nodes.quotation.sq_evaluation import (
        evaluate_quotations,
        print_evaluation,
        submit_finalized_quotations,
    )

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

    # 포털 견적은 Draft로 생성된다. 사용자가 명시적으로 최종 선정을
    # 시작할 때만 순위에 포함된 견적을 Submit하여 이후 변경을 막는다.
    submit_finalized_quotations(state["rfq_name"], result["ranking"])
    requested_supplier = (
        str(answer.get("supplier") or "").strip()
        if isinstance(answer, dict)
        else ""
    )
    return Command(
        update={
            "quotation_ranking": result["ranking"],
            "requested_supplier": requested_supplier,
            "status": "awaiting_final_selection",
            "error": "",
        },
        goto="final_selection",
    )


def final_selection_command(state: PurchaseProcessState) -> Command:
    """[8단계-대기] 순위목록을 보여주고 최종 공급사를 선정한다.

    선정과 발주는 서로 다른 사람의 명시적 행위다. 공급사를 골랐다는 이유로
    법적 효력이 생기는 PO를 즉시 만들지 않고, 협력사 선정 화면의 '발주 시작'
    입력과 PO 관리 화면의 최종 승인을 차례로 기다린다.
    """
    ranking = state.get("quotation_ranking", [])
    supplier = str(state.get("requested_supplier") or "").strip()
    if not supplier:
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
        update={
            "selected_supplier": supplier,
            "requested_supplier": "",
            "status": "supplier_selected",
            "error": "",
        },
        goto="await_order_start",
    )


def await_order_start_command(state: PurchaseProcessState) -> Command:
    """협력사 선정 화면에서 사용자가 '발주 시작'을 누를 때까지 대기."""

    answer = interrupt({
        "type": "order_start",
        "mr_name": state["mr_name"],
        "rfq_name": state.get("rfq_name"),
        "selected_supplier": state.get("selected_supplier"),
        "instructions": "선정 결과를 확인한 뒤 발주 시작을 눌러 PO 승인 단계로 이동하세요.",
    })
    decision = _decision_value(answer)
    if decision != "start_order":
        return Command(
            update={"status": "supplier_selected", "error": "start_order 입력이 필요합니다."},
            goto="await_order_start",
        )
    return Command(
        update={"status": "awaiting_po_approval", "error": ""},
        goto="po_approval",
    )


def po_approval_command(state: PurchaseProcessState) -> Command:
    """PO 생성·Submit·발송 직전 구매 담당자의 최종 승인을 받는다."""

    answer = interrupt({
        "type": "po_approval",
        "mr_name": state["mr_name"],
        "rfq_name": state.get("rfq_name"),
        "selected_supplier": state.get("selected_supplier"),
        "quotation_ranking": state.get("quotation_ranking", []),
        "purchase_mode": "direct" if state.get("direct_purchase") else "quotation",
        "direct_purchase_items": state.get("direct_purchase_items", {}),
        "instructions": "PO는 법적 효력이 있으므로 생성·발송 전에 최종 승인 또는 반려하세요.",
    })
    decision = _decision_value(answer)
    if decision == "approve":
        return Command(update={"status": "creating_po", "error": ""}, goto="create_po")
    if decision == "reject":
        return Command(
            update={"status": "human_review", "error": "PO 발송 전 최종 승인에서 반려되었습니다."},
            goto=END,
        )
    return Command(
        update={"status": "awaiting_po_approval", "error": "approve 또는 reject를 선택하세요."},
        goto="po_approval",
    )


def create_po_command(state: PurchaseProcessState) -> Command:
    """[9단계] 최종 선정된 공급사의 견적을 그대로 PO로 전환 + 포털링크 이메일 발송.

    실제 로직은 nodes/po/create_and_send_po.py(RFQ에 달린 Supplier Quotation
    재조회 -> 선정 공급사 견적 특정 -> 중복PO 방지 -> 납기일 확인 -> PO
    생성+Submit -> 포털링크 이메일 발송까지 이미 완성돼있던 독립 스크립트)에
    그대로 위임함. 그 함수는 원래 CLI 스크립트라 실패 시 sys.exit(1)로
    프로세스를 통째로 죽이는데, 그래프 노드 안에서 그러면 체크포인트/로깅이
    중간에 끊기니까 여기서 SystemExit을 잡아서 정상적인 human_review
    Command로 바꿔줌.

    이메일 발송은 send_rfq.py와 같은 이유로 TEST_MODE면 무조건 강제로
    막음(send_email=not test_mode) - erp_send_email 자체도 TEST_MODE를
    다시 확인하지만, 이중 안전장치로 여기서도 명시적으로 막음.
    """
    from backend_logic2.integrations.erp_client import is_test_mode
    from backend_logic2.nodes.po.create_and_send_po import (
        create_and_send_direct_po,
        create_and_send_po,
    )

    direct_purchase = bool(state.get("direct_purchase"))
    rfq_name = state.get("rfq_name")
    supplier = state.get("selected_supplier")
    test_mode = is_test_mode()

    print(
        f"\n[PO 생성] '{state['mr_name']}' "
        f"({'최근 거래 직접구매' if direct_purchase else f'RFQ: {rfq_name}'}) "
        f"-> 공급사: {supplier}"
    )
    print(f"  환경: {'TEST_MODE (실제 이메일 발송 안 함)' if test_mode else '운영 모드 (실제 이메일 발송됨)'}")

    try:
        if direct_purchase:
            po = create_and_send_direct_po(
                state["mr_name"],
                supplier,
                state.get("direct_purchase_items", {}),
                send_email=not test_mode,
            )
        else:
            if not rfq_name:
                raise RuntimeError("견적 기반 PO 생성에 필요한 RFQ가 없습니다.")
            po = create_and_send_po(
                rfq_name,
                supplier,
                mr_name=state["mr_name"],
                send_email=not test_mode,
            )
    except SystemExit as exc:
        print("  -> PO 생성/발송 중단됨 (사유는 위 콘솔 출력 참고)")
        raise RuntimeError(
            "PO 생성 또는 발송이 중단되었습니다. 서버 로그를 확인해 주세요."
        ) from exc

    if not po or not po.get("name"):
        print("  -> PO 생성 실패")
        raise RuntimeError("PO 생성에 실패했습니다.")

    print(f"  -> PO 처리 완료: {po['name']} (이메일 발송: {'예' if po.get('email_sent') else '아니오'})\n")

    return Command(
        update={"po_name": po["name"], "status": "po_sent", "error": ""},
        goto=END,
    )
