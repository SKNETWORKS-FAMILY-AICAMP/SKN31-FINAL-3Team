"""RunPod Serverless adapter for quotation text/vision/hybrid extraction.

Only this module knows the RunPod HTTP contract.  The workflow supplies a
prepared document and receives the same quotation dictionary that the local
parser returns.  API keys are read from the process environment, sent only in
the HTTPS Authorization header, and never included in exception messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests


RUNPOD_API_BASE_URL = "https://api.runpod.ai/v2"
TERMINAL_FAILURE_STATUSES = {"CANCELLED", "FAILED", "TIMED_OUT"}


class RunPodQuotationParserError(RuntimeError):
    """A safe-to-log error raised by the RunPod transport/response adapter."""


def _positive_float(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_int(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class RunPodQuotationConfig:
    endpoint_id: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 600.0
    poll_interval_seconds: float = 2.0
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 60.0
    max_document_bytes: int = 6 * 1024 * 1024
    max_payload_bytes: int = 9 * 1024 * 1024
    max_text_chars: int = 60_000
    max_documents: int = 8
    max_new_tokens: int = 512
    status_max_attempts: int = 3
    prompt_version: str = "qwen35-quotation-json-v3"

    @classmethod
    def from_env(cls) -> "RunPodQuotationConfig":
        endpoint_id = os.getenv("RUNPOD_QUOTATION_ENDPOINT_ID", "").strip()
        api_key = os.getenv("RUNPOD_API_KEY", "").strip()
        if not endpoint_id:
            raise ValueError("RUNPOD_QUOTATION_ENDPOINT_ID is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", endpoint_id):
            raise ValueError("RUNPOD_QUOTATION_ENDPOINT_ID has an invalid format")
        if not api_key:
            raise ValueError("RUNPOD_API_KEY is required")
        return cls(
            endpoint_id=endpoint_id,
            api_key=api_key,
            timeout_seconds=_positive_float(
                "RUNPOD_QUOTATION_TIMEOUT_SECONDS", "600"
            ),
            poll_interval_seconds=_positive_float(
                "RUNPOD_QUOTATION_POLL_INTERVAL_SECONDS", "2"
            ),
            connect_timeout_seconds=_positive_float(
                "RUNPOD_QUOTATION_CONNECT_TIMEOUT_SECONDS", "10"
            ),
            request_timeout_seconds=_positive_float(
                "RUNPOD_QUOTATION_REQUEST_TIMEOUT_SECONDS", "60"
            ),
            max_document_bytes=_positive_int(
                "RUNPOD_QUOTATION_MAX_DOCUMENT_BYTES", str(6 * 1024 * 1024)
            ),
            max_payload_bytes=_positive_int(
                "RUNPOD_QUOTATION_MAX_PAYLOAD_BYTES", str(9 * 1024 * 1024)
            ),
            max_text_chars=_positive_int(
                "RUNPOD_QUOTATION_MAX_TEXT_CHARS", "60000"
            ),
            max_documents=_positive_int("RUNPOD_QUOTATION_MAX_DOCUMENTS", "8"),
            max_new_tokens=_positive_int(
                "RUNPOD_QUOTATION_MAX_NEW_TOKENS", "512"
            ),
            status_max_attempts=_positive_int(
                "RUNPOD_QUOTATION_STATUS_MAX_ATTEMPTS", "3"
            ),
            prompt_version=(
                os.getenv(
                    "RUNPOD_QUOTATION_PROMPT_VERSION",
                    "qwen35-quotation-json-v3",
                ).strip()
                or "qwen35-quotation-json-v3"
            ),
        )


class RunPodQuotationParser:
    """Submit one quotation document job and normalize its terminal result."""

    provider_label = "RunPod Serverless"
    uses_external_service = True

    def __init__(
        self,
        config: RunPodQuotationConfig,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self._session = session or requests.Session()

    @classmethod
    def from_env(cls) -> "RunPodQuotationParser":
        return cls(RunPodQuotationConfig.from_env())

    @property
    def _endpoint_url(self) -> str:
        return f"{RUNPOD_API_BASE_URL}/{self.config.endpoint_id}"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def extraction_evidence(self) -> list[str]:
        return [
            f"RunPod Serverless 견적 추출: {self.config.endpoint_id}",
            "견적 문서를 외부 GPU 서비스에 HTTPS로 전송",
        ]

    def _documents(self, prepared: Any) -> list[dict[str, str]]:
        inputs = list(getattr(prepared, "document_inputs", []) or [])
        if not inputs:
            # Backward compatibility for PreparedSource values created before
            # document_inputs was introduced (scanned PDFs and images).
            inputs = list(getattr(prepared, "vision_inputs", []) or [])
        if len(inputs) > self.config.max_documents:
            raise RunPodQuotationParserError(
                f"RunPod 견적 문서는 최대 {self.config.max_documents}개까지 처리합니다."
            )

        documents: list[dict[str, str]] = []
        for value in inputs:
            data = bytes(value.data)
            if not data:
                raise RunPodQuotationParserError("빈 견적 문서는 전송할 수 없습니다.")
            if len(data) > self.config.max_document_bytes:
                raise RunPodQuotationParserError(
                    "견적 문서가 RunPod 전송 허용 크기를 초과했습니다."
                )
            documents.append({
                "filename": str(value.filename),
                "base64": base64.b64encode(data).decode("ascii"),
            })
        return documents

    def _document_text(self, prepared: Any) -> str:
        text = str(getattr(prepared, "text", "") or "").strip()
        if text in {"[견적서 이미지]", "[스캔 PDF]"}:
            return ""
        if len(text) > self.config.max_text_chars:
            raise RunPodQuotationParserError(
                "견적 문서의 추출 텍스트가 RunPod 허용 길이를 초과했습니다."
            )
        return text

    @staticmethod
    def _prompt(
        prepared: Any,
        reflection_errors: list[str],
    ) -> tuple[str, str]:
        # The backend owns the prompt.  The worker only executes and validates
        # the supplied prompt/hash, which allows prompt revisions without a
        # worker image rebuild.
        from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
            FINETUNED_SYSTEM_PROMPT,
            FINETUNED_USER_PROMPT,
        )

        additions: list[str] = []
        specification_keys = list(
            getattr(prepared, "specification_keys", []) or []
        )
        if specification_keys:
            additions.append(
                "[RFQ 규격 키]\n"
                + ", ".join(str(value) for value in specification_keys)
                + "\n문서에 실제 값이 있는 키만 specifications에 기록하세요."
            )
        if reflection_errors:
            additions.append(
                "[이전 검토에서 확인된 오류]\n"
                + "\n".join(f"- {value}" for value in reflection_errors)
                + "\n위 오류를 입력 문서와 다시 대조해 교정하세요."
            )
        user_prompt = FINETUNED_USER_PROMPT
        if additions:
            user_prompt += "\n\n" + "\n\n".join(additions)
        return FINETUNED_SYSTEM_PROMPT, user_prompt

    @staticmethod
    def _request_id(
        documents: list[dict[str, str]],
        document_text: str,
        rfq_name: str,
        supplier_name: str | None,
        prompt_sha256: str,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(str(rfq_name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(supplier_name or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(prompt_sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(document_text.encode("utf-8"))
        for document in documents:
            digest.update(b"\0")
            digest.update(document["filename"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(document["base64"].encode("ascii"))
        return f"quotation-{digest.hexdigest()[:32]}"

    @staticmethod
    def _json_response(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise RunPodQuotationParserError(
                f"RunPod {operation} HTTP 요청에 실패했습니다."
            ) from exc
        except ValueError as exc:
            raise RunPodQuotationParserError(
                f"RunPod {operation} 응답이 JSON이 아닙니다."
            ) from exc
        if not isinstance(payload, dict):
            raise RunPodQuotationParserError(
                f"RunPod {operation} 응답 형식이 올바르지 않습니다."
            )
        return payload

    def _submit(
        self, worker_input: dict[str, Any], *, webhook: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"input": worker_input}
        if webhook:
            body["webhook"] = webhook
        payload_size = len(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if payload_size > self.config.max_payload_bytes:
            raise RunPodQuotationParserError(
                "RunPod 요청 payload가 전송 허용 크기를 초과했습니다."
            )
        try:
            response = self._session.post(
                f"{self._endpoint_url}/run",
                headers=self._headers,
                json=body,
                timeout=(
                    self.config.connect_timeout_seconds,
                    self.config.request_timeout_seconds,
                ),
            )
        except requests.RequestException as exc:
            # POST is intentionally not retried: retrying without a returned
            # job ID could create a duplicate GPU job.
            raise RunPodQuotationParserError(
                "RunPod 견적 작업 제출에 실패했습니다. 자동 재제출하지 않았습니다."
            ) from exc
        return self._json_response(response, "작업 제출")

    def _status(self, job_id: str) -> dict[str, Any]:
        try:
            response = self._session.get(
                f"{self._endpoint_url}/status/{job_id}",
                headers=self._headers,
                timeout=(
                    self.config.connect_timeout_seconds,
                    self.config.request_timeout_seconds,
                ),
            )
        except requests.RequestException as exc:
            raise RunPodQuotationParserError(
                "RunPod 견적 작업 상태 조회에 실패했습니다."
            ) from exc
        return self._json_response(response, "상태 조회")

    @staticmethod
    def _completed_output(payload: dict[str, Any]) -> dict[str, Any]:
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RunPodQuotationParserError("RunPod 완료 응답에 output이 없습니다.")
        if str(output.get("status") or "").lower() != "success":
            raise RunPodQuotationParserError("RunPod 견적 모델 실행이 실패했습니다.")
        extraction = output.get("extraction")
        if not isinstance(extraction, dict):
            raise RunPodQuotationParserError(
                "RunPod 완료 응답에 견적 extraction이 없습니다."
            )
        return extraction

    def _wait(self, initial: dict[str, Any]) -> dict[str, Any]:
        status = str(initial.get("status") or "").upper()
        if status == "COMPLETED":
            return self._completed_output(initial)
        if status in TERMINAL_FAILURE_STATUSES:
            raise RunPodQuotationParserError(
                f"RunPod 견적 작업이 {status} 상태로 종료되었습니다."
            )
        job_id = str(initial.get("id") or "").strip()
        if not job_id:
            raise RunPodQuotationParserError("RunPod 제출 응답에 작업 ID가 없습니다.")

        deadline = time.monotonic() + self.config.timeout_seconds
        consecutive_status_errors = 0
        while time.monotonic() < deadline:
            time.sleep(self.config.poll_interval_seconds)
            try:
                payload = self._status(job_id)
            except RunPodQuotationParserError:
                consecutive_status_errors += 1
                if consecutive_status_errors >= self.config.status_max_attempts:
                    raise
                continue
            consecutive_status_errors = 0
            status = str(payload.get("status") or "").upper()
            if status == "COMPLETED":
                return self._completed_output(payload)
            if status in TERMINAL_FAILURE_STATUSES:
                raise RunPodQuotationParserError(
                    f"RunPod 견적 작업이 {status} 상태로 종료되었습니다."
                )
        raise RunPodQuotationParserError(
            "RunPod 견적 작업이 제한 시간 안에 완료되지 않았습니다."
        )

    def build_worker_input(
        self,
        prepared: Any,
        rfq_name: str,
        supplier_name: str | None,
        reflection_errors: list[str],
    ) -> dict[str, Any]:
        documents = self._documents(prepared)
        document_text = self._document_text(prepared)
        if not documents and not document_text:
            raise RunPodQuotationParserError(
                "RunPod 견적 추출에 사용할 문서 이미지나 텍스트가 없습니다."
            )
        system_prompt, user_prompt = self._prompt(prepared, reflection_errors)
        prompt_sha256 = hashlib.sha256(
            f"{system_prompt}\n{user_prompt}".encode("utf-8")
        ).hexdigest()
        worker_input = {
            "request_id": self._request_id(
                documents,
                document_text,
                rfq_name,
                supplier_name,
                prompt_sha256,
            ),
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prompt_version": self.config.prompt_version,
            "prompt_sha256": prompt_sha256,
            "documents": documents,
            "document_text": document_text,
            "input_mode": (
                "hybrid" if documents and document_text
                else "vision" if documents
                else "text"
            ),
            "max_new_tokens": self.config.max_new_tokens,
        }
        return worker_input

    def submit_job(self, worker_input: dict[str, Any], webhook: str) -> dict[str, Any]:
        """Submit once; durable job orchestration owns completion and retries."""
        return self._submit(worker_input, webhook=webhook)

    def job_status(self, job_id: str) -> dict[str, Any]:
        """Authenticated result lookup, also used to verify untrusted callbacks."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", job_id):
            raise ValueError("invalid RunPod job ID")
        return self._status(job_id)

    def __call__(
        self, prepared: Any, rfq_name: str, supplier_name: str | None,
        reflection_errors: list[str],
    ) -> dict[str, Any]:
        # Retained for explicit synchronous CLI/tests and polling fallback.
        worker_input = self.build_worker_input(
            prepared, rfq_name, supplier_name, reflection_errors,
        )
        extraction = self._wait(self._submit(worker_input))
        from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
            _normalize_finetuned_quotation,
        )

        return _normalize_finetuned_quotation(extraction)
