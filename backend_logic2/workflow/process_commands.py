"""LangGraph Command wrappers connecting the purchasing nodes end to end.

전체 흐름 재설계 완료(2026-09-01, "Draft-first" 구조로 전환):
  [0단계] confirm_no_substitute - 대체품 미사용이 확정된 시점(대체품 자체가
    없었거나 신규구매 선택)에만 Draft MR을 실제로 Submit함. 그 전까지는
    MR이 계속 Draft 상태임 - 반려/대체품선택 분기는 Submit 자체가 안 됨.
  [1단계] check_mr_item + substitute_selection - 대체품 확인, 있으면
    사람이 목록에서 고르거나 신규구매 선택 (HITL). 대체품을 고르면 그
    Draft MR은 삭제됨(Discard) - 대체품으로 처리되니 원본은 필요 없어짐.
  [1.5단계] check_urgency - 요청납기일 기준 긴급여부 자동판정. 긴급+기존
    협력사 있으면 비딩 스킵하고 카탈로그로, 긴급인데 신규탐색이 필요하면
    자동처리 포기하고 MR을 Cancel.
  [2단계] decide_bidding_choice - 비딩/카탈로그 자동판정 (decide_bidding.py
    기존 규칙 그대로 신뢰, 사람 개입 없음)
  [2.5단계] create_catalog_po - 비딩 불필요/긴급직행 케이스: RFQ 없이 최근
    거래 협력사에게 바로 PO 준비
  [3단계] resolve_suppliers_choice - 기존 공급사 풀 판정, 신규탐색 필요여부 자동분기
  [4단계] search_new_suppliers - 신규 공급사 탐색(3소스 병렬)
  [5단계] select_rfq_targets - RFQ 보낼 공급사 선택/등록 (HITL). 이메일 없는
    업체는 하드블록 대신 자동 제외. 후보 0건이면 사람이 직접 공급사를
    입력하거나 MR을 취소할 수 있음.
  [6단계] create_rfq - RFQ 생성+발송
  [7단계] check_quotations - 견적 확인(반복 가능) (HITL)
  [8단계] final_selection - 최종 공급사 선정 (HITL)
  [8.5단계] po_approval - PO 발송 전 사람 승인 (HITL, 신규) - 비딩 경로/
    카탈로그 경로 둘 다 이 게이트를 거침
  [9단계] create_po - 승인된 공급사로 PO 전환 + 발송 (RFQ 기반 또는 카탈로그
    직접생성 두 경로)

⚠️ 미구현/단순화로 END에서 멈추는 분기(사람이 수동으로 이어받아야 함):
  - human_review(각 단계 실패/후보없음/긴급인데 신규탐색 필요): 담당자
    직접 확인 필요
  - MR 반려(reject_material_request.py, 그래프 밖에서 처리): 코멘트만
    남기고 Draft 유지, 상태변경 없음
  - substitute_selected(대체품 선택됨): Draft MR을 바로 삭제. 대체품으로
    새 MR을 만드는 것 자체는 여전히 수동.
  - create_catalog_po: 품목마다 최근 거래 협력사가 다를 수 있는데 지금은
    "가장 많이 등장하는 협력사 하나"로 단순화함 - 실제 운영 전 검증 필요.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypedDict

from langgraph.graph import END
from langgraph.types import Command, interrupt

# 긴급 판정 기준(요구사항 확정, 2026-09-01): 요청납기일 - 오늘 <= 이 값(일)이면 긴급.
URGENT_DUE_DAYS = 3


class PurchaseProcessState(TypedDict, total=False):
    entrypoint: str
    mr_name: str
    case_id: str
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
    is_catalog_po: bool
    po_name: str
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
    """
    일반 시작 라우팅. 나중에 단계 추가되면 entrypoint별 분기 추가.

    ⚠️ 이 함수가 호출되는 시점 자체가 사실상 "MR 승인" 행위임(2026-09-01
    확정) - MR 반려는 이 그래프 안에 들어오지 않고 reject_material_request.py
    가 그래프 밖에서 별도로 처리함(코멘트만 남기고 Draft 유지). 즉 "승인"은
    곧 이 파이프라인을 시작하는 것 그 자체.

    2026-08-31 추가: 그래프 맨 처음(START 다음) 노드라 MR당 딱 1번만
    실행됨(재개/resume은 interrupt된 노드에서 바로 이어감, 여기로
    다시 안 옴) - 그래서 케이스 생성을 여기 한 곳에서만 함. 이후 모든
    노드는 state["case_id"]로 이 케이스를 계속 재사용(process_graph.py의
    공용 로깅 wrapper가 매 노드 상태전이를 자동으로 case_status_history에
    남김).
    """
    from backend_logic2.nodes.supplier.tools.case_logging import create_case

    case_id = state.get("case_id") or create_case(mr_name=state.get("mr_name"), status="started")
    return Command(update={"entrypoint": "", "case_id": case_id, "status": "checking_mr_item"}, goto="check_mr_item")


def check_mr_item_command(state: PurchaseProcessState) -> Command:
    """[1단계] MR 품목별로 대체품 존재여부 확인. 이 시점엔 MR이 아직
    Draft임(Submit은 confirm_no_substitute에서만 함).
    있으면 substitute_selection(사람이 고름)으로, 없으면 대체품 미사용
    확정 단계로."""
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

    return Command(
        update={"substitute_results": substitute_results, "status": "confirming_no_substitute"},
        goto="confirm_no_substitute",
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
        return Command(
            update={"status": "confirming_no_substitute", "error": ""},
            goto="confirm_no_substitute",
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

    _delete_mr_for_substitute(state["mr_name"], choice)

    return Command(
        update={"status": "substitute_selected", "selected_substitute": choice, "error": ""},
        goto=END,
    )


def _delete_mr_for_substitute(mr_name: str, item_code: str) -> None:
    """대체품 선택 확정(2026-09-01, 사용자 확인) - 이 시점엔 MR이 아직
    Submit 전(Draft)이라(Submit은 confirm_no_substitute에서만 일어나고
    이 분기는 그쪽을 안 탐), 표준 ERPNext Discard로 완전히 삭제함 -
    요청부서가 대체품을 쓰기로 확정했으니 원본 MR은 더 이상 필요 없어짐.

    코멘트는 남기지 않음 - 문서 자체가 삭제되면 거기 달린 코멘트도 같이
    사라져서 의미가 없기 때문(사용자 확인: "대체품은 그냥 지우는게 맞아").

    실패해도(erp_discard_draft 자체가 실패) 그래프 진행 자체를 막지
    않음 - 이미 확정된 사람의 결정이라, ERPNext 쪽 후처리가 실패했다고
    되돌릴 이유는 없음(fail-open, 로그만 남김).
    """
    from backend_logic2.integrations.erp_client import erp_discard_draft, ERPNextAPIError

    try:
        erp_discard_draft("Material Request", mr_name)
    except ERPNextAPIError as e:
        print(f"[substitute_selection] MR 삭제 실패({mr_name}): {e}")


def _cancel_mr_with_comment(mr_name: str, reason: str) -> None:
    """이미 Submit된 MR을 자동처리 포기하고 사람에게 넘길 때 씀(2026-09-01) -
    코멘트로 사유를 남기고 Cancel 처리. Draft 삭제(_delete_mr_for_substitute)와
    달리 Cancel은 문서를 지우지 않으므로 코멘트가 그대로 남아 담당자가
    확인할 수 있음. 실패해도 그래프 진행은 막지 않음(fail-open)."""
    from backend_logic2.integrations.erp_client import erp_add_comment, erp_cancel, ERPNextAPIError

    try:
        erp_add_comment("Material Request", mr_name, reason)
    except ERPNextAPIError as e:
        print(f"[_cancel_mr_with_comment] 코멘트 등록 실패({mr_name}): {e}")

    try:
        erp_cancel("Material Request", mr_name)
    except ERPNextAPIError as e:
        print(f"[_cancel_mr_with_comment] MR Cancel 실패({mr_name}): {e}")


def confirm_no_substitute_command(state: PurchaseProcessState) -> Command:
    """[0단계] 대체품 미사용이 확정된 시점(대체품 자체가 없었거나, 사람이
    신규구매를 선택함) - 여기서만 Draft MR을 실제로 Submit해서 Pending으로
    전환함(2026-09-01, "Draft-first" 구조 전환의 핵심 지점). Submit 실패는
    fail-open으로 넘기지 않고 바로 human_review로 보냄 - Submit 안 된
    MR을 갖고 뒷단계(비딩판정, RFQ 등)가 진행되면 더 큰 혼란이 생기기
    때문(예: create_rfq는 애초에 Submit 안 된 MR이면 실패하게 짜여있음)."""
    from backend_logic2.integrations.erp_client import erp_submit, ERPNextAPIError

    mr_name = state["mr_name"]
    try:
        erp_submit("Material Request", mr_name)
    except ERPNextAPIError as e:
        return Command(
            update={"status": "human_review", "error": f"MR Submit 실패: {e}"},
            goto=END,
        )

    return Command(update={"status": "checking_urgency"}, goto="check_urgency")


def check_urgency_command(state: PurchaseProcessState) -> Command:
    """[1.5단계] 긴급 여부를 요청납기일(schedule_date) 기준으로 계산함
    (사용자 확인, 2026-09-01): (schedule_date - 오늘) <= URGENT_DUE_DAYS
    (3일)이면 긴급. ERPNext에 별도 필드가 있는 게 아니라 매번 여기서
    계산함.
      - 긴급 + 기존 협력사 있음: 비딩 전부 스킵하고 카탈로그 방식(최근
        거래 협력사에게 바로 진행)으로 합류
      - 긴급 + 기존 협력사 없음(신규탐색 필요): 억지로 비딩 진행하지
        않고 MR을 Cancel + 코멘트로 담당자에게 넘김
      - 비긴급: 기존 decide_bidding_choice 규칙 그대로 감(이 규칙 안에도
        별도의 긴급발주 판정이 있지만 그건 품목 단위 기준이 달라서
        별개로 유지함)"""
    from backend_logic2.integrations.erp_client import erp_get_one
    from backend_logic2.nodes.supplier.resolve_supplier_pool import resolve_supplier_pool

    mr_name = state["mr_name"]
    mr = erp_get_one("Material Request", mr_name) or {}
    schedule_date = mr.get("schedule_date")

    urgent = False
    if schedule_date:
        try:
            due = datetime.strptime(str(schedule_date)[:10], "%Y-%m-%d").date()
            urgent = (due - date.today()).days <= URGENT_DUE_DAYS
        except ValueError:
            urgent = False

    if not urgent:
        return Command(update={"status": "checking_bidding"}, goto="decide_bidding_choice")

    item_codes = [row.get("item_code") for row in mr.get("items", []) if row.get("item_code")]
    pool = resolve_supplier_pool(item_codes, case_id=state.get("case_id"))

    if pool["existing_candidates"]:
        print(f"\n[긴급판정] '{mr_name}' 긴급 + 기존 협력사 있음 -> 카탈로그 방식으로 즉시 진행")
        return Command(
            update={"existing_supplier_candidates": pool["existing_candidates"], "status": "catalog_purchase"},
            goto="create_catalog_po",
        )

    print(f"\n[긴급판정] '{mr_name}' 긴급이나 기존 협력사 없음 -> 자동처리 포기, Cancel")
    _cancel_mr_with_comment(
        mr_name,
        "[AI Procurement] 긴급 발주 건이나 기존 거래 협력사가 없어 신규탐색/비딩이 필요합니다. "
        "긴급 일정상 자동 진행이 어려워 담당자 확인이 필요합니다.",
    )
    return Command(
        update={"status": "human_review", "error": "긴급 MR, 기존 협력사 없어 자동처리 불가"},
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
            update={"bidding_results": bidding_results, "status": "catalog_purchase"},
            goto="create_catalog_po",
        )

    return Command(
        update={
            "bidding_results": bidding_results,
            "bidding_items": bidding_items,
            "status": "resolving_suppliers",
        },
        goto="resolve_suppliers_choice",
    )


def _find_most_recent_supplier(item_code: str) -> str | None:
    """이 품목을 가장 최근에 거래한 협력사 이름 (Submit된 PO 기준)."""
    from backend_logic2.integrations.erp_client import erp_get

    orders = erp_get(
        "Purchase Order",
        filters=[["Purchase Order Item", "item_code", "=", item_code], ["docstatus", "=", 1]],
        fields=["name", "supplier", "transaction_date"],
        order_by="transaction_date desc",
        limit=1,
    )
    return orders[0]["supplier"] if orders else None


def _last_rate_from_supplier(item_code: str, supplier: str) -> float | None:
    """이 품목을 이 협력사한테서 가장 최근에 산 단가 (Submit된 PO 기준)."""
    from backend_logic2.integrations.erp_client import erp_get, erp_get_one

    orders = erp_get(
        "Purchase Order",
        filters=[
            ["Purchase Order Item", "item_code", "=", item_code],
            ["supplier", "=", supplier],
            ["docstatus", "=", 1],
        ],
        fields=["name"],
        order_by="transaction_date desc",
        limit=1,
    )
    if not orders:
        return None
    po_doc = erp_get_one("Purchase Order", orders[0]["name"]) or {}
    for item in po_doc.get("items", []):
        if item.get("item_code") == item_code:
            return item.get("rate")
    return None


def create_catalog_po_command(state: PurchaseProcessState) -> Command:
    """[2.5단계] 카탈로그 방식(비딩 불필요 판정, 또는 긴급+기존협력사 있음) -
    RFQ/견적비교 없이 이 MR 품목들을 가장 최근에 거래한 협력사에게 바로
    발주 준비. ⚠️ 품목마다 최근 거래 협력사가 다를 수 있는데, 지금은
    "품목별 최근 거래 협력사 중 가장 많이 나오는 협력사 하나"로 단순화함
    (품목별로 다른 공급사에 나눠 발주하는 건 지금 범위 밖) - 실제 운영
    투입 전 검증 필요."""
    from collections import Counter
    from backend_logic2.integrations.erp_client import erp_get_one

    mr_name = state["mr_name"]
    mr = erp_get_one("Material Request", mr_name) or {}
    item_codes = [row.get("item_code") for row in mr.get("items", []) if row.get("item_code")]

    suppliers = [s for s in (_find_most_recent_supplier(code) for code in item_codes) if s]
    if not suppliers:
        return Command(
            update={"status": "human_review", "error": "카탈로그 방식 대상이나 과거 거래 협력사를 찾지 못했습니다."},
            goto=END,
        )

    supplier = Counter(suppliers).most_common(1)[0][0]
    print(f"\n[카탈로그 발주] '{mr_name}' -> 최근 거래 협력사: {supplier}")

    return Command(
        update={"selected_supplier": supplier, "is_catalog_po": True, "status": "awaiting_po_approval"},
        goto="po_approval",
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


def _apply_supplier_reply_deadline(supplier_name: str, days) -> None:
    """공급사(거래처) 문서의 custom_rfq_reply_deadline_days 필드를 설정함
    (2026-09-01) - remind_rfq.py(팀원 작업, RFQ 독촉메일)가 이 필드를 이미
    읽고 있어서, 후보 0건일 때 사람이 직접 입력한 견적마감일(기본 3일)을
    RFQ 자체가 아니라 여기 저장해두면 별도 필드 추가 없이 그대로 연동됨.
    이 커스텀필드가 ERPNext에 아직 없으면 조용히 실패하고 넘어감(fail-open,
    remind_rfq.py도 같은 전제로 짜여있음)."""
    if not days:
        return
    import requests
    from backend_logic2.integrations.erp_client import SITE_URL, HEADERS

    try:
        requests.put(
            f"{SITE_URL}/api/resource/Supplier/{supplier_name}",
            headers=HEADERS,
            json={"custom_rfq_reply_deadline_days": int(days)},
        )
    except Exception as e:
        print(f"  [select_rfq_targets] {supplier_name} 마감일 커스텀필드 저장 실패(무시): {e}")


def select_rfq_targets_command(state: PurchaseProcessState) -> Command:
    """[5단계-대기] RFQ 보낼 대상 선택. existing_pool_sufficient로 바로
    온 경우엔 supplier_candidates가 아직 안 채워져 있을 수 있어서,
    그럴 땐 existing_supplier_candidates를 그대로 씀.

    ⚠️ 후보 0건(2026-09-01 변경): 예전엔 바로 human_review로 끝냈는데,
    이제 사람한테 (직접 공급사 입력해서 발송 / MR 취소) 둘 중 선택하게
    함(사용자 확인)."""
    raw_candidates = state.get("supplier_candidates") or state.get("existing_supplier_candidates", [])
    candidates = [
        dict(candidate) if isinstance(candidate, dict) else {"name": str(candidate), "registered": True}
        for candidate in raw_candidates
    ]
    names = [candidate.get("name") for candidate in candidates if candidate.get("name")]

    if not candidates:
        answer = interrupt({
            "type": "no_rfq_candidates",
            "mr_name": state["mr_name"],
            "instructions": "공급사 후보를 확보하지 못했습니다. manual_supplier로 "
                             "{name, email, reply_deadline_days(기본 3)}를 직접 입력해서 "
                             "발송하거나, decision='cancel'로 MR을 취소하세요.",
        })
        decision = _decision_value(answer)
        manual = answer.get("manual_supplier") if isinstance(answer, dict) else None

        if decision == "cancel":
            _cancel_mr_with_comment(
                state["mr_name"],
                "[AI Procurement] 공급사 후보를 확보하지 못해 담당자가 직접 확인 후 취소했습니다.",
            )
            return Command(
                update={"status": "human_review", "error": "공급사 후보 없음, MR 취소됨"},
                goto=END,
            )

        if isinstance(manual, dict) and str(manual.get("name") or "").strip() and str(manual.get("email") or "").strip():
            manual_row = {
                "name": str(manual["name"]).strip(),
                "email": str(manual["email"]).strip(),
                "reply_deadline_days": manual.get("reply_deadline_days") or 3,
                "manual": True,
            }
            return Command(
                update={"supplier_candidates": [manual_row], "status": "awaiting_supplier_approval", "error": ""},
                goto="select_rfq_targets",
            )

        return Command(
            update={
                "status": "awaiting_supplier_approval",
                "error": "manual_supplier(name, email 필수) 또는 decision='cancel' 중 하나를 입력하세요.",
            },
            goto="select_rfq_targets",
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

    # ⚠️ 이메일 없는 업체는 더 이상 하드블록하지 않음(2026-09-01 변경,
    # 사용자 확인) - 이메일 있는 업체한테만 자동으로 발송하고, 없는
    # 업체는 조용히 제외함. 선택한 업체 전부 이메일이 없을 때만 막음.
    selected_candidates_all = [row for row in candidates if row.get("name") in selected]
    missing_email = [row["name"] for row in selected_candidates_all if not str(row.get("email") or "").strip()]
    selected_candidates = [row for row in selected_candidates_all if str(row.get("email") or "").strip()]

    if missing_email:
        print(f"  [select_rfq_targets] 이메일 없는 업체는 자동 제외하고 진행: {missing_email}")

    if not selected_candidates:
        return Command(
            update={
                "supplier_candidates": candidates,
                "status": "awaiting_supplier_approval",
                "error": f"선택한 업체 전부 이메일이 없습니다. 이메일을 입력하거나 다른 업체를 선택하세요: {missing_email}",
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

    # 직접입력한 후보에 견적마감일(reply_deadline_days)이 있으면 그 값을
    # Supplier 커스텀필드에 반영(친구의 remind_rfq.py가 그대로 읽어감).
    for candidate in selected_candidates:
        if candidate.get("reply_deadline_days"):
            _apply_supplier_reply_deadline(candidate["name"], candidate["reply_deadline_days"])

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
    """[8단계-대기] 순위목록 보여주고 최종 공급사 선정 물어봄.
    ⚠️ 예전엔 여기서 바로 create_po로 갔는데, 이제 po_approval(PO 발송
    전 사람 승인)을 거쳐야 함(2026-09-01, 사용자 확인)."""
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
        update={
            "selected_supplier": supplier,
            "is_catalog_po": False,
            "status": "awaiting_po_approval",
            "error": "",
        },
        goto="po_approval",
    )


def po_approval_command(state: PurchaseProcessState) -> Command:
    """[8.5단계-대기, 신규] PO 발송 전 사람 승인 (2026-09-01, 사용자 확인:
    "PO 발송 전, 사용자의 승인이 필요"). 비딩 경로(final_selection에서)와
    카탈로그 경로(create_catalog_po에서) 둘 다 여기를 거쳐야 실제 PO가
    발송됨.

    반려 처리(2026-09-02 확정, "reject"와 "force_reject" 두 가지):
      - reject (일반 반려): 비딩 경로(is_catalog_po=False)면 이미 견적
        (quotation_ranking)까지 받아둔 상태라, 반려된 업체만 후보에서
        빼고 final_selection으로 일단 되돌려서 다른 업체를 다시 고를
        기회를 줌. 뺐더니 고를 업체가 하나도 안 남으면(원래 견적업체가
        1곳뿐이었거나 다 반려됐으면) MR을 Cancel. 카탈로그 경로
        (is_catalog_po=True)는 "최근 거래 협력사"를 자동으로 하나만
        골라오는 방식이라 되돌려도 같은 업체가 또 나올 뿐이라 다시
        물어볼 이유가 없음 - 바로 MR을 Cancel.
      - force_reject (강제 반려, 2026-09-02 추가): 비딩 경로에서 다른
        업체가 남아있어도 상관없이 무조건 바로 MR을 Cancel. reject의
        "업체 남아있으면 다시 골라보게" 재시도 루프를 건너뛰고 싶을 때
        씀(예: 이 RFQ/견적 자체를 못 믿겠다, 처음부터 다시 해야 한다고
        판단한 경우). 카탈로그 경로에서는 어차피 reject랑 동작이 같음
        (대안 자체가 없어서).
      (2026-09-02: 반려 시 MR을 살려두는 안은 폐기 - 살려두면 대시보드의
      Draft/Pending MR 목록이 "사실상 끝났는데 안 끝난 것처럼" 계속
      쌓여 번잡해진다는 이유. reject/force_reject 둘 다 최종적으로
      Cancel되는 경로에서는 Cancel로 통일해서 목록을 깔끔하게 유지함.)
      반려 사유는 반드시 ERPNext 코멘트로 남김(answer에 reason이 없으면
      "사유 미입력"으로 남김 - 나중에 프론트 text form에서 reason을
      채워 보내면 그대로 반영됨)."""
    answer = interrupt({
        "type": "po_approval",
        "mr_name": state["mr_name"],
        "selected_supplier": state.get("selected_supplier"),
        "is_catalog_po": bool(state.get("is_catalog_po")),
        "rfq_name": state.get("rfq_name"),
        "instructions": "PO를 발송하려면 decision='approve', 중단하려면 decision='reject'"
                        "(다른 업체 있으면 재선택 기회를 줌) 또는 decision='force_reject'"
                        "(무조건 바로 MR 취소), 선택: reason으로 사유 입력.",
    })
    decision = _decision_value(answer)
    if decision == "approve":
        return Command(update={"status": "creating_po", "error": ""}, goto="create_po")
    if decision in ("reject", "force_reject"):
        from backend_logic2.integrations.erp_client import erp_add_comment, ERPNextAPIError

        reason = str(answer.get("reason") or "").strip() if isinstance(answer, dict) else ""
        rejected_supplier = state.get("selected_supplier")
        label = "강제 반려" if decision == "force_reject" else "반려"
        comment = f"[AI Procurement] PO 발송이 {label}되었습니다." + (f" 사유: {reason}" if reason else " (사유 미입력)")
        if rejected_supplier:
            comment += f" (반려된 공급사: {rejected_supplier})"

        if state.get("is_catalog_po"):
            _cancel_mr_with_comment(state["mr_name"], comment + " (카탈로그 방식은 대안업체 자동재선정이 불가해 바로 취소합니다.)")
            return Command(
                update={"status": "human_review", "error": f"PO 발송이 {label}되어 MR이 취소되었습니다."},
                goto=END,
            )

        if decision == "force_reject":
            _cancel_mr_with_comment(state["mr_name"], comment + " (강제 반려 - 남은 견적 업체 존재 여부와 무관하게 바로 취소합니다.)")
            return Command(
                update={"status": "human_review", "error": "PO 발송이 강제 반려되어 MR이 취소되었습니다."},
                goto=END,
            )

        remaining = [r for r in state.get("quotation_ranking", []) if r.get("supplier") != rejected_supplier]
        if not remaining:
            _cancel_mr_with_comment(state["mr_name"], comment + " 다시 선택할 다른 견적 업체가 없어 MR을 취소합니다.")
            return Command(
                update={"status": "human_review", "error": "PO 발송 반려, 대안 업체 없어 MR 취소됨"},
                goto=END,
            )

        try:
            erp_add_comment("Material Request", state["mr_name"], comment)
        except ERPNextAPIError as e:
            print(f"[po_approval] 반려 코멘트 등록 실패({state['mr_name']}): {e}")

        return Command(
            update={
                "quotation_ranking": remaining,
                "selected_supplier": "",
                "status": "awaiting_final_selection",
                "error": "",
            },
            goto="final_selection",
        )

    return Command(
        update={"status": "awaiting_po_approval", "error": "approve, reject, force_reject 중 하나를 선택하세요."},
        goto="po_approval",
    )


def _create_catalog_po_direct(state: PurchaseProcessState) -> Command:
    """카탈로그 경로의 실제 PO 생성 - RFQ/Supplier Quotation 없이 MR
    품목을 곧바로 Purchase Order로 변환함(2026-09-01). create_rfq()가
    ERPNext 내부 매퍼에 의존하지 않고 REST로 직접 payload를 구성하는
    것과 같은 방식을 그대로 따름. ⚠️ 이 경로는 이번에 새로 짠 것이라
    실제 ERPNext 인스턴스에서 검증되지 않았음 - RFQ 경로(create_and_send_po.py)
    처럼 여러 번 실전 테스트를 거친 코드가 아니므로, 운영 투입 전에
    반드시 테스트 MR로 한 번 실행해서 Purchase Order가 기대대로
    생성되는지 확인 필요."""
    from backend_logic2.integrations.erp_client import (
        erp_get_one, erp_post, erp_submit, erp_send_email, is_test_mode, ERPNextAPIError,
    )

    mr_name = state["mr_name"]
    supplier = state.get("selected_supplier")
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return Command(update={"status": "human_review", "error": "MR을 찾을 수 없습니다."}, goto=END)

    items_payload = []
    for row in mr.get("items", []):
        item_code = row.get("item_code")
        if not item_code:
            continue
        rate = _last_rate_from_supplier(item_code, supplier)
        items_payload.append({
            "item_code": item_code,
            "item_name": row.get("item_name"),
            "description": row.get("description"),
            "qty": row.get("qty"),
            "rate": rate or 0,
            "stock_uom": row.get("stock_uom") or row.get("uom"),
            "uom": row.get("uom") or row.get("stock_uom"),
            "conversion_factor": row.get("conversion_factor") or 1,
            "schedule_date": row.get("schedule_date") or mr.get("schedule_date"),
            "warehouse": row.get("warehouse"),
            "material_request": mr_name,
            "material_request_item": row.get("name"),
        })

    if not items_payload:
        return Command(update={"status": "human_review", "error": "카탈로그 PO로 옮길 품목이 없습니다."}, goto=END)

    payload = {
        "supplier": supplier,
        "company": mr.get("company"),
        "transaction_date": date.today().isoformat(),
        "schedule_date": mr.get("schedule_date"),
        "items": items_payload,
    }

    try:
        po = erp_post("Purchase Order", payload)
        erp_submit("Purchase Order", po["name"])
    except ERPNextAPIError as e:
        return Command(update={"status": "human_review", "error": f"카탈로그 PO 생성 실패: {e}"}, goto=END)

    test_mode = is_test_mode()
    if not test_mode:
        try:
            # 2026-09-02 수정: 처음엔 포털링크 없이 "등록되었습니다" 문구만
            # 보냈었는데, create_and_send_po.py(RFQ 경로)는 원래부터 포털링크를
            # 같이 보내고 있어서 두 경로 이메일 내용이 안 맞았음(사용자가
            # 카탈로그 경로 메일 받아보고 "포털링크가 왜 없냐"고 확인해줌).
            # ERP_DOMAIN/ERP_PORTAL_PATH_TEMPLATE을 그대로 재사용해서 통일함
            # (경로 자체는 "/purchase-orders/{po_name}"로 이미 검증 완료 -
            # create_and_send_po.py 쪽 주석 참고).
            from backend_logic2.nodes.po.create_and_send_po import (
                ERP_DOMAIN,
                ERP_PORTAL_PATH_TEMPLATE,
            )

            supplier_doc = erp_get_one("Supplier", supplier) or {}
            email = supplier_doc.get("email_id")
            if email:
                portal_link = ERP_DOMAIN + ERP_PORTAL_PATH_TEMPLATE.format(po_name=po["name"])
                content = f"""
                <p>안녕하세요.</p>
                <p>발주서(<b>{po['name']}</b>)가 등록되었습니다.</p>
                <p>아래 링크에서 발주 내역을 확인해 주세요.</p>
                <p><a href="{portal_link}" target="_blank">발주서 상세 확인하기</a></p>
                """
                erp_send_email(
                    "Purchase Order", po["name"], email,
                    subject=f"[발주서] {po['name']}",
                    content=content,
                )
        except ERPNextAPIError as e:
            print(f"[catalog_po] 이메일 발송 실패(무시): {e}")

    print(f"  -> 카탈로그 PO 처리 완료: {po['name']}\n")
    return Command(update={"po_name": po["name"], "status": "po_sent", "error": ""}, goto=END)


def create_po_command(state: PurchaseProcessState) -> Command:
    """[9단계] 승인된 공급사로 PO 전환 + 발송. is_catalog_po면 RFQ 없이
    직접 생성(_create_catalog_po_direct), 아니면 기존 RFQ 기반 로직
    그대로(create_and_send_po.py에 위임).

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
    if state.get("is_catalog_po"):
        return _create_catalog_po_direct(state)

    from backend_logic2.integrations.erp_client import is_test_mode
    from backend_logic2.nodes.po.create_and_send_po import create_and_send_po

    rfq_name = state["rfq_name"]
    supplier = state.get("selected_supplier")
    test_mode = is_test_mode()

    print(f"\n[PO 생성] '{state['mr_name']}' (RFQ: {rfq_name}) -> 공급사: {supplier}")
    print(f"  환경: {'TEST_MODE (실제 이메일 발송 안 함)' if test_mode else '운영 모드 (실제 이메일 발송됨)'}")

    try:
        po = create_and_send_po(rfq_name, supplier, send_email=not test_mode)
    except SystemExit:
        print("  -> PO 생성/발송 중단됨 (사유는 위 콘솔 출력 참고)")
        return Command(
            update={"status": "human_review", "error": "PO 생성/발송 중 오류로 중단되었습니다. 콘솔 로그를 확인하세요."},
            goto=END,
        )

    if not po or not po.get("name"):
        print("  -> PO 생성 실패")
        return Command(
            update={"status": "human_review", "error": "PO 생성에 실패했습니다."},
            goto=END,
        )

    print(f"  -> PO 처리 완료: {po['name']} (이메일 발송: {'예' if po.get('email_sent') else '아니오'})\n")

    return Command(
        update={"po_name": po["name"], "status": "po_sent", "error": ""},
        goto=END,
    )
