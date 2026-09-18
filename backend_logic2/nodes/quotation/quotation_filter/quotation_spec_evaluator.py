"""Semantic specification comparison using RunPod Qwen or GPT-5.6 Luna."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import requests

from .quotation_models import QuotationReview, RFQRequirements


LOGGER = logging.getLogger(__name__)
RUNPOD_API_BASE_URL = "https://api.runpod.ai/v2"
RUNPOD_TERMINAL_FAILURE_STATUSES = {"CANCELLED", "FAILED", "TIMED_OUT"}


class SpecItemAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quotation_item: str
    rfq_item: str
    # Accepted only for compatibility with assessments produced before the
    # score/reason-only contract. New prompts neither request nor display it.
    compliant: bool | None = Field(default=None, exclude=True)
    score: float = Field(ge=0, le=100)
    reason: str


class QuotationSpecAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quotation_id: str
    # Deprecated compatibility inputs. Qwen's active output contract remains
    # quotation_id + score + reason + items.
    compliant: bool | None = Field(default=None, exclude=True)
    confidence: float | None = Field(default=None, ge=0, le=1, exclude=True)
    score: float = Field(ge=0, le=100)
    reason: str
    items: list[SpecItemAssessment]


class QuotationSpecAssessmentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[QuotationSpecAssessment]


INSTRUCTIONS = """
당신은 산업 구매 견적의 기술 규격 적합성을 판정하는 심사자입니다.
RFQ의 specifications와 각 Supplier Quotation의 specifications 및 특약/비고(notes)만 비교하세요.
가격, 공급사 인지도, 납기, 과거 실적은 규격 점수에 절대 반영하지 마세요.

판정 원칙:
- 동의어, 약어, 단위 환산, 표기 순서 차이는 의미가 같으면 일치로 봅니다.
- specifications와 notes는 동일한 근거입니다. 한쪽에 없는 규격이 다른 쪽에 있으면 합쳐서 판단합니다.
- 수치 허용오차가 명시되면 그것을 적용하고, 명시되지 않으면 임의로 허용오차를 만들지 않습니다.
- 'N 이상'은 N보다 큰 값을 충족으로, 'N 이하'는 N보다 작은 값을 충족으로 봅니다.
- 견적의 온도·용량 등 지원 범위가 RFQ 요구 범위를 완전히 포함하면 충족으로 봅니다.
- 단위를 먼저 환산한 뒤 비교합니다. 1시간=60분, 1mm=1000μm, 섭씨=켈빈-273.15,
  1라디안=약 57.2958도, 12개월=1년입니다. 환산 결과가 같으면 정확히 충족한 것입니다.
- 입력의 unit_normalizations와 notes_unit_normalizations는 백엔드가 계산한 확정적인
  환산 결과입니다. 해당 배열에 있는 등식은 다시 계산하거나 부정하지 말고 비교에 사용합니다.
- M/L/XL, M·L·XL, M, L, XL처럼 구분자만 다른 표기는 같은 사이즈 구성입니다.
- 상위 규격이 요구 규격을 완전히 포함한다는 기술적 근거가 있을 때만 적합으로 봅니다.
- 이중 구조를 요구했을 때 동일 기능의 삼중 구조처럼 요구 기능을 포함하는 상위 구조는
  기술적으로 불리하다는 근거가 없는 한 감점하지 않습니다.
- 필수 규격이 확인되지 않거나 상충하면 reason에 해당 항목과 비교 값을 구체적으로 적습니다.
- 추측하지 말고 제공된 근거만 사용합니다.
- 먼저 RFQ 필수 규격을 항목별로 나누고 각 항목의 충족 여부를 판단한 뒤 점수를 계산합니다.
  RFQ에 별도 중요도가 없으면 각 필수 규격을 동일 비중으로 봅니다.
- score는 (충족한 필수 규격 수 / 전체 필수 규격 수) × 100을 기본으로 하되 부분 충족은
  해당 항목에 부분점수를 줄 수 있습니다. 일부 필수 규격이 불일치해도 나머지 충족 항목의
  점수를 반드시 보존하며, 완전히 다른 제품이거나 근거가 거의 없을 때만 0점을 줍니다.
- quotation에 품목이 하나뿐이면 quotation score와 item score는 반드시 같아야 합니다.
- reason은 프론트 화면에 그대로 표시할 수 있도록 충족한 핵심 규격과 감점한 규격을
  수치·단위와 함께 2~4문장으로 명확하게 설명합니다. reason의 감점 내용과 score가
  모순되어서는 안 됩니다.
- 각 quotation_id마다 정확히 하나의 assessment를 반환합니다.
""".strip()


RUNPOD_SCHEMA_PROMPT = """
아래 [견적 원문]에는 JSON 형식의 rfq와 quotations(원소 1개)가 들어 있습니다.
시스템의 판정 기준에 따라 규격 적합성을 평가하고, 다른 설명 없이 아래 스키마와
정확히 같은 JSON 객체 하나만 출력하세요. 모든 필드는 필수입니다. 마크다운 코드블록,
추가 설명, 주석을 절대 붙이지 마세요. quotation_id는 입력값을 그대로 복사하세요.

{
  "assessments": [
    {
      "quotation_id": "<입력 quotation_id>",
      "score": <0~100 숫자>,
      "reason": "<충족 근거와 감점 근거를 포함한 2~4문장>",
      "items": [
        {
          "quotation_item": "<견적 품목명>",
          "rfq_item": "<대응 RFQ 품목명>",
          "score": <0~100 숫자>,
          "reason": "<충족·누락·상충 규격과 감점 근거>"
        }
      ]
    }
  ]
}
""".strip()


RUNPOD_RETRY_SUFFIX = """

이전 출력은 필수 JSON 스키마 검증에 실패했습니다. 설명을 늘리지 말고 모든 필수 필드를
채운 유효한 JSON 객체 하나만 다시 출력하세요. assessments에는 입력 견적 한 건만 넣으세요.
""".rstrip()


def _int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _float_setting(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


_NUMBER = r"(?P<value>\d+(?:\.\d+)?)"
_UNIT_PATTERNS: tuple[
    tuple[re.Pattern[str], str, Decimal, Decimal, int], ...
] = (
    (
        re.compile(rf"{_NUMBER}\s*(?:시간|hours?|hrs?|h)(?![A-Za-z])", re.I),
        "분",
        Decimal("60"),
        Decimal("0"),
        4,
    ),
    (
        re.compile(
            rf"{_NUMBER}\s*(?:μm|µm|um|micromet(?:er|re)s?)(?![A-Za-z])",
            re.I,
        ),
        "mm",
        Decimal("0.001"),
        Decimal("0"),
        6,
    ),
    (
        re.compile(rf"{_NUMBER}\s*(?:K|켈빈)(?![A-Za-z])"),
        "℃",
        Decimal("1"),
        Decimal("-273.15"),
        4,
    ),
    (
        re.compile(rf"{_NUMBER}\s*(?:rad|radian|radians|라디안)(?![A-Za-z])", re.I),
        "도",
        Decimal("57.2957795131"),
        Decimal("0"),
        2,
    ),
    (
        re.compile(rf"{_NUMBER}\s*(?:개월|months?|mos?)(?![A-Za-z])", re.I),
        "년",
        Decimal("0.083333333333333333"),
        Decimal("0"),
        4,
    ),
)


def _format_decimal(value: Decimal, decimal_places: int) -> str:
    quantum = Decimal(1).scaleb(-decimal_places)
    rendered = format(value.quantize(quantum), "f")
    return rendered.rstrip("0").rstrip(".") or "0"


def _unit_normalizations(value: Any) -> list[str]:
    """Return deterministic conversions while preserving the supplier's text."""

    if not isinstance(value, str) or not value.strip():
        return []
    conversions: list[str] = []
    for pattern, target_unit, multiplier, offset, places in _UNIT_PATTERNS:
        for match in pattern.finditer(value):
            source = match.group(0).strip()
            converted = Decimal(match.group("value")) * multiplier + offset
            equation = (
                f"{source} = {_format_decimal(converted, places)}{target_unit}"
            )
            if equation not in conversions:
                conversions.append(equation)
    return conversions


def _specification_unit_normalizations(
    specifications: dict[str, Any],
) -> list[str]:
    conversions: list[str] = []
    for key, value in specifications.items():
        for equation in _unit_normalizations(value):
            conversions.append(f"{key}: {equation}")
    return conversions


def _quotation_payload(rfq: RFQRequirements, quotation: Any) -> dict[str, Any]:
    """Keep confidential model input to RFQ specs and quotation spec/terms fields."""

    return {
        "rfq": {
            "rfq_name": rfq.rfq_name,
            "items": [
                {
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "specifications": item.specifications,
                    "unit_normalizations": _specification_unit_normalizations(
                        item.specifications
                    ),
                    "numeric_tolerance_percent": item.numeric_tolerance_percent,
                }
                for item in rfq.items
            ],
        },
        "quotations": [
            {
                "quotation_id": quotation.quotation_id,
                "items": [
                    {
                        "item_code": item.item_code,
                        "item_name": item.item_name,
                        "specifications": item.specifications,
                        "unit_normalizations": _specification_unit_normalizations(
                            item.specifications
                        ),
                    }
                    for item in quotation.items
                ],
                # ERPNext의 특약/비고에는 규격 내용이 섞일 수 있으므로 함께 평가한다.
                "notes": quotation.notes,
                "notes_unit_normalizations": _unit_normalizations(quotation.notes),
            }
        ],
    }


def specification_evaluation_fingerprint(
    rfq: RFQRequirements,
    quotation: Any,
    evaluator: "QuotationSpecEvaluator",
) -> str:
    """Identify an assessment by every input that can change its meaning."""

    cache_identity = {
        "model": evaluator.model_name,
        "instructions": INSTRUCTIONS,
        "schema_prompt": RUNPOD_SCHEMA_PROMPT,
        "prompt_version": (
            os.getenv("RUNPOD_SPEC_PROMPT_VERSION", "qwen35-spec-eval-v1").strip()
            or "qwen35-spec-eval-v1"
        ),
        "pipeline_version": (
            os.getenv("RUNPOD_SPEC_PIPELINE_VERSION", "document-text-v1").strip()
            or "document-text-v1"
        ),
        # Increment this when endpoint weights or inference behavior changes
        # without changing the public model/prompt version strings.
        "cache_version": (
            os.getenv("RUNPOD_SPEC_CACHE_VERSION", "v1").strip() or "v1"
        ),
        "payload": _quotation_payload(rfq, quotation),
    }
    canonical = json.dumps(
        cache_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class QuotationSpecEvaluator(Protocol):
    model_name: str

    @property
    def available(self) -> bool: ...

    def evaluate(
        self,
        rfq: RFQRequirements,
        reviews: list[QuotationReview],
    ) -> dict[str, QuotationSpecAssessment] | None: ...


class RunPodSpecEvaluationError(RuntimeError):
    """Safe-to-log RunPod transport or output validation failure."""


class RunPodSpecSubmissionError(RunPodSpecEvaluationError):
    """An ambiguous POST failure that must not create a duplicate GPU job."""


class RunPodSpecRetryableOutputError(RunPodSpecEvaluationError):
    """A completed job whose model output can safely be regenerated once."""


@dataclass(frozen=True)
class RunPodSpecEvaluatorConfig:
    endpoint_id: str
    api_key: str = field(repr=False)
    parallel_jobs: int = 10
    timeout_seconds: float = 360.0
    poll_interval_seconds: float = 2.0
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 60.0
    max_new_tokens: int = 900
    retry_max_new_tokens: int = 1024
    max_attempts: int = 2
    prompt_version: str = "qwen35-spec-eval-v1"
    pipeline_version: str = "document-text-v1"

    @classmethod
    def from_env(cls) -> "RunPodSpecEvaluatorConfig":
        endpoint_id = (
            os.getenv("RUNPOD_SPEC_ENDPOINT_ID", "").strip()
            or os.getenv("RUNPOD_QUOTATION_ENDPOINT_ID", "").strip()
        )
        api_key = os.getenv("RUNPOD_API_KEY", "").strip()
        if not endpoint_id:
            raise ValueError(
                "RUNPOD_SPEC_ENDPOINT_ID 또는 RUNPOD_QUOTATION_ENDPOINT_ID가 필요합니다."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", endpoint_id):
            raise ValueError("RunPod endpoint ID 형식이 올바르지 않습니다.")
        if not api_key:
            raise ValueError("RUNPOD_API_KEY가 필요합니다.")
        max_new_tokens = _int_setting(
            "RUNPOD_SPEC_MAX_NEW_TOKENS", 900, 256, 4096
        )
        retry_tokens = _int_setting(
            "RUNPOD_SPEC_RETRY_MAX_NEW_TOKENS", 1024, 256, 4096
        )
        return cls(
            endpoint_id=endpoint_id,
            api_key=api_key,
            # Queue enough jobs up front for RunPod's autoscaler to observe the
            # batch. Actual GPU concurrency is capped by endpoint Max workers.
            parallel_jobs=_int_setting("RUNPOD_SPEC_PARALLEL_JOBS", 10, 1, 50),
            timeout_seconds=_float_setting(
                "RUNPOD_SPEC_TIMEOUT_SECONDS", 360.0, 30.0, 3600.0
            ),
            poll_interval_seconds=_float_setting(
                "RUNPOD_SPEC_POLL_INTERVAL_SECONDS", 2.0, 0.25, 30.0
            ),
            connect_timeout_seconds=_float_setting(
                "RUNPOD_SPEC_CONNECT_TIMEOUT_SECONDS", 10.0, 1.0, 60.0
            ),
            request_timeout_seconds=_float_setting(
                "RUNPOD_SPEC_REQUEST_TIMEOUT_SECONDS", 60.0, 5.0, 300.0
            ),
            max_new_tokens=max_new_tokens,
            retry_max_new_tokens=max(max_new_tokens, retry_tokens),
            max_attempts=_int_setting("RUNPOD_SPEC_MAX_ATTEMPTS", 2, 1, 3),
            prompt_version=(
                os.getenv("RUNPOD_SPEC_PROMPT_VERSION", "qwen35-spec-eval-v1").strip()
                or "qwen35-spec-eval-v1"
            ),
            pipeline_version=(
                os.getenv("RUNPOD_SPEC_PIPELINE_VERSION", "document-text-v1").strip()
                or "document-text-v1"
            ),
        )


class RunPodQwenQuotationSpecEvaluator:
    """Queue one job per quotation and let the endpoint cap GPU concurrency."""

    def __init__(
        self,
        config: RunPodSpecEvaluatorConfig | None = None,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self._session = session or requests.Session()
        self._metrics_lock = threading.Lock()
        self.last_run_metrics: list[dict[str, Any]] = []
        self.model_name = os.getenv(
            "RUNPOD_SPEC_MODEL_NAME", "qwen3.5-9b-4bit"
        ).strip() or "qwen3.5-9b-4bit"

    @property
    def available(self) -> bool:
        if self.config is not None:
            return True
        return bool(
            os.getenv("RUNPOD_API_KEY", "").strip()
            and (
                os.getenv("RUNPOD_SPEC_ENDPOINT_ID", "").strip()
                or os.getenv("RUNPOD_QUOTATION_ENDPOINT_ID", "").strip()
            )
        )

    def _get_config(self) -> RunPodSpecEvaluatorConfig:
        if self.config is None:
            self.config = RunPodSpecEvaluatorConfig.from_env()
        return self.config

    @property
    def _endpoint_url(self) -> str:
        return f"{RUNPOD_API_BASE_URL}/{self._get_config().endpoint_id}"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_config().api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _json_response(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise RunPodSpecEvaluationError(
                f"RunPod {operation} HTTP 요청에 실패했습니다."
            ) from exc
        except ValueError as exc:
            raise RunPodSpecEvaluationError(
                f"RunPod {operation} 응답이 JSON이 아닙니다."
            ) from exc
        if not isinstance(payload, dict):
            raise RunPodSpecEvaluationError(
                f"RunPod {operation} 응답 형식이 올바르지 않습니다."
            )
        return payload

    def _worker_input(
        self,
        rfq: RFQRequirements,
        quotation: Any,
        *,
        attempt: int,
    ) -> dict[str, Any]:
        config = self._get_config()
        user_prompt = RUNPOD_SCHEMA_PROMPT
        if attempt > 1:
            user_prompt += RUNPOD_RETRY_SUFFIX
        prompt_sha256 = hashlib.sha256(
            f"{INSTRUCTIONS}\n{user_prompt}".encode("utf-8")
        ).hexdigest()
        document_text = json.dumps(
            _quotation_payload(rfq, quotation),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        request_digest = hashlib.sha256(
            (
                f"{rfq.rfq_name}\0{quotation.quotation_id}\0{attempt}\0"
                f"{prompt_sha256}\0{document_text}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        return {
            "request_id": f"spec-eval-{request_digest}",
            "task": "spec_eval",
            "system_prompt": INSTRUCTIONS,
            "user_prompt": user_prompt,
            "prompt_version": config.prompt_version,
            "prompt_sha256": prompt_sha256,
            "pipeline_version": config.pipeline_version,
            "documents": [],
            "document_text": document_text,
            "requires_ocr": False,
            "input_mode": "text",
            "max_new_tokens": (
                config.retry_max_new_tokens if attempt > 1 else config.max_new_tokens
            ),
        }

    def _submit(self, worker_input: dict[str, Any]) -> dict[str, Any]:
        config = self._get_config()
        try:
            response = self._session.post(
                f"{self._endpoint_url}/run",
                headers=self._headers,
                json={"input": worker_input},
                timeout=(
                    config.connect_timeout_seconds,
                    config.request_timeout_seconds,
                ),
            )
        except requests.RequestException as exc:
            # An ambiguous POST failure is not retried because it can duplicate GPU work.
            raise RunPodSpecSubmissionError(
                "RunPod 규격 평가 제출에 실패했습니다. 자동 재제출하지 않았습니다."
            ) from exc
        return self._json_response(response, "작업 제출")

    def _status(self, job_id: str) -> dict[str, Any]:
        config = self._get_config()
        try:
            response = self._session.get(
                f"{self._endpoint_url}/status/{job_id}",
                headers=self._headers,
                timeout=(
                    config.connect_timeout_seconds,
                    config.request_timeout_seconds,
                ),
            )
        except requests.RequestException as exc:
            raise RunPodSpecEvaluationError(
                "RunPod 규격 평가 상태 조회에 실패했습니다."
            ) from exc
        return self._json_response(response, "상태 조회")

    def _wait(
        self, initial: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        config = self._get_config()
        payload = initial
        status = str(payload.get("status") or "").upper()
        job_id = str(payload.get("id") or "").strip()
        deadline = time.monotonic() + config.timeout_seconds
        consecutive_status_errors = 0
        while status != "COMPLETED":
            if status in RUNPOD_TERMINAL_FAILURE_STATUSES:
                raise RunPodSpecRetryableOutputError(
                    f"RunPod 규격 평가가 {status} 상태로 종료되었습니다."
                )
            if not job_id:
                raise RunPodSpecEvaluationError(
                    "RunPod 제출 응답에 작업 ID가 없습니다."
                )
            if time.monotonic() >= deadline:
                raise RunPodSpecEvaluationError(
                    "RunPod 규격 평가가 제한 시간 안에 완료되지 않았습니다."
                )
            time.sleep(config.poll_interval_seconds)
            try:
                payload = self._status(job_id)
            except RunPodSpecEvaluationError:
                # Keep polling the same job instead of creating duplicate GPU work.
                consecutive_status_errors += 1
                if consecutive_status_errors >= 3:
                    raise
                continue
            consecutive_status_errors = 0
            status = str(payload.get("status") or "").upper()
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RunPodSpecEvaluationError("RunPod 완료 응답에 output이 없습니다.")
        if str(output.get("status") or "").lower() != "success":
            raise RunPodSpecEvaluationError("RunPod Qwen 워커가 실패를 반환했습니다.")
        if output.get("task") != "spec_eval":
            raise RunPodSpecEvaluationError(
                "RunPod 워커가 spec_eval 작업을 지원하지 않습니다."
            )
        return output, payload

    @staticmethod
    def _validated_assessment(
        output: dict[str, Any], expected_id: str
    ) -> QuotationSpecAssessment:
        try:
            batch = QuotationSpecAssessmentBatch.model_validate(output.get("extraction"))
        except ValidationError as exc:
            fields = [".".join(str(part) for part in row["loc"]) for row in exc.errors()]
            raise RunPodSpecRetryableOutputError(
                "Qwen 출력 스키마가 올바르지 않습니다: " + ", ".join(fields[:8])
            ) from exc
        if len(batch.assessments) != 1:
            raise RunPodSpecRetryableOutputError(
                "Qwen 출력 assessments에는 견적 한 건만 있어야 합니다."
            )
        assessment = batch.assessments[0]
        if assessment.quotation_id != expected_id:
            raise RunPodSpecRetryableOutputError(
                "Qwen 출력의 quotation_id가 입력 견적과 일치하지 않습니다."
            )
        return assessment

    def _evaluate_one(
        self,
        rfq: RFQRequirements,
        quotation: Any,
    ) -> QuotationSpecAssessment:
        config = self._get_config()
        last_error: RunPodSpecEvaluationError | None = None
        for attempt in range(1, config.max_attempts + 1):
            started = time.perf_counter()
            try:
                output, terminal_payload = self._wait(
                    self._submit(self._worker_input(rfq, quotation, attempt=attempt))
                )
                assessment = self._validated_assessment(
                    output, quotation.quotation_id
                )
                worker_metrics = output.get("metrics")
                metric = {
                    "quotation_id": quotation.quotation_id,
                    "attempt": attempt,
                    "job_id": terminal_payload.get("id"),
                    "delay_time_ms": terminal_payload.get("delayTime"),
                    "execution_time_ms": terminal_payload.get("executionTime"),
                    "client_elapsed_seconds": round(
                        time.perf_counter() - started, 3
                    ),
                    "worker_metrics": (
                        worker_metrics if isinstance(worker_metrics, dict) else {}
                    ),
                }
                with self._metrics_lock:
                    self.last_run_metrics.append(metric)
                return assessment
            except RunPodSpecEvaluationError as exc:
                last_error = exc
                if (
                    not isinstance(exc, RunPodSpecRetryableOutputError)
                    or attempt >= config.max_attempts
                ):
                    break
                LOGGER.warning(
                    "Qwen 규격 평가 출력 검증 실패로 견적 %s만 재시도합니다 (%d/%d): %s",
                    quotation.quotation_id,
                    attempt,
                    config.max_attempts,
                    exc,
                )
        assert last_error is not None
        raise last_error

    def evaluate(
        self,
        rfq: RFQRequirements,
        reviews: list[QuotationReview],
    ) -> dict[str, QuotationSpecAssessment] | None:
        quotations = [review.quotation for review in reviews if review.quotation is not None]
        if not quotations:
            return {}
        if not self.available:
            LOGGER.warning("RunPod 자격 증명 또는 규격 평가 endpoint ID가 없습니다.")
            return None

        with self._metrics_lock:
            self.last_run_metrics = []
        results: dict[str, QuotationSpecAssessment] = {}
        failures: dict[str, str] = {}
        worker_count = min(self._get_config().parallel_jobs, len(quotations))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self._evaluate_one, rfq, quotation): quotation.quotation_id
                for quotation in quotations
            }
            for future in as_completed(futures):
                quotation_id = futures[future]
                try:
                    results[quotation_id] = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures[quotation_id] = str(exc)
                    LOGGER.exception("RunPod Qwen 견적 %s 규격 평가에 실패했습니다.", quotation_id)
        if failures:
            LOGGER.error("RunPod Qwen 규격 평가 실패 견적: %s", sorted(failures))
            return None
        return results


class LunaQuotationSpecEvaluator:
    """Compare quotation specifications with RFQ requirements using Luna."""

    def __init__(self, client: OpenAI | None = None):
        self.model_name = (
            os.getenv("QUOTATION_SPEC_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna"
        )
        effort = os.getenv("QUOTATION_SPEC_REASONING_EFFORT", "medium").strip().lower()
        self.reasoning_effort = effort if effort in {
            "none", "low", "medium", "high", "xhigh", "max"
        } else "medium"
        self.timeout = _int_setting("QUOTATION_SPEC_TIMEOUT_SECONDS", 60, 10, 180)
        self.max_output_tokens = _int_setting(
            "QUOTATION_SPEC_MAX_OUTPUT_TOKENS", 4000, 1000, 12000
        )
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None or bool(os.getenv("OPENAI_API_KEY", "").strip())

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(timeout=self.timeout)
        return self._client

    def evaluate(
        self,
        rfq: RFQRequirements,
        reviews: list[QuotationReview],
    ) -> dict[str, QuotationSpecAssessment] | None:
        """Return one semantic assessment per quotation, or ``None`` on failure."""

        quotations = [review.quotation for review in reviews if review.quotation is not None]
        if not quotations:
            return {}
        if not self.available:
            LOGGER.warning("OPENAI_API_KEY가 없어 Luna 규격 평가를 건너뜁니다.")
            return None

        individual_payloads = [
            _quotation_payload(rfq, quotation) for quotation in quotations
        ]
        payload: dict[str, Any] = {
            "rfq": individual_payloads[0]["rfq"],
            "quotations": [
                row["quotations"][0] for row in individual_payloads
            ],
        }
        try:
            response = self._get_client().responses.parse(
                model=self.model_name,
                instructions=INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False),
                reasoning={"effort": self.reasoning_effort},
                max_output_tokens=self.max_output_tokens,
                text_format=QuotationSpecAssessmentBatch,
                store=False,
                timeout=self.timeout,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("Luna가 구조화된 규격 평가를 반환하지 않았습니다.")
            result = {row.quotation_id: row for row in parsed.assessments}
            expected_ids = {quotation.quotation_id for quotation in quotations}
            if set(result) != expected_ids:
                missing = sorted(expected_ids - set(result))
                unexpected = sorted(set(result) - expected_ids)
                raise ValueError(
                    f"Luna 규격 평가의 견적 ID가 일치하지 않습니다: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            return result
        except Exception:
            LOGGER.exception("Luna 견적 규격 평가에 실패했습니다.")
            return None


def build_quotation_spec_evaluator() -> QuotationSpecEvaluator:
    """Build the configured evaluator without silently sending RunPod data to Luna."""

    provider = os.getenv("QUOTATION_SPEC_PROVIDER", "runpod").strip().lower()
    if provider in {"runpod", "qwen", "qwen35"}:
        return RunPodQwenQuotationSpecEvaluator()
    if provider in {"luna", "openai"}:
        return LunaQuotationSpecEvaluator()
    raise ValueError(
        "QUOTATION_SPEC_PROVIDER는 runpod 또는 luna여야 합니다."
    )
