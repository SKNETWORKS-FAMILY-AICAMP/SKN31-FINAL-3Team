"""Idempotently create or update the ERPNext webhooks used by BiddingFlow.

Examples:
    python scripts/configure_erpnext_webhooks.py --base-url https://api.example.com
    python scripts/configure_erpnext_webhooks.py --base-url https://api.example.com --apply
    python scripts/configure_erpnext_webhooks.py --disable --apply

The default is a dry run.  API credentials and the shared webhook secret are
read from the project ``.env`` through ``erp_client`` and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend_logic2.integrations.erp_client import (  # noqa: E402
    API_KEY,
    API_SECRET,
    HEADERS,
    SITE_URL,
    erp_get,
)


@dataclass(frozen=True)
class WebhookSpec:
    doctype: str
    event: str
    endpoint: str
    condition: str = ""

    @property
    def name(self) -> str:
        return f"BiddingFlow - {self.doctype} - {self.event}"[:140]


def _specs() -> list[WebhookSpec]:
    specs: list[WebhookSpec] = []

    def add(doctype: str, endpoint: str, *events: str, condition: str = "") -> None:
        specs.extend(WebhookSpec(doctype, event, endpoint, condition) for event in events)

    add(
        "Material Request",
        "material-request",
        "after_insert",
        "on_update",
        "on_submit",
        "on_cancel",
        "on_trash",
    )
    add(
        "File",
        "material-request-file",
        "after_insert",
        "on_update",
        "on_trash",
        condition='doc.attached_to_doctype == "Material Request"',
    )
    # after_insert는 최초 요청, on_update는 요청자가 누락 규격을 보완한 뒤
    # 다시 저장한 Item을 재검증한다. 백엔드가 disabled=0으로 바꾼 update는
    # item_service에서 활성 Item으로 식별해 즉시 건너뛴다.
    add("Item", "item", "after_insert", "on_update")
    add(
        "Supplier Quotation",
        "supplier-quotation",
        "after_insert",
        "on_update",
        "on_submit",
        "on_cancel",
        "on_trash",
    )
    for doctype, endpoint in (
        ("Purchase Order", "purchase-order"),
        ("Purchase Receipt", "purchase-receipt"),
        ("Purchase Invoice", "purchase-invoice"),
        ("Payment Entry", "payment-entry"),
    ):
        add(doctype, endpoint, "on_submit", "on_cancel", "on_update_after_submit")
    return specs


def _payload_template(spec: WebhookSpec) -> str:
    fields = [
        '    "doctype": {{ doc.doctype | tojson }}',
        '    "name": {{ doc.name | tojson }}',
        '    "creation": {{ doc.creation | tojson }}',
        '    "modified": {{ doc.modified | tojson }}',
        '    "docstatus": {{ doc.docstatus | int }}',
        '    "status": {{ doc.get("status", "") | tojson }}',
    ]
    if spec.doctype == "File":
        fields.extend(
            [
                '    "file_name": {{ doc.file_name | tojson }}',
                '    "file_url": {{ doc.file_url | tojson }}',
                '    "attached_to_doctype": {{ doc.attached_to_doctype | tojson }}',
                '    "attached_to_name": {{ doc.attached_to_name | tojson }}',
            ]
        )
    if spec.doctype == "Item":
        fields.extend(
            [
                '    "item_code": {{ doc.item_code | tojson }}',
                '    "disabled": {{ doc.disabled | int }}',
            ]
        )
    if spec.doctype == "Payment Entry":
        fields.append('    "posting_date": {{ doc.posting_date | tojson }}')
    return "{\n  \"event\": %s,\n  \"doc\": {\n%s\n  }\n}" % (
        json.dumps(spec.event, ensure_ascii=False),
        ",\n".join(fields),
    )


def _payload(spec: WebhookSpec, base_url: str, secret: str, *, enabled: bool) -> dict[str, Any]:
    return {
        "doctype": "Webhook",
        "name": spec.name,
        "webhook_doctype": spec.doctype,
        "webhook_docevent": spec.event,
        "enabled": 1 if enabled else 0,
        "condition": spec.condition,
        "request_url": f"{base_url}/api/webhooks/erpnext/{spec.endpoint}",
        "request_method": "POST",
        "request_structure": "JSON",
        "timeout": 30,
        "enable_security": 0,
        "webhook_headers": [
            {"doctype": "Webhook Header", "key": "Content-Type", "value": "application/json"},
            {
                "doctype": "Webhook Header",
                "key": "X-ERPNext-Webhook-Secret",
                "value": secret,
            },
        ],
        "webhook_json": _payload_template(spec),
    }


def _request(method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{SITE_URL.rstrip('/')}{path}",
        headers=HEADERS,
        json=payload,
        timeout=35,
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(
            f"ERPNext Webhook {method} 실패: {response.status_code} - {response.text[:500]}"
        )
    return response.json().get("data") or {}


def _fetch_document(path: str) -> dict[str, Any]:
    response = requests.get(
        f"{SITE_URL.rstrip('/')}{path}",
        headers=HEADERS,
        timeout=35,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"ERPNext Webhook GET 실패: {response.status_code} - {response.text[:500]}"
        )
    return response.json().get("data") or {}


def _write_backup(path: str, documents: list[dict[str, Any]]) -> None:
    """Persist restorable webhook documents without exposing their secret headers."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "captured_at": datetime.now(UTC).isoformat(),
            "documents": documents,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    print(f"ERPNext 웹훅 백업 완료: {target} ({len(documents)}개)")


def _managed_existing(rows: list[dict[str, Any]], spec: WebhookSpec) -> dict[str, Any] | None:
    for row in rows:
        if row.get("webhook_doctype") != spec.doctype or row.get("webhook_docevent") != spec.event:
            continue
        name = str(row.get("name") or "")
        request_url = str(row.get("request_url") or "")
        if (
            name.startswith("BiddingFlow - ")
            or name == "Backend Webhook"
            or "/api/webhooks/erpnext/" in request_url
        ):
            return row
    return None


def configure(
    *,
    base_url: str,
    apply: bool,
    disable: bool,
    backup_file: str = "",
) -> tuple[int, int]:
    secret = os.environ.get("ERPNEXT_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("ERPNEXT_WEBHOOK_SECRET이 설정되지 않았습니다.")
    if not API_KEY or not API_SECRET:
        raise RuntimeError("ERPNext API_KEY/API_SECRET이 설정되지 않았습니다.")

    normalized_base = base_url.strip().rstrip("/")
    if not disable and not normalized_base.startswith(("https://", "http://")):
        raise RuntimeError("공개 백엔드 주소는 http:// 또는 https://로 시작해야 합니다.")

    rows = erp_get(
        "Webhook",
        fields=[
            "name",
            "webhook_doctype",
            "webhook_docevent",
            "request_url",
            "enabled",
        ],
        limit=500,
    ) or []

    if apply and backup_file:
        existing_names = {
            str(existing["name"])
            for spec in _specs()
            if (existing := _managed_existing(rows, spec)) is not None
        }
        existing_documents = [
            _fetch_document(f"/api/resource/Webhook/{quote(name, safe='')}")
            for name in sorted(existing_names)
        ]
        _write_backup(backup_file, existing_documents)

    created = 0
    updated = 0
    for spec in _specs():
        existing = _managed_existing(rows, spec)
        if disable and existing is None:
            continue
        target_base = normalized_base or "https://disabled.invalid"
        payload = _payload(spec, target_base, secret, enabled=not disable)
        action = "UPDATE" if existing else "CREATE"
        print(
            f"[{action}{' DISABLED' if disable else ''}] "
            f"{spec.doctype} / {spec.event} -> {payload['request_url']}"
        )
        if not apply:
            continue
        if existing:
            # Document names are immutable through the resource PUT endpoint.
            payload["name"] = str(existing["name"])
            _request(
                "PUT",
                f"/api/resource/Webhook/{quote(str(existing['name']), safe='')}",
                payload,
            )
            updated += 1
        else:
            _request("POST", "/api/resource/Webhook", payload)
            created += 1

    if not apply:
        print("Dry run입니다. 실제 반영은 --apply를 추가하세요.")
    else:
        print(f"ERPNext 웹훅 반영 완료: 생성 {created}개, 갱신 {updated}개")
    return created, updated


def main() -> None:
    parser = argparse.ArgumentParser(description="BiddingFlow ERPNext 웹훅 구성")
    parser.add_argument(
        "--base-url",
        default=(
            os.environ.get("PUBLIC_WEBHOOK_BASE_URL", "").strip()
            or os.environ.get("PROCUREMENT_API_BASE_URL", "").strip()
        ),
        help="ERPNext에서 접근 가능한 FastAPI 공개 주소",
    )
    parser.add_argument("--apply", action="store_true", help="ERPNext에 실제 반영")
    parser.add_argument("--disable", action="store_true", help="관리 웹훅 전체 비활성화")
    parser.add_argument(
        "--backup-file",
        default="",
        help="--apply 전에 기존 관리 웹훅 문서를 저장할 JSON 파일",
    )
    args = parser.parse_args()
    configure(
        base_url=args.base_url,
        apply=args.apply,
        disable=args.disable,
        backup_file=args.backup_file,
    )


if __name__ == "__main__":
    main()
