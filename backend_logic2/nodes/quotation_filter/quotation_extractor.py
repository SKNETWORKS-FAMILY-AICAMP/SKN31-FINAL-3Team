"""외부 견적을 로컬 Hugging Face 모델로 공통 Quotation JSON에 추출한다.

보안 원칙:
    - 견적 원문과 이미지를 외부 API로 전송하지 않는다.
    - Hugging Face 모델은 ``local_files_only=True``로만 로드한다.
    - 런타임 모델 다운로드와 원격 코드는 허용하지 않는다.

단독 실행 예:
    python -m backend_logic2.nodes.quotation_filter.quotation_extractor `
      "C:/Users/Playdata/Desktop/1.png" `
      --rfq PUR-RFQ-2026-00295 `
      --supplier-name "화진에스텍" `
      --output "./extracted.json"
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

try:
    from .quotation_models import (
        Quotation,
        QuotationItem,
        QuotationSource,
        SourceKind,
        dump_json,
        load_json,
    )
except ImportError:  # nodes 폴더에서 직접 실행할 때
    from quotation_models import (
        Quotation,
        QuotationItem,
        QuotationSource,
        SourceKind,
        dump_json,
        load_json,
    )


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".csv"}
PDF_SUFFIXES = {".pdf"}
EMAIL_SUFFIXES = {".eml"}
TEXT_SUFFIXES = {".txt", ".md"}

DEFAULT_TEXT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_VISION_MODEL = "Qwen/Qwen2-VL-2B-Instruct"
PROJECT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _project_model_setting(name: str) -> str | None:
    """Read only quotation model settings from the repository .env file.

    The project value intentionally takes precedence over a stale PowerShell
    process variable. Explicit constructor/CLI arguments still take precedence
    over both.
    """
    value = dotenv_values(PROJECT_ENV_FILE).get(name)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


class _ParsedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_code: str | None = None
    item_name: str
    description: str | None = None
    quantity: Decimal = Field(gt=0)
    unit: str | None = None
    unit_price: Decimal = Field(ge=0)
    amount: Decimal = Field(ge=0)
    delivery_date: date | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    specifications: dict[str, str | int | float] = Field(default_factory=dict)
    raw_description: str | None = None


class _ParsedQuotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # RFQ/supplier are application metadata, not values the model may decide.
    # quotation_id may be absent from a quotation document.
    quotation_id: str | None = None
    supplier_name: str | None = None
    business_registration_no: str | None = None
    quotation_date: date | None = None
    valid_until: date | None = None
    currency: str = "KRW"
    subtotal: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    items: list[_ParsedItem] = Field(min_length=1)
    notes: str | None = None


@dataclass
class VisionInput:
    data: bytes
    filename: str


@dataclass
class PreparedSource:
    kind: SourceKind
    text: str
    vision_inputs: list[VisionInput] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    specification_keys: list[str] = field(default_factory=list)


QuotationParser = Callable[
    [PreparedSource, str, str | None, list[str]],
    _ParsedQuotation | dict[str, Any],
]


def classify_source(path: str | Path) -> SourceKind:
    suffix = Path(path).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return SourceKind.EXCEL
    if suffix in PDF_SUFFIXES:
        return SourceKind.PDF
    if suffix in IMAGE_SUFFIXES:
        return SourceKind.IMAGE
    if suffix in EMAIL_SUFFIXES:
        return SourceKind.EMAIL
    if suffix in TEXT_SUFFIXES:
        return SourceKind.TEXT
    raise ValueError(f"지원하지 않는 견적 형식입니다: {suffix or '(확장자 없음)'}")


def _spreadsheet_to_text(data: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        decoded = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(decoded)))
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)

    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - 설치 환경 오류
        raise RuntimeError("Excel 추출에는 pandas와 openpyxl이 필요합니다.") from exc

    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
    rendered: list[str] = []
    for sheet_name, frame in sheets.items():
        frame = frame.fillna("")
        rendered.append(f"[sheet: {sheet_name}]\n{frame.to_csv(index=False)}")
    return "\n\n".join(rendered)


def _render_scanned_pdf(data: bytes, filename: str) -> list[VisionInput]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - 설치 환경 오류
        raise RuntimeError("스캔 PDF의 로컬 비전 처리에는 PyMuPDF가 필요합니다.") from exc

    document = fitz.open(stream=data, filetype="pdf")
    images: list[VisionInput] = []
    try:
        for index, page in enumerate(document, 1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            images.append(VisionInput(
                data=pixmap.tobytes("png"),
                filename=f"{Path(filename).stem}-page-{index}.png",
            ))
    finally:
        document.close()
    return images


def _pdf_to_source(data: bytes, filename: str) -> tuple[str, list[VisionInput], list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 설치 환경 오류
        raise RuntimeError("PDF 추출에는 pypdf가 필요합니다.") from exc

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(f"[page {idx}]\n{page}" for idx, page in enumerate(pages, 1) if page)
    evidence = [f"PDF {len(reader.pages)}페이지에서 텍스트 {len(text)}자 로컬 추출"]
    if text.strip():
        return text, [], evidence

    evidence.append("디지털 텍스트가 없어 페이지를 이미지로 변환해 로컬 비전 모델 사용")
    return "[스캔 PDF]", _render_scanned_pdf(data, filename), evidence


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"[ \t]+", " ", html.unescape(value)).strip()


def _email_to_source(data: bytes, filename: str) -> PreparedSource:
    message = BytesParser(policy=policy.default).parsebytes(data)
    headers = (
        f"From: {message.get('From', '')}\n"
        f"To: {message.get('To', '')}\n"
        f"Subject: {message.get('Subject', '')}"
    )
    body_parts: list[str] = []
    vision_inputs: list[VisionInput] = []
    evidence = [f"이메일 파일 파싱: {filename}"]

    body = message.get_body(preferencelist=("plain", "html"))
    if body:
        content = body.get_content()
        body_parts.append(_strip_html(content) if body.get_content_type() == "text/html" else str(content))

    for part in message.iter_attachments():
        attachment_name = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True) or b""
        suffix = Path(attachment_name).suffix.lower()
        evidence.append(f"이메일 첨부 로컬 처리: {attachment_name}")
        if suffix in EXCEL_SUFFIXES:
            body_parts.append(f"[attachment: {attachment_name}]\n{_spreadsheet_to_text(payload, attachment_name)}")
        elif suffix in PDF_SUFFIXES:
            pdf_text, pdf_images, pdf_evidence = _pdf_to_source(payload, attachment_name)
            body_parts.append(f"[attachment: {attachment_name}]\n{pdf_text}")
            vision_inputs.extend(pdf_images)
            evidence.extend(pdf_evidence)
        elif suffix in IMAGE_SUFFIXES:
            vision_inputs.append(VisionInput(data=payload, filename=attachment_name))
        elif part.get_content_maintype() == "text":
            body_parts.append(f"[attachment: {attachment_name}]\n{part.get_content()}")

    return PreparedSource(
        kind=SourceKind.EMAIL,
        text=f"{headers}\n\n[Body]\n" + "\n\n".join(body_parts),
        vision_inputs=vision_inputs,
        evidence=evidence,
    )


def prepare_source(path: str | Path) -> PreparedSource:
    path = Path(path)
    kind = classify_source(path)
    data = path.read_bytes()

    if kind == SourceKind.EXCEL:
        return PreparedSource(kind=kind, text=_spreadsheet_to_text(data, path.name), evidence=[f"표 파일 로컬 파싱: {path.name}"])
    if kind == SourceKind.PDF:
        text, vision_inputs, evidence = _pdf_to_source(data, path.name)
        return PreparedSource(kind=kind, text=text, vision_inputs=vision_inputs, evidence=evidence)
    if kind == SourceKind.IMAGE:
        return PreparedSource(
            kind=kind,
            text="[견적서 이미지]",
            vision_inputs=[VisionInput(data=data, filename=path.name)],
            evidence=[f"로컬 비전 입력 준비: {path.name}"],
        )
    if kind == SourceKind.EMAIL:
        return _email_to_source(data, path.name)
    return PreparedSource(kind=kind, text=data.decode("utf-8-sig", errors="replace"), evidence=[f"텍스트 파일 로컬 파싱: {path.name}"])


def _enforce_offline_mode() -> None:
    """transformers를 import하기 전에 네트워크 사용을 차단한다."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def _move_inputs_to_device(inputs: Any, device: str) -> dict[str, Any]:
    return {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in inputs.items()
    }


def _extract_json_object(generated_text: str) -> dict[str, Any]:
    """Select a quotation payload instead of an echoed schema/example."""
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", generated_text):
        try:
            value, _ = decoder.raw_decode(generated_text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)

    for value in reversed(candidates):
        if (
            isinstance(value.get("items"), list)
            and "total_amount" in value
            and "$defs" not in value
            and "properties" not in value
        ):
            return value
    if candidates:
        raise ValueError(
            "로컬 모델이 견적 데이터가 아닌 JSON Schema/예시를 출력했습니다. "
            f"응답 시작: {generated_text[:300]!r}"
        )
    raise ValueError(
        "로컬 모델 응답에서 JSON 객체를 찾지 못했습니다. "
        f"응답 시작: {generated_text[:300]!r}"
    )


def _document_item_codes(document_text: str) -> list[str]:
    """Join item codes split into PDF lines, e.g. ITEM-/SUB-/106."""
    lines = [line.strip() for line in document_text.splitlines()]
    codes: list[str] = []
    index = 0
    while index < len(lines):
        if not re.fullmatch(r"[A-Z][A-Z0-9]*-", lines[index]):
            index += 1
            continue
        parts = [lines[index]]
        cursor = index + 1
        while cursor < len(lines) and re.fullmatch(r"[A-Z0-9]+-", lines[cursor]):
            parts.append(lines[cursor])
            cursor += 1
        if cursor < len(lines) and re.fullmatch(r"[A-Z0-9]+", lines[cursor]):
            parts.append(lines[cursor])
            codes.append("".join(parts))
            index = cursor + 1
        else:
            index += 1
    return codes


def _erpnext_pdf_item_prices(document_text: str, item_count: int) -> list[tuple[Decimal, Decimal]]:
    """Read Rate/Amount pairs from the standard ERPNext quotation text layout."""
    header_words = ("discount", "distributed", "rate", "amount")
    if item_count < 1 or not all(re.search(word, document_text, re.IGNORECASE) for word in header_words):
        return []
    item_section = re.split(r"(?i)Total\s*Quantity\s*:?", document_text, maxsplit=1)[0]
    values = [
        Decimal(raw.replace(",", ""))
        for raw in re.findall(r"\b(?:KRW|USD|EUR|JPY|CNY)\s*([\d,]+(?:\.\d+)?)", item_section)
    ]
    # ERPNext emits discount amount, distributed discount, rate, amount per row.
    required = item_count * 4
    if len(values) < required:
        return []
    values = values[-required:]
    return [(values[index + 2], values[index + 3]) for index in range(0, required, 4)]


def _erpnext_document_totals(document_text: str) -> tuple[Decimal, Decimal, Decimal] | None:
    """Read subtotal, tax and grand total from an ERPNext transcription."""
    sections = re.split(r"(?i)Total\s*Quantity\s*:?", document_text, maxsplit=1)
    if len(sections) != 2:
        return None
    values = [
        Decimal(raw.replace(",", ""))
        for raw in re.findall(r"\b(?:KRW|USD|EUR|JPY|CNY)\s*([\d,]+(?:\.\d+)?)", sections[1])
    ]
    if len(values) < 3:
        return None
    return values[0], values[1], values[2]


def _normalize_decimal(value: Any) -> Any:
    """Convert model-formatted money/quantity such as ``KRW 90,000.00``."""
    if value is None or isinstance(value, (Decimal, int, float)):
        return value
    text = str(value).strip()
    if not text:
        return value
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"(?i)\b(?:KRW|USD|EUR|JPY|CNY)\b", "", text)
    cleaned = cleaned.replace(",", "").replace("₩", "").replace("$", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return value
    number = Decimal(match.group(0))
    return -number if negative else number


def _normalize_date(value: Any) -> Any:
    """Normalize common model date output to Pydantic's ISO date format."""
    if value is None or isinstance(value, date):
        return value
    text = str(value).strip()
    dmy = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})[-./](\d{4})", text)
    if dmy:
        day, month, year = map(int, dmy.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return value
    ymd = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", text)
    if ymd:
        year, month, day = map(int, ymd.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return value
    return value


def _normalize_generated_quotation(
    payload: dict[str, Any],
    document_text: str,
    supplier_name: str | None,
) -> dict[str, Any]:
    """모델의 형식 차이와 문서에서 생략된 합계 필드를 결정론적으로 보완한다."""
    quotation_match = re.search(r"\bPUR-SQTN-\d{4}-\d+\b", document_text)
    item_codes = _document_item_codes(document_text)
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    erpnext_prices = _erpnext_pdf_item_prices(document_text, len(raw_items))
    erpnext_totals = _erpnext_document_totals(document_text)
    has_delivery_field = bool(re.search(r"(?i)delivery|납기", document_text))
    color_match = re.search(r"색상\s*[:：]\s*([가-힣A-Za-z]+)", document_text)
    document_color = color_match.group(1) if color_match else None
    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            continue
        original_item_code = raw_item.get("item_code")
        item_code = original_item_code
        if index < len(item_codes):
            item_code = item_codes[index]
        quantity = _normalize_decimal(raw_item.get("quantity"))
        unit_price = _normalize_decimal(raw_item.get("unit_price", raw_item.get("rate")))
        amount = _normalize_decimal(raw_item.get("amount"))
        if index < len(erpnext_prices):
            unit_price, amount = erpnext_prices[index]
        if amount is None and quantity is not None and unit_price is not None:
            try:
                amount = Decimal(str(quantity).replace(",", "")) * Decimal(str(unit_price).replace(",", ""))
            except Exception:
                pass
        if amount is not None and quantity not in (None, 0):
            try:
                expected_rate = Decimal(str(amount)) / Decimal(str(quantity))
                if unit_price is None or Decimal(str(quantity)) * Decimal(str(unit_price)) != Decimal(str(amount)):
                    unit_price = expected_rate
            except Exception:
                pass
        description = raw_item.get("description")
        specifications = raw_item.get("specifications") if isinstance(raw_item.get("specifications"), dict) else {}
        if document_color:
            specifications = dict(specifications)
            specifications["color"] = document_color
            if description and re.search(r"색상\s*[:：]\s*[가-힣A-Za-z]+", description):
                description = re.sub(
                    r"색상\s*[:：]\s*[가-힣A-Za-z]+",
                    f"색상: {document_color}",
                    description,
                )
            elif description:
                description = f"{description} 색상: {document_color}"
            else:
                description = f"색상: {document_color}"
        item_name = raw_item.get("item_name")
        if not item_name or item_name == original_item_code:
            item_name = item_code or description or "품목명 미기재"
        unit = raw_item.get("unit")
        if str(unit or "").upper() in {"KRW", "USD", "EUR", "JPY", "CNY"}:
            unit = None
        raw_description = raw_item.get("raw_description")
        raw_description_text = str(raw_description or "").strip()
        if (
            raw_description_text.lower() in {"", "none", "null", "n/a"}
            or re.fullmatch(r"(?i)(?:KRW|USD|EUR|JPY|CNY)?\s*[\d,.]+", raw_description_text)
        ):
            raw_description = description
        items.append({
            "item_code": item_code,
            "item_name": item_name,
            "description": description,
            "quantity": quantity,
            "unit": unit,
            "unit_price": unit_price,
            "amount": amount,
            "delivery_date": _normalize_date(raw_item.get("delivery_date")) if has_delivery_field else None,
            "lead_time_days": raw_item.get("lead_time_days") if has_delivery_field else None,
            "specifications": specifications,
            "raw_description": raw_description,
        })

    subtotal = _normalize_decimal(payload.get("subtotal"))
    tax_amount = _normalize_decimal(payload.get("tax_amount"))
    total_amount = _normalize_decimal(payload.get("total_amount"))
    if erpnext_totals:
        subtotal, tax_amount, total_amount = erpnext_totals
    try:
        item_subtotal = sum(Decimal(str(item["amount"])) for item in items)
        if subtotal is None or Decimal(str(subtotal)) != item_subtotal:
            subtotal = item_subtotal
        subtotal_decimal = Decimal(str(subtotal))
        tax_was_missing = tax_amount is None

        if tax_was_missing:
            # 문서가 총액만 기재했다면 차액을 세액으로 복원한다. 세액과 총액을
            # 모두 생략한 견적은 면세/세액 미기재 견적으로 보고 0을 사용한다.
            if total_amount is not None and Decimal(str(total_amount)) >= subtotal_decimal:
                tax_amount = Decimal(str(total_amount)) - subtotal_decimal
            else:
                tax_amount = Decimal("0")

        calculated_total = subtotal_decimal + Decimal(str(tax_amount))
        if total_amount is None:
            total_amount = calculated_total
        elif not tax_was_missing and Decimal(str(total_amount)) != calculated_total:
            # 세액이 명시된 경우에는 품목합 + 세액을 신뢰해 총액을 보정한다.
            total_amount = calculated_total
    except Exception:
        pass
    generated_quotation_id = str(payload.get("quotation_id") or "").strip()
    document_backed_quotation_id = quotation_match.group(0) if quotation_match else None
    if (
        not document_backed_quotation_id
        and generated_quotation_id
        and re.search(re.escape(generated_quotation_id), document_text, flags=re.IGNORECASE)
    ):
        document_backed_quotation_id = generated_quotation_id
    return {
        "quotation_id": document_backed_quotation_id,
        # The caller overwrites this with the required command/API input.
        "supplier_name": None,
        "business_registration_no": payload.get("business_registration_no"),
        "quotation_date": _normalize_date(payload.get("quotation_date", payload.get("date"))),
        "valid_until": _normalize_date(payload.get("valid_until")),
        "currency": payload.get("currency") or "KRW",
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "items": items,
        "notes": payload.get("notes"),
    }


class LocalHuggingFaceQuotationParser:
    """텍스트 모델과 비전 모델을 지연 로딩하고 프로세스 내에서 재사용한다."""

    def __init__(self, text_model: str | None = None, vision_model: str | None = None):
        _enforce_offline_mode()
        self.text_model_name = (
            text_model
            or _project_model_setting("HF_QUOTATION_TEXT_MODEL")
            or os.getenv("HF_QUOTATION_TEXT_MODEL")
            or DEFAULT_TEXT_MODEL
        )
        self.vision_model_name = (
            vision_model
            or _project_model_setting("HF_QUOTATION_VISION_MODEL")
            or os.getenv("HF_QUOTATION_VISION_MODEL")
            or DEFAULT_VISION_MODEL
        )
        self.max_new_tokens = int(
            _project_model_setting("HF_QUOTATION_MAX_NEW_TOKENS")
            or os.getenv("HF_QUOTATION_MAX_NEW_TOKENS")
            or "2048"
        )
        self.vision_max_new_tokens = int(
            _project_model_setting("HF_QUOTATION_VISION_MAX_NEW_TOKENS")
            or os.getenv("HF_QUOTATION_VISION_MAX_NEW_TOKENS")
            or "384"
        )
        self.vision_max_pixels = int(
            _project_model_setting("HF_QUOTATION_VISION_MAX_PIXELS")
            or os.getenv("HF_QUOTATION_VISION_MAX_PIXELS")
            or str(512 * 28 * 28)
        )
        self._text_tokenizer: Any = None
        self._text_model: Any = None
        self._vision_processor: Any = None
        self._vision_model: Any = None
        self._device: str | None = None

    def _runtime(self) -> tuple[Any, str, Any]:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda":
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        else:
            # CPU에서 float32로 강제 변환하면 4B 모델 메모리가 거의 두 배가 된다.
            # 체크포인트에 저장된 dtype(bfloat16 등)을 그대로 유지한다.
            dtype = "auto"
        return torch, device, dtype

    @staticmethod
    def _local_model_error(model_name: str, exc: Exception) -> RuntimeError:
        return RuntimeError(
            f"로컬 Hugging Face 모델 '{model_name}'을 찾거나 로드할 수 없습니다. "
            "보안상 런타임 다운로드는 차단되어 있습니다. 승인된 환경에서 모델을 미리 캐시하거나 "
            "HF_QUOTATION_TEXT_MODEL/HF_QUOTATION_VISION_MODEL에 사내 로컬 경로를 지정하세요. "
            f"원인: {exc}"
        )

    def _load_text_model(self) -> None:
        if self._text_model is not None:
            return
        from transformers import AutoModelForCausalLM, AutoTokenizer

        _, device, dtype = self._runtime()
        try:
            self._text_tokenizer = AutoTokenizer.from_pretrained(
                self.text_model_name,
                local_files_only=True,
                trust_remote_code=False,
            )
            self._text_model = AutoModelForCausalLM.from_pretrained(
                self.text_model_name,
                local_files_only=True,
                trust_remote_code=False,
                dtype=dtype,
            ).to(device).eval()
        except Exception as exc:
            raise self._local_model_error(self.text_model_name, exc) from exc
        self._device = device

    def _load_vision_model(self) -> None:
        if self._vision_model is not None:
            return
        from transformers import AutoModelForImageTextToText, AutoProcessor

        _, device, dtype = self._runtime()
        try:
            self._vision_processor = AutoProcessor.from_pretrained(
                self.vision_model_name,
                local_files_only=True,
                trust_remote_code=False,
                max_pixels=self.vision_max_pixels,
            )
            self._vision_model = AutoModelForImageTextToText.from_pretrained(
                self.vision_model_name,
                local_files_only=True,
                trust_remote_code=False,
                dtype=dtype,
            ).to(device).eval()
        except Exception as exc:
            raise self._local_model_error(self.vision_model_name, exc) from exc
        self._device = device

    def _transcribe_image(self, vision_input: VisionInput) -> str:
        from PIL import Image

        self._load_vision_model()
        image = Image.open(io.BytesIO(vision_input.data)).convert("RGB")
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": (
                    "이 견적서 이미지의 모든 글자와 표를 번역하거나 고치지 말고 원문 그대로 전사하세요. "
                    "특히 문서번호의 SQTN/RFQ, 한글 품목명과 색상, 모든 숫자를 픽셀과 정확히 대조하세요. "
                    "품목명, 규격, 수량, 단가, 공급가액, 세액, 총액, 납기일을 빠뜨리지 마세요. "
                    "값을 추측하거나 계산해 채우지 마세요."
                )},
            ],
        }]
        prompt = self._vision_processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = self._vision_processor(text=prompt, images=[image], return_tensors="pt")
        inputs = _move_inputs_to_device(inputs, self._device or "cpu")
        input_length = inputs["input_ids"].shape[-1]
        outputs = self._vision_model.generate(
            **inputs,
            max_new_tokens=self.vision_max_new_tokens,
            do_sample=False,
        )
        generated = outputs[:, input_length:]
        return self._vision_processor.batch_decode(generated, skip_special_tokens=True)[0].strip()

    def _structure_text(
        self,
        prompt: str,
        document_text: str,
        supplier_name: str | None,
    ) -> _ParsedQuotation:
        self._load_text_model()
        messages = [
            {"role": "system", "content": "한국 구매 견적서를 JSON으로 구조화하는 내부 시스템입니다. JSON만 출력하세요."},
            {"role": "user", "content": prompt},
        ]
        tokenizer = self._text_tokenizer
        if hasattr(tokenizer, "apply_chat_template"):
            try:
                # Qwen3의 thinking 출력을 끄면 작은 모델이 스키마를 되풀이하거나
                # JSON 앞에 긴 추론문을 붙이는 현상을 크게 줄일 수 있다.
                model_prompt = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
            except TypeError:  # pragma: no cover - Qwen3 이전 tokenizer 호환
                model_prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        else:  # pragma: no cover - chat template 없는 모델 호환
            model_prompt = messages[0]["content"] + "\n\n" + messages[1]["content"]
        inputs = tokenizer(model_prompt, return_tensors="pt", truncation=True)
        inputs = _move_inputs_to_device(inputs, self._device or "cpu")
        input_length = inputs["input_ids"].shape[-1]
        outputs = self._text_model.generate(
            **inputs,
            max_new_tokens=self.max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
        generated = outputs[:, input_length:]
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
        payload = _extract_json_object(decoded)
        normalized = _normalize_generated_quotation(payload, document_text, supplier_name)
        return _ParsedQuotation.model_validate(normalized)

    def __call__(
        self,
        prepared: PreparedSource,
        rfq_name: str,
        supplier_name: str | None,
        reflection_errors: list[str],
    ) -> _ParsedQuotation:
        transcriptions = []
        for image in prepared.vision_inputs:
            transcriptions.append(f"[local vision: {image.filename}]\n{self._transcribe_image(image)}")
        document_text = "\n\n".join([prepared.text, *transcriptions])
        reflection = "\n".join(f"- {error}" for error in reflection_errors) or "없음"
        specification_keys = ", ".join(prepared.specification_keys) or "없음"
        prompt = (
            "/no_think\n"
            "다음 외부 공급사 견적 원문에서 실제 견적값을 추출하세요. "
            "설명, JSON Schema, 예시는 출력하지 말고 완성된 JSON 객체 하나만 출력하세요. "
            "문서에 없는 선택값은 null, specifications는 빈 객체로 두세요. "
            "RFQ 번호와 공급사명은 추출하지 마세요. 두 값은 애플리케이션이 별도로 지정합니다. "
            "견적번호는 원문에 실제로 적힌 Supplier Quotation 문서번호만 사용하고 없으면 null로 두세요. "
            "quotation_date에는 견적서의 납기일자를, valid_until에는 견적 유효기간을 넣으세요. "
            "통화 기호와 천 단위 쉼표는 숫자에서 제거하고 DD-MM-YYYY 날짜는 YYYY-MM-DD로 변환하세요.\n"
            f"specifications에 사용할 수 있는 규격 키: {specification_keys}\n"
            f"이전 검토 오류(재추출 시 교정):\n{reflection}\n"
            "최상위 필수 키: quotation_id, business_registration_no, "
            "quotation_date, valid_until, currency, subtotal, tax_amount, total_amount, items, notes.\n"
            "각 items 원소의 필수 키: item_code, item_name, description, quantity, unit, "
            "unit_price, amount, specifications, raw_description. "
            "품목별 delivery_date와 lead_time_days는 보조 필드이며 원문에 명시된 경우에만 넣으세요.\n"
            "subtotal은 세전 공급가액, tax_amount는 세액, total_amount는 세금 포함 총액입니다.\n\n"
            f"견적 원문:\n{document_text}"
        )
        return self._structure_text(prompt, document_text, None)


_LOCAL_PARSER: LocalHuggingFaceQuotationParser | None = None


def get_local_parser() -> LocalHuggingFaceQuotationParser:
    global _LOCAL_PARSER
    if _LOCAL_PARSER is None:
        _LOCAL_PARSER = LocalHuggingFaceQuotationParser()
    return _LOCAL_PARSER


def extract_quotation(
    path: str | Path,
    rfq_name: str,
    *,
    supplier_name: str | None = None,
    supplier_id: str | None = None,
    quotation_id: str | None = None,
    attempt: int = 1,
    reflection_errors: list[str] | None = None,
    rfq_requirements: dict[str, Any] | None = None,
    model_parser: QuotationParser | None = None,
) -> Quotation:
    """한 개 외부 견적을 추출한다. parser 미지정 시 로컬 HF 모델만 사용한다."""
    rfq_name = str(rfq_name or "").strip()
    supplier_name = str(supplier_name or "").strip()
    if not rfq_name:
        raise ValueError("rfq_name은 필수 입력값입니다.")
    if not supplier_name:
        raise ValueError("supplier_name은 필수 입력값입니다.")

    prepared = prepare_source(path)
    if prepared.kind == SourceKind.PORTAL:
        raise ValueError("포털 Supplier Quotation은 외부 견적 추출 대상이 아닙니다.")
    if rfq_requirements:
        # RFQ 값이 견적 추출 결과로 복사되지 않도록 규격의 키 이름만 전달한다.
        specification_keys: set[str] = set()

        def collect_specification_keys(value: Any) -> None:
            if isinstance(value, dict):
                specifications = value.get("specifications")
                if isinstance(specifications, dict):
                    specification_keys.update(
                        str(key) for key in specifications if str(key).strip()
                    )
                for child in value.values():
                    collect_specification_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_specification_keys(child)

        collect_specification_keys(rfq_requirements)
        prepared.specification_keys = sorted(specification_keys)

    parser = model_parser or get_local_parser()
    parsed_value = parser(prepared, rfq_name, supplier_name, reflection_errors or [])
    parsed = parsed_value if isinstance(parsed_value, _ParsedQuotation) else _ParsedQuotation.model_validate(parsed_value)
    payload = parsed.model_dump()
    parsed_quotation_id = str(parsed.quotation_id or "").strip()
    if parsed_quotation_id.casefold() == rfq_name.casefold():
        parsed_quotation_id = ""
    fallback_quotation_id = f"EXT-{Path(path).stem}"
    payload["quotation_id"] = (
        str(quotation_id).strip()
        if quotation_id
        else (parsed_quotation_id or fallback_quotation_id)
    )
    payload["rfq_name"] = rfq_name
    payload["supplier_id"] = supplier_id
    payload["supplier_name"] = supplier_name
    payload["items"] = [QuotationItem.model_validate(item) for item in payload["items"]]
    payload["source"] = QuotationSource(
        kind=prepared.kind,
        filename=Path(path).name,
        path=str(Path(path).resolve()),
    )
    payload["extraction_attempt"] = attempt
    model_label = parser.text_model_name if isinstance(parser, LocalHuggingFaceQuotationParser) else "주입 파서"
    payload["extraction_evidence"] = [
        *prepared.evidence,
        f"로컬 HF 텍스트 모델: {model_label}",
        "외부 API 전송 없음",
    ]
    if not quotation_id and not parsed_quotation_id:
        payload["extraction_evidence"].append(
            f"문서에서 견적번호를 확인하지 못해 파일명 기반 번호 사용: {fallback_quotation_id}"
        )
    return Quotation.model_validate(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="로컬 Hugging Face 기반 외부 견적 추출")
    parser.add_argument("input", help="xlsx/xls/csv/pdf/png/jpg/eml/txt 파일")
    parser.add_argument("--rfq", required=True, help="RFQ 이름")
    parser.add_argument(
        "--supplier-name",
        required=True,
        help="고정 공급사명(모델이 추출하거나 변경하지 않음)",
    )
    parser.add_argument("--supplier-id")
    parser.add_argument("--quotation-id")
    parser.add_argument("--attempt", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--reflection", action="append", default=[], help="이전 추출 오류(여러 번 지정 가능)")
    parser.add_argument("--rfq-context", help="선택: RFQ 요구사항 JSON. 규격 키 추출에 사용")
    parser.add_argument("--text-model", help="로컬 캐시 또는 사내 로컬 텍스트 모델 경로")
    parser.add_argument("--vision-model", help="로컬 캐시 또는 사내 로컬 비전 모델 경로")
    parser.add_argument(
        "--register-erp",
        action="store_true",
        help="추출 결과를 로컬 파일 없이 ERPNext Supplier Quotation Draft로 등록",
    )
    parser.add_argument(
        "--erp-dry-run",
        action="store_true",
        help="--register-erp 사용 시 ERP POST 없이 매핑 payload와 중복 여부만 확인",
    )
    parser.add_argument("--output", help="결과 JSON 경로. 생략하면 stdout")
    args = parser.parse_args()
    if args.erp_dry_run and not args.register_erp:
        parser.error("--erp-dry-run은 --register-erp와 함께 사용해야 합니다.")

    local_parser = LocalHuggingFaceQuotationParser(args.text_model, args.vision_model)
    quotation = extract_quotation(
        args.input,
        args.rfq,
        supplier_name=args.supplier_name,
        supplier_id=args.supplier_id,
        quotation_id=args.quotation_id,
        attempt=args.attempt,
        reflection_errors=args.reflection,
        rfq_requirements=load_json(args.rfq_context) if args.rfq_context else None,
        model_parser=local_parser,
    )
    if args.output:
        dump_json(quotation, args.output)
        print(f"추출 완료(외부 전송 없음): {args.output}")
    elif not args.register_erp:
        print(dump_json(quotation))

    if args.register_erp:
        try:
            from .quotation_registrar import register_supplier_quotation
        except ImportError:  # quotation_filter 폴더에서 직접 실행할 때
            from quotation_registrar import register_supplier_quotation
        registration = register_supplier_quotation(quotation, dry_run=args.erp_dry_run)
        print(json.dumps(registration, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
