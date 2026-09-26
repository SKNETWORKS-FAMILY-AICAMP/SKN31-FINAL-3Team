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

from datetime import date, datetime, timezone
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
    policy_version: int
    status: str
    substitute_results: dict[str, Any]
    selected_substitute: str
    bidding_results: dict[str, Any]
    bidding_items: list[str]
    direct_purchase: bool
    direct_purchase_items: dict[str, dict[str, Any]]
    force_bidding: bool
    order_started: bool
    # 협력사 선정 화면에서 "수주 접수 요청 메일을 지금 보낸다"는 확인을 이미
    # 받은 경우 True. 선정 이후 사람에게 같은 확인을 두 번(발주 시작, PR 요청)
    # 더 묻지 않고 PR 발송까지 이어서 진행한다. 신규 협력사 서류 검토처럼
    # 성격이 다른 확인 단계는 이 플래그와 무관하게 그대로 멈춘다.
    auto_pr_dispatch: bool
    existing_supplier_candidates: list[dict[str, Any]]
    supplier_candidates: list[dict[str, Any]]
    supplier_registration_results: list[dict[str, Any]]
    supplier_document_review: dict[str, Any]
    supplier_documents_approved: bool
    supplier_onboarding_note: str
    selected_suppliers: list[str]
    custom_rfq_suppliers: list[str]
    quotation_deadline: str
    rfq_name: str
    # 재비딩으로 이미 마감된 지난 라운드들의 이력. 재비딩할 때 ERPNext의
    # RFQ/Supplier Quotation을 취소·폐기하지 않고 그대로 둔 채 새 RFQ를
    # 하나 더 만들기 때문에(한 MR에 여러 RFQ가 연결됨), "몇 차"인지와
    # 지난 라운드 RFQ 이름을 여기 쌓아둔다 - 협력사 선정 화면의 차수
    # 배지/팝업이 이걸로 지난 견적을 다시 조회한다. 현재 진행 중인
    # 라운드는 rfq_name/quotation_deadline로 따로 관리하고 여기엔 안 넣는다.
    rfq_rounds: list[dict[str, Any]]
    quotation_ranking: list[dict[str, Any]]
    # 규격/정합성 검증에서 순위에 들지 못한 견적과 그 사유. 예전에는 순위가
    # 통째로 비었을 때만 error 문구에 요약해서 붙이고 버렸기 때문에, 일부
    # 견적만 제외된 경우 프론트가 "AI 평가가 아직 없는 견적"과 "검증에서
    # 탈락한 견적"을 구분할 수 없어 영원히 '평가 전'처럼 보였다. 항상 저장해
    # 화면에서 사유를 그대로 보여준다.
    quotation_excluded: list[dict[str, Any]]
    # 유효 견적 수·단독 응찰 여부·규격 AI 평가 상태 등 순위 계산 부가정보.
    quotation_ranking_meta: dict[str, Any]
    requested_supplier: str
    requested_quotation: str
    selected_supplier: str
    selected_quotation: str
    selected_rfq_name: str
    pr_id: str
    pr_status: str
    pr_rejection_reason: str
    pr_supplier_email: str
    # 수주(PR) 단계에서 거절한 공급사들의 누적 이력. 재비딩 없이 "기존
    # 견적서에서 재선택"만 반복하는 경우, 예전에 거절했던 공급사가 남은
    # 후보 목록에 다시 등장할 수 있어(예: A거절->B선택->B도거절 시 남은
    # 후보에 A가 다시 포함됨) 프론트에서 "이미 거절당함" 표시를 하려면
    # 이 누적 이력이 필요하다.
    rejected_suppliers: list[dict[str, Any]]
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


def _archive_current_rfq_round(state: PurchaseProcessState) -> list[dict[str, Any]]:
    """재비딩 시 지금 라운드를 rfq_rounds 이력에 추가해서 반환한다.

    ⚠️ ERPNext의 RFQ/Supplier Quotation 문서는 여기서 취소·폐기하지
    않는다 - 그대로 살려두고 새 RFQ를 하나 더 만드는 방식으로 바꿨기
    때문에, 지난 라운드 견적은 언제든 이 rfq_name으로 다시 조회할 수
    있다(협력사 선정 화면의 '차수' 배지/팝업이 이 이력을 사용한다)."""
    rounds_history = list(state.get("rfq_rounds") or [])
    rfq_name = str(state.get("rfq_name") or "").strip()
    if rfq_name:
        rounds_history.append({
            "round": len(rounds_history),
            "rfq_name": rfq_name,
            "deadline": state.get("quotation_deadline") or "",
            # ⚠️ datetime.now()는 시간대 정보가 없는(naive) 문자열을 만든다.
            # 서버가 UTC로 돌기 때문에 프론트가 그 문자열을 로컬(KST)로
            # 해석해서 종료 시각이 9시간 어긋나 보였다(12:50 -> 03:50).
            # 시간대를 붙여서 저장한다.
            "closed_at": datetime.now(timezone.utc).isoformat(),
        })
    return rounds_history


def _rfq_round_names(state: PurchaseProcessState) -> list[str]:
    """지난 차수부터 현재 차수까지 RFQ 이름을 순서대로 반환한다."""
    names: list[str] = []

    for entry in state.get("rfq_rounds") or []:
        if not isinstance(entry, dict):
            continue
        rfq_name = str(entry.get("rfq_name") or "").strip()
        if rfq_name and rfq_name not in names:
            names.append(rfq_name)

    current_rfq = str(state.get("rfq_name") or "").strip()
    if current_rfq and current_rfq not in names:
        names.append(current_rfq)

    return names


def _rfq_round_map(state: PurchaseProcessState) -> dict[str, int]:
    """RFQ 이름별 차수를 반환한다.

    ⚠️ entry에 저장된 "round" 필드 값은 신뢰하지 않는다. 0-based 번호 매기기
    규칙으로 바뀌기 전 체크포인트에는 옛 값(1부터 시작)이 그대로 남아있을 수
    있어서, 그 값을 그대로 쓰면 새로 archive되는 라운드와 번호가 겹친다(실제로
    두 라운드가 전부 "1차"로 겹쳐 보이는 버그로 나타났다). rfq_rounds는 재비딩이
    일어난 순서대로 이력이 쌓이는 배열이므로, 배열 안 위치(index)가 항상 진짜
    차수다.
    """
    result: dict[str, int] = {}

    for index, entry in enumerate(state.get("rfq_rounds") or [], start=0):
        if not isinstance(entry, dict):
            continue

        rfq_name = str(entry.get("rfq_name") or "").strip()
        if not rfq_name:
            continue

        result[rfq_name] = index

    current_rfq = str(state.get("rfq_name") or "").strip()
    if current_rfq:
        result[current_rfq] = len(state.get("rfq_rounds") or [])

    return result


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
    if state.get("entrypoint") == "pr_request_recovery":
        return Command(
            update={
                "entrypoint": "",
                "case_id": case_id,
                "status": "awaiting_pr_request",
                "error": "",
            },
            goto="request_pr",
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
        "instructions": "원본 재고 또는 대체품 중 하나를 item_code로 선택하거나, "
                         "'new_purchase'로 신규구매를 진행하세요.",
        "allowed_item_codes": [s["item_code"] for s in all_substitutes],
    })

    choice = answer.get("item_code") if isinstance(answer, dict) else str(answer or "").strip()
    if _decision_value(answer) == "new_purchase" or choice == "new_purchase":
        _submit_mr_for_purchase(state["mr_name"])
        return Command(
            update={"force_bidding": True, "direct_purchase": False, "status": "checking_bidding", "error": ""},
            goto="decide_bidding_choice",
        )

    valid_codes = {s["item_code"] for s in all_substitutes}
    if choice not in valid_codes:
        return Command(
            update={
                "status": "awaiting_substitute_selection",
                "error": "유효한 재고 후보 item_code 또는 'new_purchase'를 선택하세요.",
            },
            goto="substitute_selection",
        )

    selected = next(candidate for candidate in all_substitutes if candidate["item_code"] == choice)
    _apply_substitute_selected_to_mr(
        state["mr_name"],
        choice,
        existing_stock=bool(selected.get("is_original_item")),
    )

    return Command(
        update={"status": "substitute_selected", "selected_substitute": choice, "error": ""},
        goto=END,
    )


def _apply_substitute_selected_to_mr(
    mr_name: str,
    item_code: str,
    *,
    existing_stock: bool = False,
) -> None:
    """요청자가 기존 재고나 대체품을 선택하면 원본 Draft MR을 폐기한다.

    이 단계까지 MR은 의도적으로 Draft를 유지한다. 표준 Discard가 실패한
    경우 그래프도 실패로 남겨 ERP와 BiddingFlow 상태가 어긋나지 않게 한다.
    """
    from backend_logic2.nodes.mr.reject_material_request import reject_material_request

    reason = (
        f"원본 품목({item_code})의 기존 재고 사용이 확정되어 구매 MR을 종료합니다."
        if existing_stock
        else f"대체품({item_code}) 사용이 확정되어 원본 MR을 종료합니다."
    )
    reject_material_request(
        mr_name,
        reason,
        reason_code="EXISTING_STOCK_SELECTED" if existing_stock else "SUBSTITUTE_SELECTED",
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
    if state.get("force_bidding"):
        # 대체품 후보를 거절하고 신규구매로 진행한 경우 기본적으로는 다시
        # 비딩을 거치게 한다(2026-09-08 fix: preserve bidding flow after
        # substitute rejection). 다만 긴급발주(7일 이내)라 이전 PO
        # 공급사·확정단가를 그대로 재사용할 수 있는 품목까지 강제로 비딩을
        # 돌리면 납기를 맞출 수 없으므로, 그 품목만은 강제 비딩에서 제외해
        # 곧장 직접구매로 진행한다.
        urgent_direct_purchase_items = {
            code
            for code, info in bidding_results.items()
            if not info["needs_bidding"]
            and info.get("direct_supplier")
            and any(str(reason).startswith("긴급발주") for reason in info.get("reasons", []))
        }
        bidding_items = [code for code in bidding_results if code not in urgent_direct_purchase_items]

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

        # 비딩을 생략하더라도 협력사 선정 결과를 구매 담당자가 화면에서
        # 확인하고 '발주 진행'을 눌러야 PO 관리 단계로 이동한다.
        return Command(
            update={
                "bidding_results": bidding_results,
                "direct_purchase": True,
                "direct_purchase_items": direct_purchase_items,
                "selected_supplier": next(iter(direct_suppliers)),
                "order_started": False,
                "status": "supplier_selected",
                "error": "",
            },
            goto="await_order_start",
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
    custom_rfq_suppliers = [
        str(result.get("name") or "").strip()
        for candidate, result in zip(selected_candidates, registrations)
        if candidate.get("source") == "manual"
        and result.get("status") != "failed"
        and str(result.get("name") or "").strip()
    ]
    print(f"  등록 완료: {selected}\n")

    return Command(
        update={
            "selected_suppliers": selected,
            "custom_rfq_suppliers": custom_rfq_suppliers,
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
    수 있음. TEST_MODE=true면 전체를 차단하고, custom_only면 정확한 이메일
    화이트리스트와 일치하는 공급사 행만 발송하며, false일 때만 전체 발송한다."""
    from backend_logic2.integrations.erp_client import get_email_delivery_policy
    from backend_logic2.nodes.rfq.send_rfq import create_and_send_rfq

    email_policy = get_email_delivery_policy()
    custom_rfq_suppliers = list(dict.fromkeys(state.get("custom_rfq_suppliers") or []))
    send_email = email_policy != "block_all"
    email_supplier_names = None

    print(f"\n[RFQ 생성] '{state['mr_name']}' -> 대상: {state['selected_suppliers']}")
    if email_policy == "custom_only":
        print("  환경: CUSTOM_ONLY (이메일 화이트리스트 일치 수신처만 발송)")
    else:
        print(f"  환경: {'TEST_MODE (실제 이메일 발송 안 함)' if email_policy == 'block_all' else '운영 모드 (실제 이메일 발송됨)'}")

    rfq = create_and_send_rfq(
        state["mr_name"],
        state["selected_suppliers"],
        send_email=send_email,
        email_supplier_names=email_supplier_names,
        submit=True,
    )

    if not rfq or not rfq.get("name"):
        print("  -> RFQ 생성/발송 실패")
        # 실패 노드를 END로 커밋하면 snapshot.next가 비어 실제 재시도가
        # 불가능하다. 예외로 남겨 직전 RFQ 대상 선택 체크포인트에서 다시
        # 응답할 수 있게 한다.
        raise RuntimeError("RFQ 생성 또는 발송에 실패했습니다.")

    if email_policy == "block_all":
        delivery_result = "발송 안 함, TEST_MODE"
    elif email_policy == "custom_only":
        delivery_result = "이메일 화이트리스트 일치 수신처만 실제 발송"
    else:
        delivery_result = "선택 대상 전체 실제 발송"
    print(f"  -> RFQ 생성 완료: {rfq['name']} (이메일 {delivery_result})\n")

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
                    "finalize: 지금까지 견적으로 최종선정 단계로 진행 / "
                    "rebid: 현재 견적을 후보로 유지한 채 새 RFQ 차수 진행)",
        "allowed": ["check", "later", "finalize", "rebid"],
    })
    choice = _decision_value(answer)
    if choice not in ("check", "later", "finalize", "rebid"):
        return Command(
            update={"status": "awaiting_quotation_check", "error": "check, later, finalize, rebid 중 선택하세요."},
            goto="check_quotations",
        )
    if choice == "later":
        return Command(
            update={"status": "awaiting_quotation_check", "error": ""},
            goto="check_quotations",
        )

    if choice == "rebid":
        # 견적 마감이 지났는데 아직 아무도 선정하지 않은 상태에서 구매
        # 담당자가 "지금까지 들어온 견적은 버리고 새로 RFQ를 다시
        # 보낸다"를 선택한 경우.
        #
        # ⚠️ 예전엔 여기서 기존 RFQ/Supplier Quotation을 ERPNext에서
        # 취소·폐기했는데, 협력사 선정 화면에 "차수(라운드)" 기록/조회
        # 기능이 추가되면서 지난 RFQ를 지우지 않고 그대로 둔 채 새 RFQ를
        # 하나 더 만드는 방식으로 바꿨다 - 한 MR에 RFQ가 여러 건 연결될
        # 수 있고, 지난 라운드 견적은 rfq_rounds 이력의 rfq_name으로
        # 언제든 다시 조회 가능하다. supplier_candidates(추천/직접추가
        # 협력사 풀)도 그대로 남겨서 select_rfq_targets에서 같은 후보
        # 목록으로 다시 고를 수 있게 한다.
        return Command(
            update={
                "rfq_rounds": _archive_current_rfq_round(state),
                "rfq_name": "",
                "requested_supplier": "",
                "selected_suppliers": [],
                "supplier_registration_results": [],
                "quotation_deadline": "",
                "status": "awaiting_supplier_approval",
                "error": "",
            },
            goto="select_rfq_targets",
        )

    # check와 finalize 둘 다 일단 지금 시점 견적을 조회함
    from backend_logic2.nodes.quotation.quotation_filter.quotation_ranker import (
        evaluate_quotations_for_rfqs,
        print_evaluation,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
        submit_finalized_quotations,
    )

    result = evaluate_quotations_for_rfqs(
        _rfq_round_names(state),
        current_rfq_name=state["rfq_name"],
        round_by_rfq=_rfq_round_map(state),
    )
    print_evaluation(result)
    # 그래프가 계산한 결과를 케이스의 실시간 순위(그래프 밖 읽기 모델)에도
    # 그대로 반영해서, 화면이 보는 순위가 두 군데로 갈라지지 않게 한다.
    from backend_logic2.services.quotation_service import save_live_ranking_from_result

    save_live_ranking_from_result(
        state.get("case_id"),
        result,
        rfq_name=state["rfq_name"],
        rfq_names=_rfq_round_names(state),
    )

    if result.get("error") or result.get("message") or not result.get("ranking"):
        # ⚠️ "제출된 견적이 없습니다"라는 기본 문구만 보여주면, 실제로는 견적이
        # 있는데 AI 규격 평가(RunPod)가 그 견적들을 전부 제외해서 순위가
        # 비어버린 경우와 구분이 안 된다 - 원인 파악을 위해 매번 서버 로그를
        # 뒤져야 했다. evaluate_quotations_for_rfqs가 반환하는 excluded(왜
        # 제외됐는지 견적별 evidence)를 에러 문구에 그대로 붙여서, 다음에
        # 이 에러가 뜨면 화면에서 바로 원인을 볼 수 있게 한다.
        excluded_rows = result.get("excluded") or []
        excluded_summary = "; ".join(
            f"{row.get('supplier_name') or row.get('quotation_id') or '알 수 없음'}: "
            f"{', '.join(str(item) for item in (row.get('evidence') or [])) or '평가 결과 없음'}"
            for row in excluded_rows[:5]
            if isinstance(row, dict)
        )
        # ⚠️ 견적이 들어왔는데 전부 순위에서 빠진 경우에도 "제출된 견적이
        # 없습니다"라고 떠서, 협력사가 회신을 안 한 것처럼 읽혔다. 이제 순위에서
        # 빠지는 건 파싱 실패(견적서를 읽지 못함)와 다른 RFQ 견적뿐이다.
        parse_failed_rows = [
            row for row in excluded_rows
            if isinstance(row, dict) and row.get("kind") == "parse_failed"
        ]
        if parse_failed_rows and len(parse_failed_rows) == len(excluded_rows):
            fallback_message = (
                f"회신된 견적 {len(parse_failed_rows)}건을 모두 읽지 못했습니다(파싱 실패). "
                "견적서 원본 파일을 직접 확인해 주세요."
            )
        elif excluded_rows:
            fallback_message = (
                f"회신된 견적 {len(excluded_rows)}건이 모두 순위 대상에서 빠졌습니다."
            )
        else:
            fallback_message = (
                result.get("message") or result.get("error") or "제출된 견적이 없습니다. 나중에 다시 확인하세요."
            )
        if excluded_summary:
            fallback_message = f"{fallback_message} (제외 사유 - {excluded_summary})"
        return Command(
            update={
                "quotation_ranking": state.get("quotation_ranking") or [],
                "quotation_excluded": excluded_rows,
                "quotation_ranking_meta": {
                    "competition_count": result.get("competition_count", 0),
                    "single_bid": bool(result.get("single_bid")),
                    "specification_evaluation": result.get("specification_evaluation") or {},
                },
                "status": "awaiting_quotation_check",
                "error": fallback_message,
            },
            goto="check_quotations",
        )

    if choice == "check":
        # 조회는 했지만 아직 확정은 아님 - 결과만 갱신하고 계속 대기상태 유지
        return Command(
            update={
                "quotation_ranking": result["ranking"],
                "quotation_excluded": result.get("excluded") or [],
                "quotation_ranking_meta": {
                    "competition_count": result.get("competition_count", 0),
                    "single_bid": bool(result.get("single_bid")),
                    "specification_evaluation": result.get("specification_evaluation") or {},
                },
                "status": "awaiting_quotation_check",
                "error": "",
            },
            goto="check_quotations",
        )

    # 포털 견적은 Draft로 생성된다. 사용자가 명시적으로 최종 선정을
    # 시작할 때만 순위에 포함된 견적을 Submit하여 이후 변경을 막는다.
    submit_finalized_quotations(result["ranking"])
    requested_supplier = (
        str(answer.get("supplier") or "").strip()
        if isinstance(answer, dict)
        else ""
    )
    requested_quotation = (
        str(answer.get("quotation_id") or "").strip()
        if isinstance(answer, dict)
        else ""
    )
    return Command(
        update={
            "auto_pr_dispatch": _auto_pr_dispatch_requested(answer),
            "quotation_ranking": result["ranking"],
            "quotation_excluded": result.get("excluded") or [],
            "quotation_ranking_meta": {
                "competition_count": result.get("competition_count", 0),
                "single_bid": bool(result.get("single_bid")),
                "specification_evaluation": result.get("specification_evaluation") or {},
            },
            "requested_supplier": requested_supplier,
            "requested_quotation": requested_quotation,
            "status": "awaiting_final_selection",
            "error": "",
        },
        goto="final_selection",
    )


def _auto_pr_dispatch_requested(answer: Any) -> bool:
    """선정 화면에서 수주 접수 요청 메일 발송까지 확인받았는지."""
    return isinstance(answer, dict) and bool(answer.get("start_order"))


def final_selection_command(state: PurchaseProcessState) -> Command:
    """[8단계-대기] 순위목록을 보여주고 최종 공급사를 선정한다.

    선정과 발주는 서로 다른 사람의 명시적 행위다. 공급사를 골랐다는 이유로
    법적 효력이 생기는 PO를 즉시 만들지 않고, 협력사 선정 화면의 '발주 시작'
    입력과 PO 관리 화면의 최종 승인을 차례로 기다린다.
    """
    ranking = state.get("quotation_ranking", [])
    supplier = str(state.get("requested_supplier") or "").strip()
    quotation_id = str(state.get("requested_quotation") or "").strip()
    auto_pr_dispatch = bool(state.get("auto_pr_dispatch"))
    if not supplier:
        answer = interrupt({
            "type": "final_selection",
            "rfq_name": state["rfq_name"],
            "ranking": ranking,
        })
        supplier = answer.get("supplier") if isinstance(answer, dict) else str(answer or "").strip()
        quotation_id = (
            str(answer.get("quotation_id") or "").strip()
            if isinstance(answer, dict)
            else ""
        )
        # 이 화면에서 다시 고르는 경우엔 이번 답변의 확인 여부만 따른다
        # (예전 확인이 남아 메일이 나가는 일이 없도록).
        auto_pr_dispatch = _auto_pr_dispatch_requested(answer)
    valid_suppliers = {r.get("supplier") for r in ranking}
    if supplier not in valid_suppliers:
        return Command(
            update={
                "status": "awaiting_final_selection",
                "auto_pr_dispatch": False,
                "error": "순위 목록의 supplier를 선택하세요.",
            },
            goto="final_selection",
        )

    selected_row = next((
        row
        for row in ranking
        if (
            quotation_id
            and str(row.get("quotation_id") or row.get("name") or "").strip()
            == quotation_id
            and str(row.get("supplier") or "").strip() == supplier
        )
    ), None)
    if selected_row is None:
        selected_row = next((
            row for row in ranking
            if str(row.get("supplier") or "").strip() == supplier
        ), {})

    # 협력사가 견적에 설정한 유효기간(valid_till)이 지난 견적은 최종
    # 선정에서 제외한다. 이전 차수 견적도 후보 풀에 남아있는 채로 선택
    # 가능해졌기 때문에(요구사항: 이전 차수 최종선정 허용), 오래된 견적이
    # 만료됐는데도 그대로 발주로 이어지는 걸 여기서 막아야 한다.
    valid_till_raw = str(selected_row.get("valid_till") or "").strip()
    if valid_till_raw:
        try:
            valid_till_date = date.fromisoformat(valid_till_raw[:10])
        except ValueError:
            valid_till_date = None
        if valid_till_date is not None and valid_till_date < date.today():
            return Command(
                update={
                    "status": "awaiting_final_selection",
                    "requested_supplier": "",
                    "requested_quotation": "",
                    "auto_pr_dispatch": False,
                    "error": "선택한 견적은 유효기간이 지났습니다. 다른 견적을 선택하거나 재비딩해 주세요.",
                },
                goto="final_selection",
            )

    return Command(
        update={
            "auto_pr_dispatch": auto_pr_dispatch,
            "selected_supplier": supplier,
            "selected_quotation": str(
                selected_row.get("quotation_id") or selected_row.get("name") or ""
            ).strip(),
            "selected_rfq_name": str(selected_row.get("rfq_name") or state.get("rfq_name") or "").strip(),
            "requested_supplier": "",
            "requested_quotation": "",
            "supplier_document_review": {},
            "supplier_documents_approved": False,
            "supplier_onboarding_note": "",
            "status": "supplier_selected",
            "error": "",
        },
        goto="await_order_start",
    )


def await_order_start_command(state: PurchaseProcessState) -> Command:
    """Wait for 발주 진행, then move the case to PO management."""

    selected_supplier = str(state.get("selected_supplier") or "").strip()
    selected_registration = next(
        (
            row
            for row in state.get("supplier_registration_results") or []
            if str(row.get("name") or "").strip() == selected_supplier
        ),
        {},
    )
    requires_document_review = bool(
        selected_registration.get("is_new_supplier")
        or selected_registration.get("onboarding_status") == "PROVISIONAL"
    )

    # 견적을 통해 새로 선정된 업체는 발주 시작 전에 서류 검토를 완료해야 한다.
    # 직접구매 공급사는 과거 PO 실적에서 가져온 기존 등록 업체이므로 제외한다.
    if (
        not state.get("direct_purchase")
        and requires_document_review
        and not state.get("supplier_documents_approved")
        and not state.get("supplier_document_review")
    ):
        return Command(
            update={
                "status": "checking_supplier_documents",
                "error": "",
            },
            goto="inspect_selected_supplier_documents",
        )

    # 대체품 후보를 거절하고 신규구매로 진행한 뒤에도, 그 직접구매가
    # 긴급발주(7일 이내) 근거로 이전 PO 공급사를 재사용하는 것이라면 다시
    # 비딩으로 돌려보내지 않는다(위 decide_bidding_choice_command와 동일한
    # 예외). 그 외의 직접구매(예: 평소 반복구매)만 대체품 거절 이력이 있으면
    # 안전하게 비딩으로 재확인시킨다.
    direct_purchase_basis = state.get("direct_purchase_items") or {}
    is_urgent_direct_purchase = bool(direct_purchase_basis) and all(
        str(item.get("reason") or "").startswith("긴급발주")
        for item in direct_purchase_basis.values()
    )
    if (
        state.get("direct_purchase")
        and state.get("substitute_results")
        and not state.get("order_started")
        and not is_urgent_direct_purchase
    ):
        return Command(
            update={
                "force_bidding": True,
                "direct_purchase": False,
                "direct_purchase_items": {},
                "selected_supplier": "",
                "status": "checking_bidding",
                "error": "",
            },
            goto="decide_bidding_choice",
        )

    # 긴급발주 직접구매는 비딩으로 새로 고른 업체가 없고 이전 PO 공급사를
    # 그대로 재사용할 뿐이라, 사람이 "발주 시작"을 눌러 재확인할 대상 자체가
    # 없다. 다른 건들처럼 곧장 PR 요청 대기 단계로 넘어간다.
    if state.get("direct_purchase") and is_urgent_direct_purchase and not state.get("order_started"):
        return Command(
            update={"order_started": True, "status": "awaiting_pr_request", "error": ""},
            goto="request_pr",
        )

    # 선정 팝업에서 "수주 접수 요청 메일을 지금 보낸다"는 확인을 이미 받았으면
    # 같은 뜻의 '발주 시작' 확인을 다시 묻지 않는다.
    if state.get("auto_pr_dispatch") and not state.get("order_started"):
        return Command(
            update={"order_started": True, "status": "awaiting_pr_request", "error": ""},
            goto="request_pr",
        )

    answer = interrupt({
        "type": "order_start",
        "mr_name": state["mr_name"],
        "rfq_name": state.get("rfq_name"),
        "selected_rfq_name": state.get("selected_rfq_name"),
        "selected_quotation": state.get("selected_quotation"),
        "selected_supplier": state.get("selected_supplier"),
        "instructions": "선정 결과를 확인한 뒤 발주 진행을 눌러 PO 관리 화면으로 이동하세요.",
    })
    decision = _decision_value(answer)
    if decision != "start_order":
        return Command(
            update={"status": "supplier_selected", "error": "start_order 입력이 필요합니다."},
            goto="await_order_start",
        )
    return Command(
        update={"order_started": True, "status": "awaiting_pr_request", "error": ""},
        goto="request_pr",
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
        return Command(update={"status": "creating_pr", "error": ""}, goto="create_pr")
    if decision == "reject":
        return Command(
            update={"status": "human_review", "error": "PO 발송 전 최종 승인에서 반려되었습니다."},
            goto=END,
        )
    return Command(
        update={"status": "awaiting_po_approval", "error": "approve 또는 reject를 선택하세요."},
        goto="po_approval",
    )


def request_pr_command(state: PurchaseProcessState) -> Command:
    """Wait for the buyer to send the supplier PR from PO management."""
    if not state.get("order_started"):
        return Command(
            update={"status": "supplier_selected", "error": "발주 진행을 먼저 눌러 주세요."},
            goto="await_order_start",
        )
    # 같은 확인을 세 번 받지 않는다. 플래그는 여기서 지워서, PR 발송이
    # 실패해 다시 돌아오면 그때는 사람이 직접 'PR 요청'을 누르게 한다.
    if state.get("auto_pr_dispatch"):
        return Command(
            update={"auto_pr_dispatch": False, "status": "creating_pr", "error": ""},
            goto="create_pr",
        )

    answer = interrupt({
        "type": "pr_request",
        "case_id": state["case_id"],
        "mr_name": state["mr_name"],
        "rfq_name": state.get("rfq_name"),
        "selected_supplier": state.get("selected_supplier"),
        "quotation_ranking": state.get("quotation_ranking") or [],
        "purchase_mode": "direct" if state.get("direct_purchase") else "quotation",
        "direct_purchase_items": state.get("direct_purchase_items") or {},
    })
    if _decision_value(answer) == "request_pr":
        return Command(update={"status": "creating_pr", "error": ""}, goto="create_pr")
    return Command(
        update={"status": "awaiting_pr_request", "error": "PR 요청을 눌러 주세요."},
        goto="request_pr",
    )


def create_pr_command(state: PurchaseProcessState) -> Command:
    """Send a supplier acceptance request after internal PO approval."""
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.pr.service import create_and_send_pr

    supplier_id = str(state.get("selected_supplier") or "").strip()
    if not supplier_id:
        raise RuntimeError("PR을 발송할 선정 공급사가 없습니다.")
    supplier = erp_get_one("Supplier", supplier_id)
    supplier_email = str(
        (supplier or {}).get("email_id")
        or (supplier or {}).get("email")
        or (supplier or {}).get("supplier_email")
        or ""
    ).strip()
    if not supplier_email:
        raise RuntimeError(f"공급사 '{supplier_id}'에 등록된 이메일이 없습니다.")

    ranking = state.get("quotation_ranking") or []
    quotation_name = str(state.get("selected_quotation") or "").strip() or next((
        str(row.get("name") or "").strip()
        for row in ranking
        if str(row.get("supplier") or "").strip() == supplier_id
    ), "")
    pr = create_and_send_pr(
        case_id=state["case_id"], mr_name=state["mr_name"],
        supplier_id=supplier_id, supplier_email=supplier_email,
        rfq_name=state.get("selected_rfq_name") or state.get("rfq_name"),
        supplier_quotation=quotation_name or None,
        purchase_mode="direct" if state.get("direct_purchase") else "quotation",
        direct_purchase_items=state.get("direct_purchase_items") or {},
        expires_in_hours=72,
    )
    return Command(
        update={
            "pr_id": str(pr["pr_id"]), "pr_status": str(pr["status"]),
            "pr_supplier_email": supplier_email, "pr_rejection_reason": "",
            "status": "awaiting_supplier_pr_response", "error": "",
        },
        goto="await_supplier_pr_response",
    )


def await_supplier_pr_response_command(state: PurchaseProcessState) -> Command:
    """Pause until the supplier submits the signed email response form."""
    answer = interrupt({
        "type": "supplier_pr_response", "case_id": state["case_id"],
        "pr_id": state["pr_id"], "mr_name": state["mr_name"],
        "selected_supplier": state.get("selected_supplier"),
        "supplier_email": state.get("pr_supplier_email"),
    })
    decision = _decision_value(answer)
    reason = str(answer.get("reason") or "").strip() if isinstance(answer, dict) else ""
    if decision == "accept":
        return Command(
            update={"pr_status": "ACCEPTED", "pr_rejection_reason": "", "status": "creating_po", "error": ""},
            goto="create_po",
        )
    if decision == "reject" and len(reason) >= 2:
        rejected_supplier = str(state.get("selected_supplier") or "").strip()
        rejected_history = list(state.get("rejected_suppliers") or [])
        if rejected_supplier:
            rejected_history.append({
                "supplier": rejected_supplier,
                "reason": reason,
                "rejected_at": datetime.now().isoformat(),
            })
        return Command(
            update={
                "pr_status": "REJECTED", "pr_rejection_reason": reason, "status": "supplier_pr_rejected",
                "rejected_suppliers": rejected_history, "error": "",
            },
            goto="handle_pr_rejection",
        )
    return Command(
        update={"status": "awaiting_supplier_pr_response", "error": "공급사 응답 또는 거절 사유가 올바르지 않습니다."},
        goto="await_supplier_pr_response",
    )


def handle_pr_rejection_command(state: PurchaseProcessState) -> Command:
    """Let the buyer choose another ranked supplier, rebid, or stop."""
    rejected = str(state.get("selected_supplier") or "").strip()
    remaining = [row for row in state.get("quotation_ranking") or [] if str(row.get("supplier") or "").strip() != rejected]
    answer = interrupt({
        "type": "pr_rejection_review", "pr_id": state.get("pr_id"),
        "mr_name": state["mr_name"], "rejected_supplier": rejected,
        "rejection_reason": state.get("pr_rejection_reason"),
        "remaining_suppliers": remaining,
        # 이번 한 번만 거절한 공급사가 아니라, 이 케이스에서 지금까지
        # 수주를 거절한 공급사 전체 누적 이력 - 프론트가 remaining_suppliers
        # 중 "예전에 이미 거절했던 공급사"를 빨간 배지로 표시하는 데 쓴다.
        "rejected_suppliers": state.get("rejected_suppliers") or [],
        "allowed": ["select_next_supplier", "rebid", "cancel"],
    })
    decision = _decision_value(answer)
    if decision == "select_next_supplier":
        supplier = str(answer.get("supplier") or "").strip() if isinstance(answer, dict) else ""
        valid = {str(row.get("supplier") or "").strip() for row in remaining}
        if supplier not in valid:
            return Command(update={"status": "supplier_pr_rejected", "error": "차순위 공급사를 선택해 주세요."}, goto="handle_pr_rejection")
        selected_row = next(
            (row for row in remaining if str(row.get("supplier") or "").strip() == supplier),
            {},
        )
        return Command(
            update={
                "selected_supplier": supplier,
                "selected_quotation": str(selected_row.get("quotation_id") or selected_row.get("name") or "").strip(),
                "selected_rfq_name": str(selected_row.get("rfq_name") or state.get("rfq_name") or "").strip(),
                "pr_id": "", "pr_status": "", "pr_supplier_email": "", "status": "awaiting_pr_request", "error": "",
            },
            goto="request_pr",
        )
    if decision == "rebid":
        # ⚠️ 예전엔 여기서 decide_bidding_choice로 되돌려서
        # resolve_suppliers_choice -> search_new_suppliers를 다시 타게
        # 했는데, search_new_suppliers_command가 supplier_candidates를
        # existing_supplier_candidates + 새로 검색된 후보로 통째로
        # 교체해버려서 직접 추가(수동 입력)했던 협력사와 그 이메일이
        # 전부 날아가는 버그가 있었다. check_quotations_command의
        # "rebid"(마감 후 재비딩)와 완전히 같은 방식으로 select_rfq_targets로
        # 바로 돌아가 supplier_candidates(추천/직접추가 협력사 풀)는 그대로
        # 남긴다. 기존 RFQ/Supplier Quotation도 취소·폐기하지 않고 그대로
        # 둔 채 rfq_rounds 이력에 남기기만 한다("차수" 기록/조회 기능).
        return Command(
            update={
                "rfq_rounds": _archive_current_rfq_round(state),
                "selected_supplier": "", "selected_quotation": "", "selected_rfq_name": "",
                "pr_id": "", "pr_status": "", "pr_supplier_email": "",
                "rfq_name": "", "quotation_ranking": [], "requested_supplier": "",
                "selected_suppliers": [], "supplier_registration_results": [], "quotation_deadline": "",
                "status": "awaiting_supplier_approval", "error": "",
            },
            goto="select_rfq_targets",
        )
    if decision == "cancel":
        return Command(update={"status": "human_review", "error": "공급사가 수주를 거절하여 구매 담당자 확인이 필요합니다."}, goto=END)
    return Command(update={"status": "supplier_pr_rejected", "error": "차순위 선정, 재비딩 또는 종료를 선택해 주세요."}, goto="handle_pr_rejection")


def create_po_command(state: PurchaseProcessState) -> Command:
    """[9단계] 최종 선정된 공급사의 견적을 그대로 PO로 전환 + 포털링크 이메일 발송.

    실제 로직은 nodes/po/create_and_send_po.py(RFQ에 달린 Supplier Quotation
    재조회 -> 선정 공급사 견적 특정 -> 중복PO 방지 -> 납기일 확인 -> PO
    생성+Submit -> 포털링크 이메일 발송까지 이미 완성돼있던 독립 스크립트)에
    그대로 위임함. 그 함수는 이제 실패하면 (ERPNext의 실제 에러 메시지를 실은)
    예외를 던지는데, 그래프 노드 안에서 그걸 그대로 흘려보내면 체크포인트가
    끊기고 케이스가 이전 단계에 조용히 멈춘 것처럼 보인다. 그래서 여기서
    모든 실패를 잡아 human_review Command로 바꾸고, ERPNext가 실제로 보여준
    에러 문자열을 state["error"]에 그대로 실어서 프론트(MRListView의
    workflowError)에 그대로 노출한다 - "처리 확인 필요" 뱃지 + 실제 원인
    문구가 클릭 없이 바로 보이게.

    이메일 발송은 공통 정책을 따른다. true는 차단하고, custom_only는 PO
    생성 함수까지 진입한 뒤 공통 메일 게이트에서 정확한 주소 화이트리스트를
    다시 확인하며, false만 제한 없이 발송한다.
    """
    from backend_logic2.integrations.erp_client import get_email_delivery_policy
    from backend_logic2.nodes.po.create_and_send_po import (
        create_and_send_direct_po,
        create_and_send_po,
    )
    from backend_logic2.pr import repository as pr_repository
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.nodes.supplier.onboarding import (
        is_existing_registered_supplier,
    )

    pr_id = str(state.get("pr_id") or "").strip()

    def _fail(message: str) -> Command:
        message = message or "PO 생성 중 알 수 없는 오류가 발생했습니다."
        if pr_id:
            pr_repository.record_po_result(pr_id, po_name=None, error=message)
        print(f"  -> PO 생성 중단: {message}")
        return Command(
            update={"status": "po_creation_failed", "error": message},
            goto="handle_po_creation_failure",
        )

    if str(state.get("pr_status") or "").upper() != "ACCEPTED":
        return _fail("공급사가 PR을 수락하지 않아 PO를 생성할 수 없습니다.")

    supplier_id = str(state.get("selected_supplier") or "").strip()

    supplier_doc = erp_get_one("Supplier", supplier_id)

    if not is_existing_registered_supplier(supplier_doc):
        return _fail(
            f"신규 업체 '{supplier_id}'의 제출서류 확인 및 "
            "정식 거래처 등록이 완료되지 않아 "
            "PO를 생성할 수 없습니다."
        )

    direct_purchase = bool(state.get("direct_purchase"))
    rfq_name = state.get("selected_rfq_name") or state.get("rfq_name")
    supplier = state.get("selected_supplier")
    email_policy = get_email_delivery_policy()
    send_email = email_policy != "block_all"

    print(
        f"\n[PO 생성] '{state['mr_name']}' "
        f"({'최근 거래 직접구매' if direct_purchase else f'RFQ: {rfq_name}'}) "
        f"-> 공급사: {supplier}"
    )
    print(f"  이메일 정책: {email_policy}")

    try:
        if direct_purchase:
            po = create_and_send_direct_po(
                state["mr_name"],
                supplier,
                state.get("direct_purchase_items", {}),
                send_email=send_email,
            )
        else:
            if not rfq_name:
                raise RuntimeError("견적 기반 PO 생성에 필요한 RFQ가 없습니다.")
            po = create_and_send_po(
                rfq_name,
                supplier,
                mr_name=state["mr_name"],
                send_email=send_email,
            )
    except Exception as exc:
        return _fail(str(exc))

    if not po or not po.get("name"):
        return _fail("ERPNext가 PO 번호를 반환하지 않았습니다.")

    if pr_id:
        pr_repository.record_po_result(pr_id, po_name=str(po["name"]))

    print(f"  -> PO 처리 완료: {po['name']} (이메일 발송: {'예' if po.get('email_sent') else '아니오'})\n")

    return Command(
        update={"po_name": po["name"], "status": "po_sent", "error": ""},
        goto=END,
    )


def handle_po_creation_failure_command(state: PurchaseProcessState) -> Command:
    """PO 생성/Submit 실패 시, 담당자가 ERPNext에서 원인을 확인해 고친 뒤
    재시도하거나 반려할 수 있게 하는 HITL 분기.

    재시도하면 create_po로 다시 들어가는데, create_and_send_po.py가 이미
    Draft PO 재사용 로직을 갖고 있어서(기존 Draft가 있으면 새로 만들지
    않고 그걸 그대로 씀) 담당자가 ERPNext에서 Draft를 고친 뒤 재시도해도
    중복 Draft가 새로 생기지 않는다.
    """
    answer = interrupt({
        "type": "po_creation_failed",
        "case_id": state.get("case_id"),
        "mr_name": state["mr_name"],
        "pr_id": state.get("pr_id"),
        "error": state.get("error"),
        "allowed": ["retry", "reject"],
    })
    decision = _decision_value(answer)
    if decision == "retry":
        return Command(update={"status": "creating_po", "error": ""}, goto="create_po")
    if decision == "reject":
        return Command(
            update={
                "status": "human_review",
                "error": state.get("error") or "PO 생성 실패로 담당자 확인이 필요합니다.",
            },
            goto=END,
        )
    return Command(
        update={"status": "po_creation_failed", "error": "재시도 또는 반려를 선택해 주세요."},
        goto="handle_po_creation_failure",
    )


# 서류 조회
def inspect_selected_supplier_documents_command(
    state: PurchaseProcessState,
) -> Command:
    from backend_logic2.nodes.supplier.onboarding import (
        inspect_supplier_documents,
    )

    result = inspect_supplier_documents(
        state["rfq_name"],
        state["selected_supplier"],
    )

    if not result["review_required"]:
        return Command(
            update={
                "supplier_document_review": result,
                "supplier_documents_approved": True,
                "status": "supplier_selected",
                "error": "",
            },
            goto="await_order_start",
        )

    return Command(
        update={
            "supplier_document_review": result,
            "supplier_documents_approved": False,
            "status": "awaiting_supplier_document_review",
            "error": "",
        },
        goto="review_supplier_documents",
    )


# 담당자 검토
def review_supplier_documents_command(
    state: PurchaseProcessState,
) -> Command:
    review = state.get(
        "supplier_document_review"
    ) or {}

    answer = interrupt({
        "type": "supplier_document_review",
        "supplier": state["selected_supplier"],
        "rfq_name": state["rfq_name"],
        "documents": review.get("documents", []),
        "missing_documents": review.get(
            "missing_documents",
            [],
        ),
        "required_documents": [
            "business_registration",
            "bankbook",
        ],
        "optional_documents": [
            "corporate_seal_certificate",
            "seal_usage_certificate",
            "corporate_registry",
            "tax_clearance_certificate",
        ],
        "allowed": [
            "approve",
            "recheck",
            "reject",
        ],
    })

    decision = _decision_value(answer)
    answer_data = answer if isinstance(answer, dict) else {}

    if decision == "recheck":
        return Command(
            update={
                "status": "checking_supplier_documents",
                "error": "",
            },
            goto="inspect_selected_supplier_documents",
        )

    if decision == "approve":
        from backend_logic2.nodes.supplier.onboarding import (
            approve_supplier_onboarding,
        )

        approve_supplier_onboarding(
            state["selected_supplier"],
            documents_complete=bool(review.get("complete")),
            business_registration_verified=bool(
                answer_data.get(
                    "business_registration_verified"
                )
            ),
            bankbook_verified=bool(
                answer_data.get("bankbook_verified")
            ),
            note=str(answer_data.get("note") or ""),
        )

        return Command(
            update={
                "supplier_documents_approved": True,
                "supplier_onboarding_note": str(
                    answer_data.get("note") or ""
                ),
                "status": "supplier_selected",
                "error": "",
            },
            goto="await_order_start",
        )

    if decision == "reject":
        return Command(
            update={
                "selected_supplier": "",
                "status": "awaiting_final_selection",
                "error": (
                    "신규 업체 제출서류가 "
                    "반려되었습니다."
                ),
            },
            goto="final_selection",
        )

    return Command(
        update={
            "status": "awaiting_supplier_document_review",
            "error": (
                "approve, recheck, reject 중 "
                "하나를 선택하세요."
            ),
        },
        goto="review_supplier_documents",
    )
