"""CLI for starting and resuming the Command-based purchasing graph.

Examples:
    python -m backend_logic2.nodes.process_cli start --mr MAT-MR-2026-00001
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --decision reject
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --decision bidding
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --decision search
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --suppliers "업체A" "업체B"
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --decision check
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --supplier "업체A"
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
    """Build resume payload without requiring inline JSON on PowerShell."""
    data: dict[str, Any] = {}
    if args.data:
        parsed = json.loads(args.data)
        if not isinstance(parsed, dict):
            raise ValueError("--data JSON은 객체여야 합니다.")
        data.update(parsed)
    if args.decision:
        # 어느 interrupt 지점이냐에 따라 이 하나의 값이 다르게 해석됨:
        #   substitute_approval: approve/reject
        #   catalog_or_bidding_interrupt: catalog/bidding
        #   supplier_source_choice: search/existing_only
        #   check_quotations: check/later
        data["decision"] = args.decision
    if args.action:
        data["action"] = args.action
    if args.suppliers:
        data["suppliers"] = args.suppliers
    supplier_emails = getattr(args, "supplier_email", None)
    if supplier_emails:
        updates = []
        for value in supplier_emails:
            if "=" not in value:
                raise ValueError("--supplier-email은 '업체명=email' 형식이어야 합니다.")
            name, email = value.split("=", 1)
            if not name.strip() or not email.strip():
                raise ValueError("--supplier-email의 업체명과 이메일은 비어 있을 수 없습니다.")
            updates.append({"name": name.strip(), "email": email.strip()})
        data["supplier_updates"] = updates
    dismissed = getattr(args, "dismiss_suppliers", None)
    if dismissed:
        data["dismiss"] = dismissed
    if args.supplier:
        # final_selection 단계에서 최종 선정 공급사로 씀
        data["supplier"] = args.supplier
    if not data:
        raise ValueError("재개 입력이 없습니다. --decision, --action, --suppliers, --supplier 또는 --data를 지정하세요.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Command 기반 구매 프로세스")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="MR에서 새 프로세스 시작")
    start.add_argument("--mr", required=True, help="Material Request 이름")
    start.add_argument("--thread", help="체크포인트 thread id(기본값: MR 이름)")

    resume = subparsers.add_parser("resume", help="대기 중인 interrupt 재개")
    resume.add_argument("--thread", required=True)
    resume.add_argument("--data", help="고급 사용: 재개 입력 JSON")
    resume.add_argument(
        "--decision",
        help="approve/reject(대체품 승인), catalog/bidding(방식 선택), "
             "search/existing_only(공급사 탐색 방식), check/later(견적 확인 시점)",
    )
    resume.add_argument("--action", choices=("approve_all",), help="공급사 전체승인(select_rfq_targets 단계용)")
    resume.add_argument("--suppliers", nargs="+", help="RFQ를 발송할 공급사 목록")
    resume.add_argument(
        "--supplier-email",
        nargs="+",
        help="누락 이메일 보완. 예: --supplier-email '업체A=a@example.com'",
    )
    resume.add_argument("--dismiss-suppliers", nargs="+", help="후보에서 제외할 공급사 목록")
    resume.add_argument("--supplier", help="최종 선정 공급사 (final_selection 단계용)")

    retry = subparsers.add_parser("retry", help="오류로 중단된 현재 작업 재시도")
    retry.add_argument("--thread", required=True)

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
        except (json.JSONDecodeError, ValueError) as exc:
            parser.error(f"재개 입력 오류: {exc}")
        result = app.invoke(Command(resume=data), config=_config(args.thread))
    elif args.command == "retry":
        snapshot = app.get_state(_config(args.thread))
        if not snapshot.next:
            parser.error("재시도할 작업이 없습니다. status로 현재 상태를 확인하세요.")
        if any(task.interrupts for task in snapshot.tasks):
            parser.error("현재 작업은 오류가 아니라 사람 입력 대기 상태입니다. resume을 사용하세요.")
        result = app.invoke(None, config=_config(args.thread))
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