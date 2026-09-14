"""HTML rendering for supplier PR messages and response pages."""

from __future__ import annotations

from html import escape
from datetime import datetime
from typing import Any
from urllib.parse import quote


def render_email(
    pr: dict[str, Any],
    response_base_url: str,
    token: str,
    *,
    preview: dict[str, Any] | None = None,
) -> str:
    base = response_base_url.rstrip("/")
    encoded = quote(token, safe="")
    accept_url = f"{base}/api/public/pr/respond/{encoded}?decision=accept"
    reject_url = f"{base}/api/public/pr/respond/{encoded}?decision=reject"
    preview = preview or {}
    currency = escape(str(preview.get("currency") or "KRW"))
    item_rows = "".join(
        "<tr>"
        f"<td style='padding:8px;border:1px solid #ddd'>{escape(str(row.get('item_code') or '-'))}</td>"
        f"<td style='padding:8px;border:1px solid #ddd'>{escape(str(row.get('item_name') or row.get('description') or '-'))}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;text-align:right'>{escape(str(row.get('qty') or 0))}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;text-align:right'>{escape(str(row.get('rate') or 0))}</td>"
        f"<td style='padding:8px;border:1px solid #ddd'>{escape(str(row.get('schedule_date') or row.get('delivery_date') or '-'))}</td>"
        "</tr>"
        for row in preview.get("items") or []
    )
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#172033">
      <h2>수주 가능 여부 확인</h2>
      <p><b>{escape(str(pr['supplier_id']))}</b> 담당자님, 아래 발주 건의 수주 가능 여부를 회신해 주세요.</p>
      <table style="border-collapse:collapse;width:100%;margin:20px 0">
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">PR 번호</th><td style="padding:8px;border:1px solid #ddd">{escape(str(pr['pr_id']))}</td></tr>
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">MR 번호</th><td style="padding:8px;border:1px solid #ddd">{escape(str(pr['mr_name']))}</td></tr>
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">RFQ 번호</th><td style="padding:8px;border:1px solid #ddd">{escape(str(pr.get('rfq_name') or '-'))}</td></tr>
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">응답 기한</th><td style="padding:8px;border:1px solid #ddd">{escape(str(pr['expires_at']))}</td></tr>
      </table>
      <h3>발주 예정 내용</h3>
      <table style="border-collapse:collapse;width:100%;margin:12px 0">
        <thead><tr><th style="padding:8px;border:1px solid #ddd">품목코드</th><th style="padding:8px;border:1px solid #ddd">품목</th><th style="padding:8px;border:1px solid #ddd">수량</th><th style="padding:8px;border:1px solid #ddd">단가</th><th style="padding:8px;border:1px solid #ddd">납기</th></tr></thead>
        <tbody>{item_rows}</tbody>
      </table>
      <p style="text-align:right"><b>예정 합계: {escape(str(preview.get('total') or 0))} {currency}</b></p>
      <p style="margin:28px 0">
        <a href="{accept_url}" style="background:#16794c;color:white;padding:12px 22px;text-decoration:none;border-radius:6px;margin-right:10px">수주 접수</a>
        <a href="{reject_url}" style="background:#b42318;color:white;padding:12px 22px;text-decoration:none;border-radius:6px">수주 거절</a>
      </p>
      <p style="font-size:12px;color:#667085">버튼을 누른 뒤 확인 화면에서 최종 제출됩니다. 링크를 다른 사람에게 전달하지 마세요.</p>
    </div>
    """


def render_reminder_email(
    pr: dict[str, Any], *, now: datetime
) -> tuple[str, str]:
    """Render a reminder without reproducing the intentionally unhashed token."""
    expires_at = pr.get("expires_at")
    expires_text = (
        expires_at.astimezone().strftime("%Y-%m-%d %H:%M")
        if isinstance(expires_at, datetime)
        else str(expires_at or "-")
    )
    reminder_number = int(pr.get("reminder_count") or 0) + 1
    supplier = escape(str(pr.get("supplier_id") or "공급사"))
    pr_id = escape(str(pr.get("pr_id") or "-"))
    mr_name = escape(str(pr.get("mr_name") or "-"))
    subject = f"[재안내][PR:{pr.get('pr_id')}] 수주 가능 여부를 확인해 주세요"
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#172033">
      <h2>수주 가능 여부 재안내</h2>
      <p><b>{supplier}</b> 담당자님, 아직 아래 발주 건의 응답이 확인되지 않았습니다.</p>
      <table style="border-collapse:collapse;width:100%;margin:20px 0">
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">PR 번호</th><td style="padding:8px;border:1px solid #ddd">{pr_id}</td></tr>
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">MR 번호</th><td style="padding:8px;border:1px solid #ddd">{mr_name}</td></tr>
        <tr><th style="text-align:left;padding:8px;border:1px solid #ddd">응답 기한</th><td style="padding:8px;border:1px solid #ddd">{escape(expires_text)}</td></tr>
      </table>
      <p>최초 PR 안내 메일의 <b>수주 접수</b> 또는 <b>수주 거절</b> 버튼을 이용해 기한 내 회신해 주세요.</p>
      <p style="font-size:12px;color:#667085">본 메일은 {reminder_number}회차 자동 재안내입니다.</p>
    </div>
    """
    return subject, body


def render_response_form(token: str, decision: str, supplier: str, pr_id: str) -> str:
    rejecting = decision == "reject"
    title = "수주 거절" if rejecting else "수주 접수"
    reason = (
        '<label for="reason">거절 사유</label><textarea id="reason" name="reason" '
        'required minlength="2" maxlength="2000" rows="6"></textarea>'
        if rejecting else ""
    )
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>{title}</title><style>body{{font-family:Arial,sans-serif;background:#f5f7fa;padding:30px}}main{{max-width:560px;margin:auto;background:white;padding:28px;border-radius:10px}}label{{display:block;margin:18px 0 8px;font-weight:bold}}textarea{{box-sizing:border-box;width:100%;padding:10px}}button{{margin-top:20px;padding:12px 20px;border:0;border-radius:6px;background:{'#b42318' if rejecting else '#16794c'};color:white;font-weight:bold}}</style></head>
    <body><main><h2>{title} 확인</h2><p>PR <b>{escape(pr_id)}</b> · {escape(supplier)}</p><p>아래 버튼을 눌러야 응답이 최종 반영됩니다.</p>
    <form method="post" action="/api/public/pr/respond/{quote(token, safe='')}"><input type="hidden" name="decision" value="{decision}">{reason}<button type="submit">{title} 확정</button></form></main></body></html>"""


def render_result(title: str, message: str) -> str:
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{escape(title)}</title></head><body style="font-family:Arial,sans-serif;background:#f5f7fa;padding:30px"><main style="max-width:560px;margin:auto;background:white;padding:28px;border-radius:10px"><h2>{escape(title)}</h2><p>{escape(message)}</p></main></body></html>"""
