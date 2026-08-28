"""CLI for starting and resuming the Command-based purchasing graph.

Examples:
    python -m backend_logic2.nodes.process_cli start --mr MAT-MR-2026-00001
    python -m backend_logic2.nodes.process_cli resume --thread MAT-MR-2026-00001 --decision approve
    python -m backend_logic2.nodes.process_cli status --thread MAT-MR-2026-00001
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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
    if args.data_file:
        parsed = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("--data-file JSON은 객체여야 합니다.")
        data.update(parsed)
    if args.decision:
        data["decision"] = args.decision
    if args.reason:
        data["reason"] = args.reason
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
        data["supplier"] = args.supplier
    if args.manifest_path:
        data["manifest_path"] = args.manifest_path
    quotation_files = getattr(args, "quotation_file", None) or []
    quotation_suppliers = getattr(args, "quotation_supplier", None) or []
    if quotation_files or quotation_suppliers:
        if args.manifest_path:
            raise ValueError("--manifest-path와 --quotation-file은 함께 사용할 수 없습니다.")
        if len(quotation_files) != len(quotation_suppliers):
            raise ValueError("각 --quotation-file마다 --quotation-supplier를 하나씩 지정해야 합니다.")
        data["manifest"] = {
            "quotations": [
                {"path": path, "supplier_name": supplier}
                for path, supplier in zip(quotation_files, quotation_suppliers)
            ]
        }
    if args.top_k is not None:
        data["top_k"] = args.top_k
    if getattr(args, "no_email", False):
        data["send_email"] = False
    if getattr(args, "draft_only", False):
        data["submit"] = False
        data["send_email"] = False
    if data.get("action") == "process" and "manifest_path" not in data and "manifest" not in data:
        data["manifest"] = {"quotations": []}
    if not data:
        raise ValueError("재개 입력이 없습니다. --decision, --action, --supplier 또는 --data를 지정하세요.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Command 기반 구매 프로세스")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="MR에서 새 프로세스 시작")
    start.add_argument("--mr", required=True, help="Material Request 이름")
    start.add_argument("--thread", help="체크포인트 thread id(기본값: MR 이름)")
    start.add_argument("--quotation-deadline", help="견적 접수 마감일 YYYY-MM-DD")

    resume = subparsers.add_parser("resume", help="사람 승인/마감 대기 상태 재개")
    resume.add_argument("--thread", required=True)
    resume.add_argument("--data", help="고급 사용: 재개 입력 JSON")
    resume.add_argument("--data-file", help="고급 사용: 재개 입력 JSON 파일")
    resume.add_argument("--decision", choices=("approve", "reject"), help="품목/MR 승인 또는 반려")
    resume.add_argument("--reason", help="반려 사유")
    resume.add_argument("--action", choices=("approve_all", "process"), help="공급사 전체 승인 또는 견적 처리")
    resume.add_argument("--suppliers", nargs="+", help="RFQ를 발송할 공급사 목록")
    resume.add_argument(
        "--supplier-email",
        nargs="+",
        help="누락 이메일 보완. 예: --supplier-email '업체A=a@example.com'",
    )
    resume.add_argument("--dismiss-suppliers", nargs="+", help="후보에서 제외할 공급사 목록")
    resume.add_argument("--supplier", help="최종 선정 공급사")
    resume.add_argument("--manifest-path", help="외부 견적 manifest JSON 경로")
    resume.add_argument(
        "--quotation-file",
        action="append",
        help="외부 견적 파일 경로. 여러 건이면 옵션을 반복",
    )
    resume.add_argument(
        "--quotation-supplier",
        action="append",
        help="바로 앞 견적 파일의 공급사명. --quotation-file과 같은 횟수로 지정",
    )
    resume.add_argument("--top-k", type=int, help="견적 추천 개수")
    resume.add_argument("--no-email", action="store_true", help="RFQ는 Submit하되 공급사 메일 발송 안 함")
    resume.add_argument("--draft-only", action="store_true", help="RFQ Draft만 생성하고 Submit/메일 발송 안 함")

    retry = subparsers.add_parser("retry", help="오류로 중단된 현재 작업 재시도")
    retry.add_argument("--thread", required=True)

    recover_rfq = subparsers.add_parser(
        "recover-rfq",
        help="RFQ 단계부터 복구(공급사 탐색은 다시 실행하지 않음)",
    )
    recover_rfq.add_argument("--thread", required=True)

    recover_suppliers = subparsers.add_parser(
        "recover-suppliers",
        help="저장된 탐색 결과로 공급사 선택 단계부터 복구(재검색 안 함)",
    )
    recover_suppliers.add_argument("--thread", required=True)

    recover_po = subparsers.add_parser(
        "recover-po",
        help="저장된 최종 공급사로 PO 생성 단계부터 복구",
    )
    recover_po.add_argument("--thread", required=True)
    recover_po.add_argument("--no-email", action="store_true", help="PO 생성·Submit 후 이메일 발송 안 함")

    status = subparsers.add_parser("status", help="저장된 프로세스 상태 확인")
    status.add_argument("--thread", required=True)

    args = parser.parse_args()
    app = get_process_app()
    if args.command == "start":
        thread_id = args.thread or args.mr
        result = app.invoke(
            {
                "mr_name": args.mr,
                "quotation_deadline": args.quotation_deadline or "",
                "status": "started",
            },
            config=_config(thread_id),
        )
    elif args.command == "resume":
        try:
            data = _build_resume_data(args)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            parser.error(f"재개 입력 오류: {exc}")
        result = app.invoke(Command(resume=data), config=_config(args.thread))
    elif args.command == "retry":
        snapshot = app.get_state(_config(args.thread))
        if not snapshot.next:
            parser.error("재시도할 작업이 없습니다. status로 현재 상태를 확인하세요.")
        if any(task.interrupts for task in snapshot.tasks):
            parser.error("현재 작업은 오류가 아니라 사람 입력 대기 상태입니다. resume을 사용하세요.")
        result = app.invoke(None, config=_config(args.thread))
    elif args.command == "recover-rfq":
        snapshot = app.get_state(_config(args.thread))
        values = snapshot.values
        if not values.get("mr_name"):
            parser.error("저장된 Material Request가 없습니다.")
        if not values.get("selected_suppliers"):
            parser.error("승인된 RFQ 공급사가 저장되어 있지 않습니다.")
        result = app.invoke(
            {"entrypoint": "create_rfq", "status": "creating_rfq", "error": ""},
            config=_config(args.thread),
        )
    elif args.command == "recover-suppliers":
        snapshot = app.get_state(_config(args.thread))
        values = snapshot.values
        candidates_by_name: dict[str, dict[str, Any]] = {}
        for item_code, search in values.get("supplier_search_results", {}).items():
            registered_names = {
                row.get("name")
                for row in search.get("registrations", [])
                if row.get("status") in {"created", "updated", "already_exists"}
            }
            for candidate in search.get("candidates", []):
                name = str(candidate.get("name") or "").strip()
                if not name:
                    continue
                record = dict(candidate)
                record.update({
                    "name": name,
                    "registered": name in registered_names or search.get("source") == "existing",
                    "item_codes": [item_code],
                })
                candidates_by_name.setdefault(name, record)
        if not candidates_by_name:
            parser.error("저장된 공급사 탐색 결과가 없습니다.")
        result = app.invoke(
            {
                "entrypoint": "supplier_approval",
                "supplier_candidates": list(candidates_by_name.values()),
                "status": "awaiting_supplier_approval",
                "error": "",
            },
            config=_config(args.thread),
        )
    elif args.command == "recover-po":
        snapshot = app.get_state(_config(args.thread))
        values = snapshot.values
        if not values.get("rfq_name") or not values.get("selected_supplier"):
            parser.error("저장된 RFQ 또는 최종 선정 공급사가 없습니다.")
        result = app.invoke(
            {
                "entrypoint": "create_po",
                "quotation_result": to_checkpoint_data(values.get("quotation_result", {})),
                "send_po_email": not args.no_email,
                "status": "creating_po",
                "error": "",
            },
            config=_config(args.thread),
        )
    else:
        snapshot = app.get_state(_config(args.thread))
        result = {
            "values": snapshot.values,
            "next": list(snapshot.next),
            "interrupts": [str(task.interrupts) for task in snapshot.tasks if task.interrupts],
        }
    print(_render(result))


if __name__ == "__main__":
    main()
