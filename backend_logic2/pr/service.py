"""PR issuance, supplier response, and accepted-order finalization."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from backend_logic2.integrations.erp_client import erp_get_one, erp_send_email
from . import repository
from .email_template import render_email


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _build_po_preview(
    *,
    mr_name: str,
    supplier_quotation: str | None,
    direct_purchase_items: dict[str, Any],
) -> dict[str, Any]:
    source = erp_get_one("Supplier Quotation", supplier_quotation) if supplier_quotation else None
    if source:
        return {
            "currency": source.get("currency") or "KRW",
            "total": source.get("grand_total") or source.get("rounded_total") or source.get("net_total"),
            "items": source.get("items") or [],
        }
    mr = erp_get_one("Material Request", mr_name) or {}
    items = []
    for row in mr.get("items") or []:
        item_code = str(row.get("item_code") or "")
        basis = direct_purchase_items.get(item_code) or {}
        qty = row.get("qty") or 0
        rate = basis.get("rate") or 0
        items.append({**row, "rate": rate, "amount": float(qty) * float(rate)})
    return {"currency": "KRW", "total": sum(float(row.get("amount") or 0) for row in items), "items": items}


def create_and_send_pr(
    *, case_id: str, mr_name: str, supplier_id: str, supplier_email: str,
    rfq_name: str | None = None, supplier_quotation: str | None = None,
    purchase_mode: str = "quotation", direct_purchase_items: dict[str, Any] | None = None,
    expires_in_hours: int = 72,
) -> dict[str, Any]:
    """Create a single-use PR and email it after internal approval."""
    if purchase_mode not in {"quotation", "direct"}:
        raise ValueError("purchase_mode는 quotation 또는 direct여야 합니다.")
    if purchase_mode == "quotation" and not rfq_name:
        raise ValueError("견적 구매 PR에는 rfq_name이 필요합니다.")
    if "@" not in supplier_email:
        raise ValueError("유효한 공급사 이메일이 필요합니다.")
    base_url = os.getenv("BIDDINGFLOW_PUBLIC_URL", "").strip()
    if not base_url.startswith(("https://", "http://")):
        raise RuntimeError("BIDDINGFLOW_PUBLIC_URL을 외부 접근 가능한 URL로 설정하세요.")
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, expires_in_hours))
    pr = repository.create_request({
        "case_id": case_id, "mr_name": mr_name, "rfq_name": rfq_name,
        "supplier_quotation": supplier_quotation, "supplier_id": supplier_id,
        "supplier_email": supplier_email, "token_hash": _token_hash(token),
        "expires_at": expires_at, "purchase_mode": purchase_mode,
        "direct_purchase_items": direct_purchase_items or {},
    })
    if not pr.pop("_created", False):
        # A graph retry must not generate another token or send another email.
        return pr
    preview = _build_po_preview(
        mr_name=mr_name,
        supplier_quotation=supplier_quotation,
        direct_purchase_items=direct_purchase_items or {},
    )
    html = render_email(pr, base_url, token, preview=preview)
    recipient_email = os.getenv("PR_EMAIL_OVERRIDE", "").strip() or supplier_email
    subject_prefix = "[TEST] " if recipient_email != supplier_email else ""
    try:
        erp_send_email(
            "Material Request", mr_name, [recipient_email],
            f"{subject_prefix}[PR:{pr['pr_id']}] 수주 가능 여부 확인", html,
        )
    except Exception as exc:
        repository.cancel_draft(str(pr["pr_id"]), error=str(exc))
        raise
    return repository.mark_sent(str(pr["pr_id"]))


def inspect_token(token: str) -> dict[str, Any] | None:
    return repository.get_by_token_hash(_token_hash(token))


def respond(token: str, decision: str, reason: str | None = None) -> dict[str, Any]:
    status = {"accept": "ACCEPTED", "reject": "REJECTED"}.get(decision)
    if not status:
        raise ValueError("accept 또는 reject만 허용됩니다.")
    cleaned_reason = (reason or "").strip()
    if status == "REJECTED" and len(cleaned_reason) < 2:
        raise ValueError("거절 사유를 2자 이상 입력해 주세요.")
    pr = repository.record_response(_token_hash(token), status, cleaned_reason or None)
    if not pr:
        raise RuntimeError("이미 응답했거나 만료된 PR입니다.")
    return pr
