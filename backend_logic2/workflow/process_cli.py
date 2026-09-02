"""CLI for starting and resuming the Command-based purchasing graph.

Examples:
    python -m backend_logic2.nodes.process_cli start --mr MAT-MR-2026-00001
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --decision new_purchase
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --item-code ITEM-SUB-106
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --suppliers "업체A" "업체B"
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --action approve_all --dismiss-suppliers "업체C"
    python -m backend_logic2.nodes.process_cli status --thread MAT-MR-2026-00001
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from langgraph.types import Command

from .process_commands import to_checkpoint_data
from .process_graph import get_process_app


def _render(value: Any) -> str:
    def default(item: Any):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if hasattr(item, "value"):
            return item.value
        return str(item)

    return json.dumps(value, ensure_ascii=False, indent=2, default=default)


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _build_resume_data(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if args.decision:
        # substitute_selection 단계에서 'new_purchase'로 신규구매 선택할 때 씀
        data["decision"] = args.decision
    if args.item_code:
        # substitute_selection 단계에서 대체품 하나를 선택할 때 씀
        data["item_code"] = args.item_code
    if args.action:
        # select_rfq_targets 단계에서 전체승인할 때 씀
        data["action"] = args.action
    if args.suppliers:
        # select_rfq_targets 단계에서 번호 대신 이름으로 직접 선택할 때 씀
        data["suppliers"] = args.suppliers
    if args.dismiss_suppliers:
        data["dismiss"] = args.dismiss_suppliers
    if args.supplier_email:
        updates = []
        for value in args.supplier_email:
            if "=" not in value:
                raise ValueError("--supplier-email은 '업체명=email' 형식이어야 합니다.")
            name, email = value.split("=", 1)
            updates.append({"name": name.strip(), "email": email.strip()})
        data["supplier_updates"] = updates
    if args.supplier:
        # final_selection 단계에서 최종 선정 공급사로 씀
        data["supplier"] = args.supplier
    if args.manual_supplier:
        # select_rfq_targets 단계에서 후보 0건일 때 공급사를 직접 입력할 때 씀
        value = args.manual_supplier
        if "=" not in value:
            raise ValueError("--manual-supplier는 '업체명=email[:마감일수]' 형식이어야 합니다.")
        name, rest = value.split("=", 1)
        email, _, days = rest.partition(":")
        manual = {"name": name.strip(), "email": email.strip()}
        if days.strip():
            manual["reply_deadline_days"] = int(days.strip())
        data["manual_supplier"] = manual
    if args.reason:
        # po_approval 단계에서 --decision reject랑 같이 써서 반려 사유를 ERPNext 코멘트로 남김
        data["reason"] = args.reason
    if not data:
        raise ValueError("재개 입력이 없습니다. --decision, --item-code, --action, --suppliers 중 하나 이상을 지정하세요.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Command 기반 구매 프로세스")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="MR에서 새 프로세스 시작")
    start.add_argument("--mr", required=True, help="Material Request 이름")
    start.add_argument("--thread", help="체크포인트 thread id(기본값: MR 이름)")

    resume = subparsers.add_parser("resume", help="대기 중인 interrupt 재개")
    resume.add_argument("--thread", required=True)
    resume.add_argument(
        "--decision",
        help="'new_purchase'(대체품 선택), 'check'(견적 조회+계속대기)/"
             "'later'(대기)/'finalize'(견적 확정하고 최종선정 단계로), "
             "'approve'/'reject'(남은 업체 있으면 재선택)/'force_reject'"
             "(무조건 MR취소, po_approval 단계용), "
             "'cancel'(후보0건일 때 MR 취소용)",
    )
    resume.add_argument("--item-code", help="선택할 대체품의 item_code (대체품 선택 단계용)")
    resume.add_argument("--action", choices=("approve_all",), help="공급사 전체승인(select_rfq_targets 단계용)")
    resume.add_argument("--suppliers", nargs="+", help="RFQ를 발송할 공급사 목록")
    resume.add_argument("--dismiss-suppliers", nargs="+", help="후보에서 제외할 공급사 목록")
    resume.add_argument("--supplier", help="최종 선정 공급사 (final_selection 단계용)")
    resume.add_argument(
        "--supplier-email",
        nargs="+",
        help="누락 이메일 보완. 예: --supplier-email '업체A=a@example.com'",
    )
    resume.add_argument(
        "--manual-supplier",
        help="공급사 후보 0건일 때 직접 입력. 예: --manual-supplier '업체A=a@example.com:5' "
             "(마감일수 생략시 기본 3일). MR 취소는 --decision cancel.",
    )
    resume.add_argument(
        "--reason",
        help="po_approval 단계에서 --decision reject와 같이 써서 반려 사유를 ERPNext 코멘트로 남김.",
    )

    status = subparsers.add_parser("status", help="저장된 프로세스 상태 확인")
    status.add_argument("--thread", required=True)

    args = parser.parse_args()
    app = get_process_app()

    if args.command == "start":
        thread_id = args.thread or args.mr
        result = app.invoke(
            {"mr_name": args.mr, "status": "started"},
            config=_config(thread_id),
        )
    elif args.command == "resume":
        try:
            data = _build_resume_data(args)
        except ValueError as exc:
            parser.error(str(exc))
        result = app.invoke(Command(resume=data), config=_config(args.thread))
    else:
        snapshot = app.get_state(_config(args.thread))
        result = {
            "values": snapshot.values,
            "next": list(snapshot.next),
            "interrupts": [str(task.interrupts) for task in snapshot.tasks if task.interrupts],
        }

    print(_render(to_checkpoint_data(result)))


if __name__ == "__main__":
    main()