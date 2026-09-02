"""process_cli.py를 매번 --thread/--decision/... 손으로 치면서 부르는 대신,
MR 이름 하나만 주면 지금 멈춰있는 interrupt 타입을 보고 뭘 물어봐야
하는지 알아서 판단해서 대화형으로 끝까지 진행시켜주는 테스트 스크립트
(2026-09-02, 수동 CLI 테스트가 너무 번거롭다는 요청으로 추가).

process_cli.py처럼 subprocess를 새로 띄우는 게 아니라 get_process_app()을
직접 불러서 한 프로세스 안에서 interrupt -> 답변 -> resume을 계속
반복함 - 매번 파이썬 새로 띄우는 오버헤드도 없고, 후보 목록/순위표를
번호로 골라서 답할 수 있어서 긴 --decision/--suppliers 문자열을 직접
조립할 필요가 없음.

⚠️ 실제 그래프(get_process_app())를 그대로 쓰기 때문에 ERPNext에 실제로
Submit/Cancel/PO생성 등이 그대로 일어남 - process_cli.py랑 완전히 같은
효과. 테스트용 MR로만 쓸 것.

사용법 (레포 루트에서):
    python -m backend_logic2.workflow.test_repl MAT-MR-2026-00286
    python -m backend_logic2.workflow.test_repl MAT-MR-2026-00286 --thread my-test-1

중간에 뭘 입력해야 할지 모르겠으면 그냥 물음표(?)를 입력하면 현재
interrupt의 원본 데이터를 그대로 보여줌.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from langgraph.types import Command

from .process_commands import to_checkpoint_data
from .process_graph import get_process_app


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _print_raw(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _print_state(result: dict[str, Any]) -> None:
    status = result.get("status")
    error = result.get("error")
    line = f"\n--- status: {status}"
    if error:
        line += f" | error: {error}"
    line += " ---"
    print(line)


def _handle_substitute_selection(payload: dict) -> dict:
    flattened = []
    for item_code, info in payload.get("substitute_results", {}).items():
        for sub in info.get("substitutes", []):
            flattened.append(sub)

    print("대체품 후보:")
    for i, s in enumerate(flattened, 1):
        fulfill = "전량충족" if s.get("fulfills_full_qty") else f"부분충족({s.get('total_qty')})"
        print(f"  {i}. {s['item_code']} - {s.get('item_name')} ({fulfill}) - {s.get('reason')}")
    print("  n. 신규구매(대체품 안 씀, new_purchase)")

    choice = _ask("선택 (번호 또는 n): ")
    if choice == "?":
        _print_raw(payload)
        return _handle_substitute_selection(payload)
    if choice.lower() == "n":
        return {"decision": "new_purchase"}
    return {"item_code": flattened[int(choice) - 1]["item_code"]}


def _handle_no_rfq_candidates(payload: dict) -> dict:
    choice = _ask("공급사 후보 0건. [c]ancel 또는 [m]anual 직접입력: ")
    if choice == "?":
        _print_raw(payload)
        return _handle_no_rfq_candidates(payload)
    if choice.lower().startswith("c"):
        return {"decision": "cancel"}

    name = _ask("  업체명: ")
    email = _ask("  이메일: ")
    days = _ask("  마감일수(기본 3, 엔터로 스킵): ")
    manual = {"name": name, "email": email}
    if days:
        manual["reply_deadline_days"] = int(days)
    return {"manual_supplier": manual}


def _handle_select_rfq_targets(payload: dict) -> dict:
    candidates = payload.get("candidates", [])
    missing = set(payload.get("missing_email", []))

    print("공급사 후보:")
    for i, c in enumerate(candidates, 1):
        flag = " [이메일없음]" if c["name"] in missing else ""
        print(f"  {i}. {c['name']}{flag} - {c.get('email') or ''} (출처: {c.get('source')})")

    raw = _ask("선택할 번호(콤마구분, all=전체승인, ?=원본보기): ")
    if raw == "?":
        _print_raw(payload)
        return _handle_select_rfq_targets(payload)

    answer: dict[str, Any] = {}
    if raw.lower() == "all":
        answer["action"] = "approve_all"
    else:
        idxs = [int(x) - 1 for x in raw.split(",") if x.strip()]
        answer["suppliers"] = [candidates[i]["name"] for i in idxs]

    fix = _ask("이메일 보완할 업체 있으면 '업체명=이메일' 콤마구분 입력(없으면 엔터): ")
    if fix:
        updates = []
        for part in fix.split(","):
            if "=" in part:
                n, e = part.split("=", 1)
                updates.append({"name": n.strip(), "email": e.strip()})
        if updates:
            answer["supplier_updates"] = updates

    return answer


def _handle_check_quotations(payload: dict) -> dict:
    choice = _ask("견적 확인 [c]heck(조회만+대기) / [l]ater(대기) / [f]inalize(최종선정으로): ")
    if choice == "?":
        _print_raw(payload)
        return _handle_check_quotations(payload)
    mapping = {"c": "check", "l": "later", "f": "finalize"}
    return {"decision": mapping.get(choice.lower(), choice)}


def _handle_final_selection(payload: dict) -> dict:
    ranking = payload.get("ranking", [])
    print("견적 순위:")
    for i, r in enumerate(ranking, 1):
        print(f"  {i}. {r}")
    choice = _ask("선택할 번호(?=원본보기): ")
    if choice == "?":
        _print_raw(payload)
        return _handle_final_selection(payload)
    return {"supplier": ranking[int(choice) - 1]["supplier"]}


def _handle_po_approval(payload: dict) -> dict:
    print(f"PO 승인 대상: {payload.get('selected_supplier')} (catalog={payload.get('is_catalog_po')}, rfq={payload.get('rfq_name')})")
    choice = _ask("[a]pprove / [r]eject(남은 업체 있으면 재선택) / [f]orce_reject(무조건 MR취소) (?=원본보기): ")
    if choice == "?":
        _print_raw(payload)
        return _handle_po_approval(payload)
    if choice.lower().startswith("a"):
        return {"decision": "approve"}
    decision = "force_reject" if choice.lower().startswith("f") else "reject"
    reason = _ask("  반려 사유(엔터로 스킵): ")
    answer = {"decision": decision}
    if reason:
        answer["reason"] = reason
    return answer


_HANDLERS = {
    "substitute_selection": _handle_substitute_selection,
    "no_rfq_candidates": _handle_no_rfq_candidates,
    "select_rfq_targets": _handle_select_rfq_targets,
    "check_quotations": _handle_check_quotations,
    "final_selection": _handle_final_selection,
    "po_approval": _handle_po_approval,
}


def _handle_interrupt(itype: str, payload: dict) -> dict:
    handler = _HANDLERS.get(itype)
    if handler is None:
        print(f"[알 수 없는 interrupt 타입: {itype}] 원본 데이터:")
        _print_raw(payload)
        raw = _ask("resume에 넘길 JSON을 직접 입력하세요: ")
        return json.loads(raw)
    return handler(payload)


def main() -> None:
    if len(sys.argv) < 2:
        print("사용법: python -m backend_logic2.workflow.test_repl <MR이름> [--thread 이름]")
        return

    mr_name = sys.argv[1]
    thread_id = mr_name
    if "--thread" in sys.argv:
        thread_id = sys.argv[sys.argv.index("--thread") + 1]

    app = get_process_app()
    config = {"configurable": {"thread_id": thread_id}}

    print(f"=== '{mr_name}' 프로세스 시작 (thread={thread_id}) ===")
    result = app.invoke({"mr_name": mr_name, "status": "started"}, config=config)

    while True:
        # __interrupt__는 우리가 interrupt()에 넘긴 dict를 그대로 담고 있는
        # 게 아니라, LangGraph의 Interrupt 객체(.value 안에 그 dict가 들어
        #있음)로 옴 - to_checkpoint_data는 이걸 모르는 타입이라 그냥
        # 통과시켜버려서 .get()이 터짐(process_cli.py의 _render()는
        # hasattr(item, "value")로 이미 처리하고 있었는데 여기선 빠뜨렸음).
        # 그래서 __interrupt__는 따로 떼서 .value를 먼저 풀고 나서
        # to_checkpoint_data를 태움.
        raw_interrupts = result.get("__interrupt__")
        display = to_checkpoint_data({k: v for k, v in result.items() if k != "__interrupt__"})
        _print_state(display)

        if not raw_interrupts:
            print("=== 종료 (더 이상 대기 중인 interrupt 없음) ===")
            _print_raw(display)
            break

        raw_payload = raw_interrupts[0]
        payload = to_checkpoint_data(getattr(raw_payload, "value", raw_payload))
        itype = payload.get("type") if isinstance(payload, dict) else None
        instructions = payload.get("instructions", "") if isinstance(payload, dict) else ""
        print(f"\n[interrupt: {itype}] {instructions}")

        while True:
            try:
                answer = _handle_interrupt(itype, payload)
                break
            except (ValueError, IndexError, KeyError) as e:
                print(f"  입력을 이해 못 했습니다({e}). 다시 입력하세요.")

        result = app.invoke(Command(resume=answer), config=config)


if __name__ == "__main__":
    main()
