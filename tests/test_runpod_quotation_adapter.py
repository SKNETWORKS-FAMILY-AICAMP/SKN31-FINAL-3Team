from __future__ import annotations

import base64
from dataclasses import dataclass

import pytest

from backend_logic2.integrations.quotation_extraction.factory import (
    get_configured_quotation_parser,
    reset_parser_cache,
)
from backend_logic2.integrations.quotation_extraction.runpod import (
    RunPodQuotationConfig,
    RunPodQuotationParser,
    RunPodQuotationParserError,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
    PreparedSource,
    VisionInput,
    extract_quotation_bytes,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_models import SourceKind


@dataclass
class _Response:
    payload: dict
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status={self.status_code}")

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self, post_payload: dict, get_payloads: list[dict] | None = None):
        self.post_payload = post_payload
        self.get_payloads = list(get_payloads or [])
        self.post_call = None
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return _Response(self.post_payload)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _Response(self.get_payloads.pop(0))


def _config(**overrides) -> RunPodQuotationConfig:
    values = {
        "endpoint_id": "endpoint-123",
        "api_key": "secret-test-key",
        "timeout_seconds": 2,
        "poll_interval_seconds": 0.001,
    }
    values.update(overrides)
    return RunPodQuotationConfig(**values)


def _extraction() -> dict:
    return {
        "quotation_id": "EST-001",
        "supplier_name": "untrusted model value",
        "business_registration_no": None,
        "quotation_date": "2026-09-15",
        "valid_until": None,
        "currency": "KRW",
        "subtotal": 1000,
        "tax_amount": 100,
        "total_amount": 1100,
        "items": [{
            "item_code": "ITEM-001",
            "item_name": "안전모",
            "description": None,
            "quantity": 1,
            "unit": "EA",
            "unit_price": 1000,
            "amount": 1000,
            "expected_delivery_date": None,
            "lead_time_days": None,
            "specifications": {},
            "raw_description": "안전모 1 EA",
        }],
        "notes": None,
    }


def _prepared() -> PreparedSource:
    document = VisionInput(data=b"image-bytes", filename="quotation.png")
    return PreparedSource(
        kind=SourceKind.IMAGE,
        text="[견적서 이미지]",
        vision_inputs=[document],
        document_inputs=[document],
    )


def test_submit_then_poll_returns_extraction(monkeypatch) -> None:
    session = _Session(
        {"id": "job-1", "status": "IN_QUEUE"},
        [
            {"id": "job-1", "status": "IN_PROGRESS"},
            {
                "id": "job-1",
                "status": "COMPLETED",
                "output": {"status": "success", "extraction": _extraction()},
            },
        ],
    )
    parser = RunPodQuotationParser(_config(), session=session)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = parser(_prepared(), "RFQ-1", "Trusted Supplier", [])

    assert result["quotation_id"] == "EST-001"
    assert len(session.get_calls) == 2
    url, request = session.post_call
    assert url.endswith("/endpoint-123/run")
    assert request["headers"]["Authorization"] == "Bearer secret-test-key"
    worker_input = request["json"]["input"]
    assert base64.b64decode(worker_input["documents"][0]["base64"]) == b"image-bytes"
    assert worker_input["documents"][0]["filename"] == "quotation.png"
    assert len(worker_input["prompt_sha256"]) == 64
    assert "secret-test-key" not in str(request["json"])


def test_failed_job_error_does_not_expose_api_key() -> None:
    parser = RunPodQuotationParser(
        _config(),
        session=_Session({"id": "job-1", "status": "FAILED"}),
    )

    with pytest.raises(RunPodQuotationParserError) as captured:
        parser(_prepared(), "RFQ-1", "Supplier", [])

    assert "FAILED" in str(captured.value)
    assert "secret-test-key" not in str(captured.value)


def test_runpod_rejects_non_image_document_before_network_call() -> None:
    session = _Session({"id": "should-not-run"})
    parser = RunPodQuotationParser(_config(), session=session)
    prepared = PreparedSource(kind=SourceKind.DOCX, text="quotation text")

    with pytest.raises(RunPodQuotationParserError, match="이미지 또는 PDF"):
        parser(prepared, "RFQ-1", "Supplier", [])

    assert session.post_call is None


def test_factory_selects_provider(monkeypatch) -> None:
    reset_parser_cache()
    local = object()
    monkeypatch.setenv("QUOTATION_EXTRACTOR_PROVIDER", "local")
    assert get_configured_quotation_parser(lambda: local) is local

    monkeypatch.setenv("QUOTATION_EXTRACTOR_PROVIDER", "invalid")
    with pytest.raises(ValueError, match="local, runpod"):
        get_configured_quotation_parser(lambda: local)


def test_env_requires_key_without_printing_it(monkeypatch) -> None:
    monkeypatch.setenv("RUNPOD_QUOTATION_ENDPOINT_ID", "endpoint-123")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with pytest.raises(ValueError, match="RUNPOD_API_KEY is required"):
        RunPodQuotationConfig.from_env()


def test_common_extractor_uses_configured_adapter_and_trusts_app_metadata(
    monkeypatch,
) -> None:
    class _Adapter:
        def __call__(self, *_args):
            return _extraction()

        @staticmethod
        def extraction_evidence():
            return ["remote adapter", "external transport"]

    monkeypatch.setattr(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_extractor.get_configured_parser",
        lambda: _Adapter(),
    )

    quotation = extract_quotation_bytes(
        b"image-bytes",
        "quotation.png",
        "RFQ-1",
        supplier_name="Trusted Supplier",
        supplier_id="SUP-1",
    )

    assert quotation.supplier_name == "Trusted Supplier"
    assert quotation.supplier_id == "SUP-1"
    assert "remote adapter" in quotation.extraction_evidence
