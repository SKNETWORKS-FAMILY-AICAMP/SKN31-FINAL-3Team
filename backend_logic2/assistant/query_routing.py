"""Read-only query vocabulary, separate from workflow transitions and policy.

Waiting labels describe stages, not item search text. Keep deterministic routing
for these common questions even when the model is unavailable or misclassifies.
"""
import re
from datetime import datetime, timedelta, timezone

WAITING_STAGES = {
    "decision": {"MR_REVIEW", "RFQ_TARGET_SELECTION", "SUPPLIER_SELECTION", "ORDER_START", "PRE_PO_APPROVAL", "PR_REJECTED", "SCORECARD", "HUMAN_REVIEW", "PO_CREATION_FAILED"},
    "external": {"SUBSTITUTE_DECISION", "QUOTATION_COLLECTION", "PR_RESPONSE_WAITING", "DELIVERY"},
    "requester": {"SUBSTITUTE_DECISION"},
    "quotation": {"QUOTATION_COLLECTION"},
    "supplier_confirmation": {"PR_RESPONSE_WAITING"},
    "delivery": {"DELIVERY"},
    "po_approval": {"PRE_PO_APPROVAL"},
    "mr_review": {"MR_REVIEW"},
    "approval": {"MR_REVIEW", "PRE_PO_APPROVAL"},
}


def waiting_group(message: str) -> str | None:
    message = current_request_clause(message)
    formal = formal_approval_group(message)
    if formal:
        return formal
    if decision_question(message):
        return 'decision'
    text = re.sub(r"\s+", "", message).casefold()
    if not any(word in text for word in ("대기", "기다", "미회신", "응답전", "회신전")):
        return None
    if "외부" in text:
        return "external"
    if any(word in text for word in ("대체품", "요청자", "요청부서")):
        return "requester"
    if any(word in text for word in ("po승인", "발주승인", "결재", "최종승인")):
        return "po_approval"
    if any(word in text for word in ("mr승인", "요청승인", "mr검토", "요청검토")):
        return "mr_review"
    if any(word in text for word in ("수주", "pr응답", "pr회신", "pr대기", "공급사응답", "공급사승인", "발주확인")):
        return "supplier_confirmation"
    if any(word in text for word in ("견적", "rfq", "미회신")):
        return "quotation"
    if any(word in text for word in ("입고", "물품도착", "배송", "납품")):
        return "delivery"
    if "승인" in text:
        return "approval"
    return None


def item_keyword(keyword: str | None, message: str) -> str | None:
    """Keep an explicit product keyword but remove workflow-only vocabulary.

    This applies only after an explicit waiting group is recognized. It never
    broadens permissions, and does not change arbitrary free-text searches.
    """
    if not keyword:
        return None
    value = keyword.strip()
    # The model must not invent an item term absent from the user's question.
    if re.sub(r"\s+", "", value).casefold() not in re.sub(r"\s+", "", message).casefold():
        return None
    value = re.sub(r"(?:외부|진행|응답|승인|회신|수주|확인|대체품|요청부서|요청자|공급사|협력사|견적|발주|입고|배송|납품|물품\s*도착|최종|결재|구매\s*요청|대기\s*중인|대기\s*중|대기|작업|항목|목록|\b건\b|\b중인\b|\b중\b|\bMR\b|\bPO\b|\bPR\b|\bRFQ\b)", " ", value, flags=re.I)
    return " ".join(value.split()) or None


def auto_progress_blockers(row: dict) -> list[dict]:
    """Read the same stored verdict used by dashboardTasks, without rerunning it.

    A quote-stage case can be waiting for a buyer's decision rather than a
    supplier reply. Stage alone is not enough to classify that case as external.
    Failed cases retain their failure meaning, as in the dashboard.
    """
    if str(row.get("status") or "").upper() == "FAILED":
        return []
    value = row.get("workflow_snapshot")
    for key in ("values", "quotation_ranking_meta", "auto_progress"):
        value = value.get(key) if isinstance(value, dict) else None
    checks = value.get("checks") if isinstance(value, dict) else None
    return [check for check in checks if isinstance(check, dict) and check.get("status") == "blocked"] if isinstance(checks, list) else []


def matches_waiting(row: dict, group: str) -> bool:
    stage = str(row.get("stage") or "").upper()
    status = str(row.get("status") or "").upper()
    if group == "decision":
        return needs_buyer_action(row)
    if group == "external" and auto_progress_blockers(row):
        return False
    # Delivery can remain RUNNING while waiting for an ERP receipt.
    return stage in WAITING_STAGES[group] and (
        status == "WAITING_INPUT"
        or (stage == "MR_REVIEW" and status == "AWAITING_MR_REVIEW")
        or (stage == "DELIVERY" and status in {"RUNNING", "PENDING"})
    )


def asks_for_count(message: str) -> bool:
    return bool(re.search(r"몇\s*(?:개|건)|개수|갯수|건수|총\s*얼마|얼마나\s*(?:남|있)|세어\s*(?:줘|주|봐)|세\s*줘|집계", message))


def current_request_clause(message: str) -> str:
    """Ignore a rejected earlier clause when identifying the current actor/group.

    Keep the original message for item extraction and explicit condition removal.
    This is not authorization: the read adapter still checks the real actor.
    """
    return re.split(r"말고|아니라", message)[-1]


def formal_approval_group(message: str) -> str | None:
    compact = re.sub(r"\s+", "", current_request_clause(message)).casefold()
    if any(word in compact for word in ('승인', '결재', '검토')):
        if 'po' in compact or '발주승인' in compact or '발주결재' in compact:
            return 'po_approval'
        # '승인 대기 중인 MR' historically means cases awaiting either formal
        # approval. Only an explicit MR-approval phrase narrows it to MR review.
        if any(word in compact for word in ('mr승인', 'mr결재', 'mr검토', '요청승인', '요청검토')):
            return 'mr_review'
    return None


def personal_scope(message: str) -> str | None:
    compact = re.sub(r"\s+", "", current_request_clause(message))
    if any(word in compact for word in ("내담당", "제가담당", "내게배정", "나한테배정")):
        return "assigned"
    if any(word in compact for word in ("내가", "제가", "내승인", "내결재", "내할일", "나한테")):
        if any(word in compact for word in ('외부', '회신', '답장', '공급사응답')):
            return 'assigned'
        return "actionable"
    return None


def decision_question(message: str) -> bool:
    message = current_request_clause(message)
    if formal_approval_group(message):
        return False
    compact = re.sub(r"\s+", "", message)
    if any(word in compact for word in ('요청자', '요청부서', '대체품', '외부')):
        return False
    return any(word in compact for word in ("결정대기", "결정할작업", "선택대기", "내할일")) or (
        personal_scope(message) == "actionable" and any(word in compact for word in ("처리할", "해야할", "할일", "결정", "확인할", "선택할")))


def ambiguous_personal_approval(message: str) -> bool:
    compact = re.sub(r"\s+", "", message).casefold()
    if requests_mutation(message) or any(word in compact for word in ("po", "발주승인", "mr승인", "요청승인", "결재", "어떻게", "방법", "버튼", "어디")):
        return False
    return personal_scope(message) == "actionable" and "승인" in compact


def requests_mutation(message: str) -> bool:
    # '승인해야 하는 작업' is a query, not the imperative '승인해 줘'.
    return bool(re.search(r'(?:시작|승인|반려|발송|삭제|진행)\s*해\s*(?:줘|주세요|주실|줄래|줄\s*수|[.!?]*$)|보내\s*줘', message))


def needs_buyer_action(row: dict) -> bool:
    stage, status = str(row.get('stage') or '').upper(), str(row.get('status') or '').upper()
    if status in {'COMPLETED', 'CANCELLED', 'REJECTED'}:
        return False
    if auto_progress_blockers(row):
        return True
    if status == 'FAILED' or stage in {'HUMAN_REVIEW', 'PO_CREATION_FAILED', 'PR_REJECTED', 'SCORECARD'}:
        return True
    return stage in WAITING_STAGES['decision'] and status in {'WAITING_INPUT', 'AWAITING_MR_REVIEW'}


def explicit_item_prefix(message: str) -> str | None:
    """Conservative offline fallback for '<item> 구매 작업 ...' questions."""
    match = re.match(r"^(.+?)\s+(?:구매\s*(?:작업|요청|건)|관련\s*MR)", message, re.I)
    value = match.group(1).strip() if match else None
    if value and any(word in value for word in ('처음', '검색했던', '돌아가', '그중', '내 담당', '내가', '제가', '조건', '승인', '첨부', '완료', '진행', '대기', '납기', '마감')):
        return None
    return value


def business_today():
    # ERP schedule_date is a Korean business date, even on a UTC host.
    return datetime.now(timezone(timedelta(hours=9))).date()


def due_window(message: str) -> int | None:
    if '납기' not in message:
        return None
    days = re.search(r"(\d+)\s*일\s*(?:이내|안)", message)
    if days:
        return min(int(days.group(1)), 365)
    if re.search(r"납기(?:일|가|는)?\s*(?:가까운|임박)|(?:가까운|임박한)\s*납기", message):
        return 7
    return None


def due_item_keyword(keyword: str | None, message: str) -> str | None:
    value = item_keyword(keyword, message)
    if not value:
        return None
    value = re.sub(r"납기(?:일|가|는)?|가까운|임박한|임박|\d+\s*일\s*(?:이내|안)|항목", " ", value)
    return " ".join(value.split()) or None
