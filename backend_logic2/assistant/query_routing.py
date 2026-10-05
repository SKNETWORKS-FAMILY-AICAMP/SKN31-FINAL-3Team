"""Read-only query vocabulary, separate from workflow transitions and policy.

Waiting labels describe stages, not item search text. Keep deterministic routing
for these common questions even when the model is unavailable or misclassifies.
"""
import re

WAITING_STAGES = {
    "external": {"SUBSTITUTE_DECISION", "QUOTATION_COLLECTION", "PR_RESPONSE_WAITING", "DELIVERY"},
    "requester": {"SUBSTITUTE_DECISION"},
    "quotation": {"QUOTATION_COLLECTION"},
    "supplier_confirmation": {"PR_RESPONSE_WAITING"},
    "delivery": {"DELIVERY"},
    "po_approval": {"PRE_PO_APPROVAL"},
}


def waiting_group(message: str) -> str | None:
    text = re.sub(r"\s+", "", message).casefold()
    if not any(word in text for word in ("대기", "기다", "미회신", "응답전", "회신전")):
        return None
    if "외부" in text:
        return "external"
    if any(word in text for word in ("대체품", "요청자", "요청부서")):
        return "requester"
    if any(word in text for word in ("po승인", "발주승인", "결재", "최종승인")):
        return "po_approval"
    if any(word in text for word in ("수주", "pr응답", "pr회신", "pr대기", "공급사응답", "공급사승인", "발주확인")):
        return "supplier_confirmation"
    if any(word in text for word in ("견적", "rfq", "미회신")):
        return "quotation"
    if any(word in text for word in ("입고", "물품도착", "배송", "납품")):
        return "delivery"
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


def matches_waiting(row: dict, group: str) -> bool:
    stage = str(row.get("stage") or "").upper()
    status = str(row.get("status") or "").upper()
    # Delivery can remain RUNNING while waiting for an ERP receipt.
    return stage in WAITING_STAGES[group] and (
        status == "WAITING_INPUT" or (stage == "DELIVERY" and status in {"RUNNING", "PENDING"})
    )
