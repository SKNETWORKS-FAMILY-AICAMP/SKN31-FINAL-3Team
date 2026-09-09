from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    HEADERS,
    SITE_URL,
    erp_get_one,
    erp_get_rfq_received_communications,
)


BUSINESS_REGISTRATION_KEYWORDS = (
    "사업자등록증",
    "사업자 등록증",
    "business registration",
)

BANKBOOK_KEYWORDS = (
    "통장사본",
    "통장 사본",
    "bankbook",
    "bank account",
)

ALLOWED_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
}


def _matches(
    filename: str,
    keywords: tuple[str, ...],
) -> bool:
    normalized = (
        filename.casefold()
        .replace("_", " ")
        .replace("-", " ")
    )
    return any(
        keyword.casefold() in normalized
        for keyword in keywords
    )


def is_existing_registered_supplier(
    supplier: dict[str, Any] | None,
) -> bool:
    if not supplier:
        return False

    if not supplier.get("custom_is_new_supplier"):
        return True

    return (
        supplier.get("custom_onboarding_status")
        == "APPROVED"
    )


def inspect_supplier_documents(
    rfq_name: str,
    supplier_id: str,
) -> dict[str, Any]:
    supplier = erp_get_one("Supplier", supplier_id)
    if not supplier:
        raise LookupError(
            f"Supplier를 찾을 수 없습니다: {supplier_id}"
        )

    if is_existing_registered_supplier(supplier):
        return {
            "supplier": supplier_id,
            "is_new_supplier": False,
            "review_required": False,
            "complete": True,
            "documents": [],
            "missing_documents": [],
        }

    supplier_email = str(
        supplier.get("email_id") or ""
    ).strip().casefold()

    communications = erp_get_rfq_received_communications(
        rfq_name
    )

    documents = []

    for communication in communications:
        sender = str(
            communication.get("sender") or ""
        ).casefold()

        # 다른 공급사의 첨부파일이 섞이는 것을 방지한다.
        if supplier_email and supplier_email not in sender:
            continue

        for attachment in (
            communication.get("attachments") or []
        ):
            filename = str(
                attachment.get("file_name") or ""
            )
            suffix = Path(filename).suffix.casefold()

            documents.append({
                "file_id": attachment.get("name"),
                "file_name": filename,
                "file_url": attachment.get("file_url"),
                "allowed_type": suffix in ALLOWED_SUFFIXES,
                "communication": communication.get("name"),
            })

    business_registration_found = any(
        document["allowed_type"]
        and _matches(
            document["file_name"],
            BUSINESS_REGISTRATION_KEYWORDS,
        )
        for document in documents
    )

    bankbook_found = any(
        document["allowed_type"]
        and _matches(
            document["file_name"],
            BANKBOOK_KEYWORDS,
        )
        for document in documents
    )

    missing_documents = []

    if not business_registration_found:
        missing_documents.append(
            "business_registration"
        )

    if not bankbook_found:
        missing_documents.append("bankbook")

    return {
        "supplier": supplier_id,
        "is_new_supplier": True,
        "review_required": True,
        "complete": not missing_documents,
        "business_registration_found":
            business_registration_found,
        "bankbook_found": bankbook_found,
        "documents": documents,
        "missing_documents": missing_documents,
    }


def approve_supplier_onboarding(
    supplier_id: str,
    *,
    documents_complete: bool,
    business_registration_verified: bool,
    bankbook_verified: bool,
    note: str = "",
) -> dict:
    if not documents_complete:
        raise ValueError("필수 제출서류가 모두 첨부된 후 승인할 수 있습니다.")

    if not business_registration_verified:
        raise ValueError(
            "사업자등록증 확인이 필요합니다."
        )

    if not bankbook_verified:
        raise ValueError(
            "통장사본 확인이 필요합니다."
        )

    response = requests.put(
        (
            f"{SITE_URL}/api/resource/Supplier/"
            f"{quote(supplier_id, safe='')}"
        ),
        headers=HEADERS,
        json={
            "custom_is_new_supplier": 0,
            "custom_onboarding_status": "APPROVED",
            "custom_business_registration_verified": 1,
            "custom_bankbook_verified": 1,
            "custom_onboarding_verified_at": (
                datetime.now(timezone.utc).isoformat()
            ),
            "custom_onboarding_note": note,
        },
    )

    if response.status_code != 200:
        raise ERPNextAPIError(
            "Supplier 정식 등록 실패: "
            f"{response.status_code} - "
            f"{response.text[:500]}"
        )

    return response.json().get("data") or {}
