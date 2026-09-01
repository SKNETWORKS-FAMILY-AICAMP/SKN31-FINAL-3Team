"""
nodes/mr/substitute_reply_watcher.py — ERPNext MR 댓글로 온 대체품 선택
답장을 감지해서 그래프를 재개(resume)시키는 폴링 스크립트 (2026-09-01).

배경: process_graph.py의 substitute_selection 노드는 interrupt()로 멈춰서
누군가 resume을 호출해주기만 기다림. 예전엔 이걸 사람이 CLI로 직접
(`process_cli resume --thread <MR명> --item-code ...`) 쳐줘야 했는데, 실제
결정권자는 요청부서라 CLI를 쓰게 할 수 없음. 그래서 요청부서가 ERPNext MR
화면에서 평소 쓰던 댓글로 답장하면 이 스크립트가 그걸 읽어서 대신 resume을
호출해줌 - 새 API 엔드포인트/Client Script 없이 기존 ERPNext 댓글+할당
기능만으로 처리(find_substitute.py의 notify_requester_of_substitutes가
반대편에서 댓글+할당을 남김).

동작:
  1. procurement.procurement_case에서 status='awaiting_substitute_selection'인
     MR 목록 조회 (지금 실제로 답장을 기다리는 중인 MR들).
  2. 각 MR의 댓글 타임라인 조회 -> 우리가 마지막으로 단 "[AI Procurement]"
     댓글(anchor) 다음에 새로 달린 댓글이 있으면 그게 사람의 답장.
  3. 답장을 파싱(숫자 -> 그 번호에 해당하는 item_code / '구매'·'구입'
     포함 -> new_purchase) -> process_graph 앱에 Command(resume=...)으로
     넣어줌.
  4. resume 후에도 상태가 여전히 awaiting_substitute_selection이면(잘못된
     답장, 그래프 자체 검증에 걸림) 그 사유를 "[AI Procurement]" 댓글로
     다시 안내함 - 이 새 댓글이 anchor가 갱신되는 효과라, 같은(무효한)
     답장을 다음 폴링에서 또 처리해버리는 걸 막아줌.

실행 트리거는 아직 안 정해짐 - 주기적 실행(cron 등)은 나중에 서버관리
담당이 붙이면 됨. 여기는 "한 번 싹 돌면서 처리"하는 run_once()만 있으면
충분.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/mr/이 파일
실행: python -m backend_logic2.nodes.mr.substitute_reply_watcher
"""

from __future__ import annotations

import re

from langgraph.types import Command

from backend_logic2.integrations.erp_client import erp_get, erp_add_comment, ERPNextAPIError
from backend_logic2.nodes.mr.find_substitute import flatten_substitute_candidates
from backend_logic2.workflow.process_graph import get_process_app

ANCHOR_PREFIX = "[AI Procurement]"


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _plain_text(content: str) -> str:
    """ERPNext 댓글은 리치텍스트 에디터를 거치면 <div>/<p> 등으로 감싸져서
    올 수 있어서, 접두어 비교/답장 파싱 전에 태그를 걷어냄."""
    plain = re.sub(r"<[^>]+>", " ", content or "")
    return re.sub(r"\s+", " ", plain).strip()


def get_mrs_awaiting_substitute_selection() -> list:
    """지금 답장을 기다리는 중(status='awaiting_substitute_selection')인 MR 목록."""
    try:
        from procurement_db import get_connection
    except ImportError:
        print("[substitute_reply_watcher] procurement_db 모듈을 못 찾음")
        return []
    try:
        with get_connection(autocommit=True) as conn:
            rows = conn.execute(
                "SELECT mr_name FROM procurement.procurement_case "
                "WHERE status = 'awaiting_substitute_selection' AND mr_name IS NOT NULL"
            ).fetchall()
    except Exception as e:
        print(f"[substitute_reply_watcher] 대기 MR 조회 실패: {e}")
        return []
    return [row["mr_name"] for row in rows]


def _get_comments(mr_name: str) -> list:
    comments = erp_get(
        "Comment",
        filters=[
            ["reference_doctype", "=", "Material Request"],
            ["reference_name", "=", mr_name],
        ],
        fields=["name", "content", "creation"],
        order_by="creation asc",
    )
    return comments or []


def _find_reply_after_anchor(comments: list):
    """마지막 '[AI Procurement]' 댓글(anchor) 다음에 달린 첫 댓글을 답장으로 간주."""
    anchor_idx = None
    for i, c in enumerate(comments):
        if _plain_text(c.get("content")).startswith(ANCHOR_PREFIX):
            anchor_idx = i
    if anchor_idx is None:
        return None
    after = comments[anchor_idx + 1:]
    return after[0] if after else None


def _parse_reply(content: str, flattened_candidates: list):
    """반환: process_cli._build_resume_data와 같은 형태의 dict, 인식 못하면 None."""
    plain = _plain_text(content)
    if not plain:
        return None

    # ⚠️ 숫자를 '구매'/'구입' 키워드보다 먼저 확인함 - ERPNext 타임라인의
    # "답장(Reply)" 기능으로 답하면 원본 댓글(우리가 단 안내문, "원래 품목을
    # 그대로 구매하려면: '구매'"라는 문구가 들어있음)을 인용해서 같이 붙여
    # 보내는 경우가 있어서, 순서를 반대로 하면 사람이 실제로 입력한 숫자는
    # 보지도 않고 인용된 "구매" 문구에 먼저 걸려 오판정하는 버그가 있었음
    # (실사용에서 확인됨: "1"이라고 답했는데 new_purchase로 파싱됨).
    #
    # 숫자도 "처음 나오는 것"이 아니라 "마지막에 나오는 것"을 씀 - 답장이
    # 원본을 인용하면 보통 인용문이 위에 붙고 사람이 새로 쓴 내용이 그
    # 아래(끝)에 오는 구조라서, 인용문 속 숫자(재고 수량 등)보다 실제 답이
    # 뒤에 있을 가능성이 높음.
    matches = re.findall(r"\d+", plain)
    if matches:
        idx = int(matches[-1]) - 1
        if 0 <= idx < len(flattened_candidates):
            return {"item_code": flattened_candidates[idx]["item_code"]}

    if "구매" in plain or "구입" in plain:
        return {"decision": "new_purchase"}

    return None


def process_mr(mr_name: str) -> None:
    app = get_process_app()
    snapshot = app.get_state(_config(mr_name))
    values = snapshot.values or {}
    substitute_results = values.get("substitute_results", {})
    flattened = flatten_substitute_candidates(substitute_results)

    comments = _get_comments(mr_name)
    reply = _find_reply_after_anchor(comments)
    if not reply:
        print(f"  [{mr_name}] 아직 답장 없음, 대기 계속")
        return

    parsed = _parse_reply(reply.get("content"), flattened)
    if parsed is None:
        print(f"  [{mr_name}] 답장을 인식하지 못함: {reply.get('content')!r} -> 재안내 댓글 등록")
        try:
            erp_add_comment(
                "Material Request", mr_name,
                f"{ANCHOR_PREFIX} 답장을 이해하지 못했습니다. 후보 번호(숫자) 또는 "
                f"'구매'라고만 다시 남겨주세요.",
            )
        except ERPNextAPIError as e:
            print(f"    재안내 댓글 등록 실패: {e}")
        return

    print(f"  [{mr_name}] 답장 파싱 결과: {parsed} -> resume 호출")
    app.invoke(Command(resume=parsed), config=_config(mr_name))

    # resume 후에도 여전히 대기중이면(그래프 자체 검증 실패) 그 사유를 안내
    new_values = (app.get_state(_config(mr_name)).values) or {}
    if new_values.get("status") == "awaiting_substitute_selection" and new_values.get("error"):
        try:
            erp_add_comment("Material Request", mr_name, f"{ANCHOR_PREFIX} {new_values['error']}")
        except ERPNextAPIError as e:
            print(f"    재안내 댓글 등록 실패: {e}")


def run_once() -> None:
    mr_names = get_mrs_awaiting_substitute_selection()
    print(f"[substitute_reply_watcher] 대기중인 MR {len(mr_names)}건: {mr_names}")
    for mr_name in mr_names:
        process_mr(mr_name)


if __name__ == "__main__":
    run_once()
