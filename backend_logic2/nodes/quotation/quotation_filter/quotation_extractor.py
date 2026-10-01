"""외부 견적을 공급자 어댑터로 공통 Quotation JSON에 추출한다.

보안 원칙:
    - 기본 local 공급자는 견적 원문과 이미지를 외부 API로 전송하지 않는다.
    - RunPod 공급자는 명시적으로 설정한 경우에만 HTTPS로 문서를 전송한다.
    - API 키는 공급자 어댑터 내부의 인증 헤더에서만 사용한다.


"""

from __future__ import annotations

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

from .quotation_models import Quotation, QuotationItem, QuotationSource, SourceKind


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".csv"}
DOCX_SUFFIXES = {".docx"}
PDF_SUFFIXES = {".pdf"}
EMAIL_SUFFIXES = {".eml"}
TEXT_SUFFIXES = {".txt", ".md"}
MAX_VISUAL_PDF_PAGES = 8  # Match the RunPod worker's default MAX_PDF_PAGES.

VALID_UNTIL_LABELS = (
    "유효기간", "견적유효기간", "validuntil", "validity",
)
DELIVERY_DATE_LABELS = (
    "예상납품일", "납품예정일", "납기일", "납기일자", "납기예정일",
    "출고예정일", "배송예정일", "인도예정일", "도착예정일", "발송예정일",
    "deliverydate", "deliverby", "shippingdate", "shipmentdate",
    "dispatchdate", "arrivaldate", "eta",
)
LEAD_TIME_LABELS = (
    "리드타임", "소요기간", "제작기간", "공급기간", "leadtime",
)

DEFAULT_TEXT_MODEL = "Qwen/Qwen3.5-9B"
DEFAULT_VISION_MODEL = "Qwen/Qwen3.5-9B"
DEFAULT_VISION_ADAPTER = "lyc9872/qwen_3.5_9b_peft"
PROJECT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"

FINETUNED_SYSTEM_PROMPT = (
    "견적서 문서에서 값을 읽어 지정된 JSON만 출력한다. "
    "문서에 없는 값은 null로 출력한다."
)
FINETUNED_USER_PROMPT = """이 견적서의 정보를 아래 규칙과 JSON 스키마에 맞게 추출하세요.

[추출 규칙]

1. supplier_name은 견적서를 발행하거나 물품을 공급하는 회사명입니다. 문서에 '공급사명' 또는 '공급자명' 필드가 있으면 그 값을 우선하고, 없으면 발행사명으로 판단되는 상호를 사용합니다. 고객사·수신처명은 supplier_name으로 사용하지 않습니다.
2. quotation_id와 item_code의 영문, 숫자, 하이픈을 한 글자씩 구분합니다. 불명확한 값을 임의로 완성하지 않습니다.
3. 날짜는 의미가 명확한 경우 YYYY-MM-DD로 변환합니다. 일부만 보이거나 날짜인지 확실하지 않으면 null입니다.
4. 금액과 수량은 통화 기호와 천 단위 쉼표를 제거한 JSON 숫자로 출력합니다. 문서의 금액이 계산 결과와 다르더라도 문서에 적힌 값을 그대로 사용합니다.
5. currency는 문서에 통화명이나 통화 기호가 명시된 경우에만 ISO 통화 코드(KRW, USD, JPY 등)로 출력하고, 표시가 없으면 null입니다.
6. subtotal은 문서 전체의 세전 공급가액 합계이고, item.amount는 해당 품목 행의 공급가액입니다. 두 값을 서로 대신 사용하지 않습니다.
7. item_name에는 제품명만, description에는 규격·사양·설명 내용을 기록합니다. specifications에는 문서에 표시된 규격 항목을 키-값으로 기록합니다.
8. raw_description에는 해당 품목 행에 보이는 품목명과 규격·설명 원문을 읽는 순서대로 보존합니다. 원문에 없는 구분 문구를 만들지 않습니다.
9. 품목이 여러 개이면 생략하거나 합치지 말고 items 배열에 위에서 아래 순서로 모두 출력합니다.
10. 전체 페이지의 본문·표·상업 조건·기술 특약을 모두 읽은 뒤, 위치나 제목이 아니라 내용의 의미로 다음 두 가지로 분류해 간결하게 정리합니다. (가) 규격사항: 치수, 재질, 출력, 전원, 효율, 보호 등급, 절연, 설치 형식, 적용 표준 및 공급 제외 제품 등의 기술적 조건은 해당 품목의 specifications에 항목별 키-값으로 기록합니다. (나) 그 외 사항: 지급·결제, 보증, 운송·포장, 설치공사 포함/제외, 무상 제출 서류, 조건부 할인 등 거래·서비스 조건은 사람이 확인할 수 있는 짧은 문장으로 notes에 줄바꿈하여 정리합니다. 할인 기준 수량·할인율, 결제 기한·기산점, 보증 기간, 포함/제외 범위 및 적용 조건은 요약해도 반드시 보존합니다. 예: '120개 이상 구매 시 10% 할인'을 조건 없이 '10% 할인'으로 줄이거나 현재 주문에 적용된 할인이라고 단정하지 않습니다. 한 문장에 두 종류가 섞여 있으면 의미 단위로 나눕니다. 예: 'IE3 이상이며 납품일 기준 12개월 보증'은 specifications의 효율 등급과 notes의 보증으로 분리합니다. 원문의 값·단위·부정·제외·적용 품목·범위를 보존하고, 누락·추측·중복 기재하지 않습니다. 규격이 특약 영역에 있어도 specifications로 옮기며 notes에는 다시 복사하지 않습니다. notes 키는 반드시 출력하고, 그 외 사항이 실제로 없을 때만 null입니다.
11. 문서 상단의 '유효기간', '견적 유효기간', 'Validity', 'Valid Till' 날짜는 valid_until에 기록합니다. quotation_date나 납기일과 혼동하지 않습니다.
12. 품목 행의 '납기일', '납품일', '납품예정일' 또는 특약사항의 '예상 납품일'은 해당 품목의 expected_delivery_date에 기록합니다. 모든 품목에 공통으로 표시된 날짜라면 각 품목에 같은 날짜를 기록합니다.
13. 전체 페이지 이미지 뒤에 상단 또는 하단 확대 이미지가 추가로 제공될 수 있습니다. 확대 이미지는 같은 문서의 세부 영역이므로 품목을 중복 생성하지 말고, 전체 이미지에서 작게 보여 누락되기 쉬운 유효기간·납기일·특약사항을 보완하는 데 사용합니다.

[출력 스키마]
{
  "quotation_id": string|null,
  "supplier_name": string|null,
  "business_registration_no": string|null,
  "quotation_date": string|null,
  "valid_until": string|null,
  "currency": string|null,
  "subtotal": number|null,
  "tax_amount": number|null,
  "total_amount": number|null,
  "items": [
    {
      "item_code": string|null,
      "item_name": string|null,
      "description": string|null,
      "quantity": number|null,
      "unit": string|null,
      "unit_price": number|null,
      "amount": number|null,
      "expected_delivery_date": string|null,
      "lead_time_days": number|null,
      "specifications": object,
      "raw_description": string|null
    }
  ],
  "notes": string|null
}

유효한 JSON 객체 하나만 출력하세요."""

TEXT_STRUCTURE_SYSTEM_PROMPT = (
    "견적서에서 추출된 원문 텍스트를 해석해 지정된 JSON만 출력한다. "
    "원문에 없는 값은 null로 출력하고 OCR 충돌값을 임의로 합치지 않는다."
)
TEXT_STRUCTURE_USER_PROMPT = FINETUNED_USER_PROMPT.replace(
    "이 견적서의 정보를",
    "아래 [견적 원문]의 정보를",
    1,
).replace(
    "13. 전체 페이지 이미지 뒤에 상단 또는 하단 확대 이미지가 추가로 제공될 수 있습니다. 확대 이미지는 같은 문서의 세부 영역이므로 품목을 중복 생성하지 말고, 전체 이미지에서 작게 보여 누락되기 쉬운 유효기간·납기일·특약사항을 보완하는 데 사용합니다.",
    "13. 원문에는 전체 이미지와 확대 영역에서 전사한 문장이 중복될 수 있습니다. 완전히 같은 근거만 중복으로 판단하고, 날짜·금액·수량·품목코드가 다르면 어느 한쪽을 임의로 선택하거나 합치지 않습니다.\n"
    "14. supplier_name은 참고용 문서 값입니다. 실제 ERPNext 등록 공급사는 RFQ 회신 문맥에서 백엔드가 확정하며 이 출력으로 변경되지 않습니다.\n"
    "15. 위 [출력 스키마]에 나열된 키는 하나도 빠짐없이 모두 출력하세요. 문서에 해당 내용이 없으면 그 키는 유지한 채 값만 null로 출력하고, 키 자체를 생략하지 마세요. notes처럼 내용이 아예 없을 수 있는 필드도 마찬가지로 키는 항상 포함되어야 합니다.",
)


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
    expected_delivery_date: date | None = None
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
    currency: str | None = None
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
    # Original image/PDF bytes retained for remote VLM adapters.  Local
    # parsers continue to use text/vision_inputs and never inspect this field.
    document_inputs: list[VisionInput] = field(default_factory=list)
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
    if suffix in DOCX_SUFFIXES:
        return SourceKind.DOCX
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
        decoded = _decode_text_document(data)
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


def _decode_text_document(data: bytes) -> str:
    """Accept UTF-8 and legacy Korean CP949 without silently corrupting text."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("텍스트 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949로 저장해 주세요.")


def _render_scanned_pdf(data: bytes, filename: str) -> list[VisionInput]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - 설치 환경 오류
        raise RuntimeError("스캔 PDF의 로컬 비전 처리에는 PyMuPDF가 필요합니다.") from exc

    document = fitz.open(stream=data, filetype="pdf")
    images: list[VisionInput] = []
    try:
        if document.page_count > MAX_VISUAL_PDF_PAGES:
            raise ValueError(f"이미지 PDF는 최대 {MAX_VISUAL_PDF_PAGES}페이지까지 처리합니다.")
        for index, page in enumerate(document, 1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            images.append(VisionInput(
                data=pixmap.tobytes("png"),
                filename=f"{Path(filename).stem}-page-{index}.png",
            ))
    finally:
        document.close()
    return images


def _tables_to_text(tables: list[list[list[str]]]) -> str:
    """행x열 표 그리드를 LLM 프롬프트용 평문으로 렌더링한다.

    " | "로 열을 구분해 좌표 정보 없이도 어느 값이 어느 열인지 LLM이 구분할 수
    있게 한다. pypdf.extract_text()의 통짜 평문화(열 순서가 보장되지 않음)와
    가장 크게 다른 지점이 여기다.
    """
    rendered = []
    for index, grid in enumerate(tables, start=1):
        lines = [f"[표 {index}]"]
        for row in grid:
            lines.append(" | ".join(cell.replace("\n", " ").strip() for cell in row))
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered)


def _docx_to_text(source: str | Path | bytes, filename: str | None = None) -> tuple[str, list[str]]:
    """DOCX 본문과 표를 XML 기반으로 직접 읽어 텍스트 모델 입력을 만든다."""
    from .docx_table_extractor import extract_paragraphs, extract_tables_from_docx

    paragraphs = extract_paragraphs(source)
    tables = extract_tables_from_docx(source)
    sections: list[str] = []
    if paragraphs:
        sections.append("[본문]\n" + "\n".join(paragraphs))
    if tables:
        sections.append(_tables_to_text(tables))
    if not sections and not _docx_to_images(source, filename, allow_compact_image=True):
        source_name = filename or (Path(source).name if not isinstance(source, bytes) else "attachment.docx")
        raise ValueError(f"DOCX에서 읽을 수 있는 본문이나 표가 없습니다: {source_name}")

    evidence = [
        f"DOCX 본문 {len(paragraphs)}개, 표 {len(tables)}개 로컬 추출",
        "DOCX XML 텍스트와 표 구조 직접 판독(OCR 미사용)",
    ]
    return "\n\n".join(sections), evidence


def _docx_to_images(
    source: str | Path | bytes,
    filename: str | None = None,
    *,
    allow_compact_image: bool = False,
) -> list[VisionInput]:
    """Send substantive body pictures to vision, not logos or seals."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn
    from PIL import Image
    from .docx_table_extractor import _open_document

    document = _open_document(source)
    found: list[VisionInput] = []
    seen: set[str] = set()
    # Body XML includes table cells, but not header/footer logos. Only images
    # actually referenced by visible body content are candidates.
    for element in document.element.body.iter(qn("a:blip")):
        relationship_id = element.get(qn("r:embed"))
        if not relationship_id or relationship_id in seen:
            continue
        seen.add(relationship_id)
        rel = document.part.rels.get(relationship_id)
        if rel is None or rel.reltype != RT.IMAGE or rel.is_external:
            continue
        image_part = rel.target_part
        media_type = str(image_part.content_type or "").lower()
        if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            continue
        blob = image_part.blob
        try:
            with Image.open(io.BytesIO(blob)) as picture:
                width, height = picture.size
        except (OSError, ValueError):
            # A broken embedded picture must not discard the readable DOCX.
            continue
        # Common body logos and stamps are short or small. A document scan,
        # screenshot or specification table is normally materially larger.
        substantive = min(width, height) >= 250 and width * height >= 120_000
        compact_table = allow_compact_image and width >= 500 and height >= 100 and width * height >= 50_000
        if not (substantive or compact_table):
            continue
        suffix = ".jpg" if media_type == "image/jpeg" else "." + media_type.split("/")[1]
        stem = Path(filename or "attachment.docx").stem
        found.append(VisionInput(data=blob, filename=f"{stem}-embedded-{len(found) + 1}{suffix}"))
    return found


def _pdf_page_needs_vision(page: Any) -> bool:
    """Detect visible scanned content, not merely the absence of a text layer."""
    native = page.get_text()
    compact = re.sub(r"\s+", "", native)
    lower = native.lower()
    corrupt = (
        "(cid:" in lower
        or (len(compact) >= 10 and compact.count("\ufffd") / len(compact) >= 0.1)
    )
    top = page.rect.y0 + page.rect.height * 0.13
    bottom = page.rect.y0 + page.rect.height * 0.88
    visible_body_chars = sum(
        len(span["chars"])
        for span in page.get_texttrace()
        if span["type"] != 3 and span["bbox"][1] < bottom and span["bbox"][3] > top
    )
    page_area = max(1.0, page.rect.width * page.rect.height)
    visible_image_area = sum(
        max(0.0, min(float(image["bbox"][2]), page.rect.x1) - max(float(image["bbox"][0]), page.rect.x0))
        * max(0.0, min(float(image["bbox"][3]), page.rect.y1) - max(float(image["bbox"][1]), page.rect.y0))
        for image in page.get_image_info()
    )
    image_ratio = min(1.0, visible_image_area / page_area)
    if corrupt:
        return True
    if image_ratio >= 0.55:
        if visible_body_chars >= 80:
            return False  # Digital quote over a full-page stationery background.
        if not compact:
            # A printer may append a full-page white raster. Avoid sending
            # that blank page (and every preceding digital page) to the GPU.
            preview = page.get_pixmap(dpi=36, alpha=False)
            samples = preview.samples
            if not any(
                min(samples[index:index + preview.n]) < 170
                for index in range(0, len(samples), preview.n)
            ):
                return False
        return True  # Full-page scan, including a hidden OCR text layer.
    if len(compact) < 30 and visible_body_chars < 15 and image_ratio >= 0.10:
        return True  # Short footer or watermark alongside the actual image.
    if not compact and not image_ratio:
        drawings = page.get_drawings()
        # One outline/border is blank stationery. Outlined text has many paths.
        return len(drawings) >= 20 or sum(len(path.get("items", [])) for path in drawings) >= 60
    return False


def _pdf_to_source(data: bytes, filename: str) -> tuple[str, list[VisionInput], list[str]]:

    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - 설치 환경 오류
        raise RuntimeError("PDF 추출에는 PyMuPDF가 필요합니다.") from exc

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise ValueError(f"손상되었거나 지원하지 않는 PDF입니다: {filename}") from exc
    try:
        page_count = document.page_count
        if document.needs_pass:
            raise ValueError(f"암호화된 PDF는 비밀번호 없이 추출할 수 없습니다: {filename}")
        if not page_count:
            raise ValueError(f"페이지가 없는 PDF입니다: {filename}")
        # PyMuPDF4LLM otherwise auto-OCRs image-only pages and makes them
        # look like digital text. One substantive visual page triggers the
        # vision route for the whole PDF, preserving cross-page context.
        needs_vision = any(_pdf_page_needs_vision(page) for page in document)
        extraction_method = "pymupdf4llm"
        extraction_note = "pymupdf4llm 마크다운 변환 직접 판독(OCR 미사용)"
        if needs_vision:
            if page_count > MAX_VISUAL_PDF_PAGES:
                raise ValueError(f"이미지 PDF는 최대 {MAX_VISUAL_PDF_PAGES}페이지까지 처리합니다: {filename}")
            markdown_text = ""
        else:
            try:
                import pymupdf4llm
            except ImportError as exc:  # pragma: no cover - 설치 환경 오류
                raise RuntimeError("디지털 PDF 추출에는 pymupdf4llm이 필요합니다.") from exc
            rotated_pages = {
                index for index, page in enumerate(document)
                if page.rotation and page.get_text().strip()
            }
            if rotated_pages:
                # PyMuPDF4LLM 1.28.2 drops text on /Rotate pages, even when
                # other pages in the same document produce valid Markdown.
                sections = []
                native_fallback_pages = []
                for index, page in enumerate(document):
                    if index in rotated_pages:
                        page_text = page.get_text(sort=True).strip()
                    else:
                        page_text = (pymupdf4llm.to_markdown(
                            document, pages=[index], use_ocr=False,
                        ) or "").strip()
                        if not page_text:
                            page_text = page.get_text(sort=True).strip()
                            if page_text:
                                native_fallback_pages.append(index + 1)
                    if page_text:
                        sections.append(f"[page {index + 1}]\n{page_text}")
                markdown_text = "\n\n".join(sections)
                extraction_method = "pymupdf4llm + PyMuPDF get_text"
                rotation_details = ", ".join(
                    f"{index + 1}페이지({document[index].rotation}도)"
                    for index in sorted(rotated_pages)
                )
                extraction_note = (
                    f"회전 페이지 {rotation_details}는 PyMuPDF 디지털 텍스트로 추출; "
                    "나머지는 pymupdf4llm 마크다운 사용(OCR 미사용)"
                )
                if native_fallback_pages:
                    extraction_note += f"; 마크다운 빈 페이지 {native_fallback_pages}도 PyMuPDF 폴백"
            else:
                markdown_text = (pymupdf4llm.to_markdown(document, use_ocr=False) or "").strip()
            if not markdown_text:
                native_pages = [
                    f"[page {index}]\n{page_text}"
                    for index, page in enumerate(document, 1)
                    if (page_text := page.get_text(sort=True).strip())
                ]
                if native_pages:
                    markdown_text = "\n\n".join(native_pages)
                    extraction_method = "PyMuPDF get_text"
                    extraction_note = (
                        "pymupdf4llm 빈 결과로 PyMuPDF 디지털 텍스트 폴백 적용"
                        "(OCR 미사용)"
                    )
    finally:
        document.close()

    if not markdown_text:
        evidence = [
            f"PDF {page_count}페이지 중 이미지 기반 페이지 포함" if needs_vision
            else f"PDF {page_count}페이지에서 읽을 수 있는 디지털 텍스트 없음",
            "모든 페이지를 이미지로 변환해 비전 모델 입력으로 전달",
        ]
        return "[스캔 PDF]", _render_scanned_pdf(data, filename), evidence

    return markdown_text, [], [
        f"PDF {page_count}페이지에서 디지털 텍스트 {len(markdown_text)}자 로컬 추출({extraction_method})",
        extraction_note,
    ]


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
    document_inputs: list[VisionInput] = []
    evidence = [f"이메일 파일 파싱: {filename}"]

    body = message.get_body(preferencelist=("plain", "html"))
    if body:
        try:
            content = body.get_content()
        except (LookupError, UnicodeError):
            content = _decode_text_document(body.get_payload(decode=True) or b"")
        body_parts.append(_strip_html(content) if body.get_content_type() == "text/html" else str(content))

    for part in message.iter_attachments():
        attachment_name = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True) or b""
        suffix = Path(attachment_name).suffix.lower()
        evidence.append(f"이메일 첨부 로컬 처리: {attachment_name}")
        if suffix in EXCEL_SUFFIXES:
            body_parts.append(f"[attachment: {attachment_name}]\n{_spreadsheet_to_text(payload, attachment_name)}")
        elif suffix in DOCX_SUFFIXES:
            docx_text, docx_evidence = _docx_to_text(payload, attachment_name)
            body_parts.append(f"[attachment: {attachment_name}]\n{docx_text}")
            docx_images = _docx_to_images(payload, attachment_name, allow_compact_image=not docx_text.strip())
            vision_inputs.extend(docx_images)
            document_inputs.extend(docx_images)
            evidence.extend(docx_evidence)
        elif suffix in PDF_SUFFIXES:
            pdf_text, pdf_images, pdf_evidence = _pdf_to_source(payload, attachment_name)
            body_parts.append(f"[attachment: {attachment_name}]\n{pdf_text}")
            vision_inputs.extend(pdf_images)
            if pdf_images:
                document_inputs.append(VisionInput(data=payload, filename=attachment_name))
            evidence.extend(pdf_evidence)
        elif suffix in IMAGE_SUFFIXES:
            image_input = VisionInput(data=payload, filename=attachment_name)
            vision_inputs.append(image_input)
            document_inputs.append(image_input)
        elif part.get_content_maintype() == "text":
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                content = _decode_text_document(payload)
            body_parts.append(f"[attachment: {attachment_name}]\n{content}")

    return PreparedSource(
        kind=SourceKind.EMAIL,
        text=f"{headers}\n\n[Body]\n" + "\n\n".join(body_parts),
        vision_inputs=vision_inputs,
        document_inputs=document_inputs,
        evidence=evidence,
    )


def prepare_source_bytes(data: bytes, filename: str) -> PreparedSource:
    """파일을 디스크에 저장하지 않고 이름과 바이트만으로 모델 입력을 준비한다."""
    kind = classify_source(filename)
    if kind == SourceKind.DOCX:
        text, evidence = _docx_to_text(data, filename)
        images = _docx_to_images(data, filename, allow_compact_image=not text.strip())
        return PreparedSource(
            kind=kind, text=text, vision_inputs=images, document_inputs=images,
            evidence=[*evidence, f"DOCX 내 이미지 {len(images)}개 비전 입력"] if images else evidence,
        )
    if kind == SourceKind.EXCEL:
        return PreparedSource(kind=kind, text=_spreadsheet_to_text(data, filename), evidence=[f"표 파일 메모리 파싱: {filename}"])
    if kind == SourceKind.PDF:
        text, vision_inputs, evidence = _pdf_to_source(data, filename)
        return PreparedSource(
            kind=kind,
            text=text,
            vision_inputs=vision_inputs,
            document_inputs=[VisionInput(data=data, filename=filename)] if vision_inputs else [],
            evidence=evidence,
        )
    if kind == SourceKind.IMAGE:
        image_input = VisionInput(data=data, filename=filename)
        return PreparedSource(
            kind=kind,
            text="[견적서 이미지]",
            vision_inputs=[image_input],
            document_inputs=[image_input],
            evidence=[f"메모리 비전 입력 준비: {filename}"],
        )
    if kind == SourceKind.EMAIL:
        return _email_to_source(data, filename)
    return PreparedSource(kind=kind, text=_decode_text_document(data), evidence=[f"텍스트 파일 메모리 파싱: {filename}"])


def prepare_source(path: str | Path) -> PreparedSource:
    path = Path(path)
    return prepare_source_bytes(path.read_bytes(), path.name)


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


def _normalize_currency(value: Any) -> str | None:
    """Normalize an explicitly extracted currency without inventing a default."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    upper = text.upper()
    aliases = {
        "원": "KRW", "₩": "KRW", "WON": "KRW", "KOREAN WON": "KRW",
        "달러": "USD", "$": "USD", "US$": "USD", "DOLLAR": "USD",
        "DOLLARS": "USD", "US DOLLAR": "USD", "US DOLLARS": "USD",
        "엔": "JPY", "円": "JPY", "YEN": "JPY", "JAPANESE YEN": "JPY",
        "유로": "EUR", "€": "EUR", "EURO": "EUR", "EUROS": "EUR",
        "위안": "CNY", "元": "CNY", "RMB": "CNY", "YUAN": "CNY",
        "파운드": "GBP", "£": "GBP", "POUND": "GBP", "POUNDS": "GBP",
    }
    if upper in aliases:
        return aliases[upper]
    return upper if re.fullmatch(r"[A-Z]{3}", upper) else None


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


def _labelled_date_values(
    document_text: str,
    labels: tuple[str, ...],
) -> list[str]:
    """Return distinct, valid dates that are explicitly attached to a label."""

    compact = re.sub(r"\s+", "", str(document_text or "")).casefold()
    if not compact:
        return []
    joined = "|".join(
        re.escape(label.casefold())
        for label in sorted(labels, key=len, reverse=True)
    )
    date_pattern = r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})"
    values: list[str] = []
    for match in re.finditer(rf"(?:{joined})[:：]?{date_pattern}", compact):
        year, month, day = map(int, match.groups()[-3:])
        try:
            normalized = date(year, month, day).isoformat()
        except ValueError:
            continue
        if normalized not in values:
            values.append(normalized)
    return values


def _labelled_lead_time_days(document_text: str) -> list[int]:
    """Return explicit day/week lead times without inferring from bare words."""

    compact = re.sub(r"\s+", "", str(document_text or "")).casefold()
    if not compact:
        return []
    joined = "|".join(
        re.escape(label.casefold())
        for label in sorted(LEAD_TIME_LABELS, key=len, reverse=True)
    )
    values: list[int] = []
    pattern = rf"(?:{joined})[:：]?(\d{{1,4}})(일|days?|주|weeks?)"
    for match in re.finditer(pattern, compact, flags=re.IGNORECASE):
        amount = int(match.group(1))
        unit = match.group(2).casefold()
        days = amount * 7 if unit in {"주", "week", "weeks"} else amount
        if days not in values:
            values.append(days)
    return values


def _normalize_lead_time_days(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _document_note_sections(document_text: str) -> list[str]:
    """Recover explicitly headed clauses, not the entire OCR document.

    Preserve clause line breaks and stop at tables, page/footer boundaries or
    an unrelated section. These source-backed sections can supplement partial
    model notes as well as a missing notes key, without another inference.
    """
    heading = re.compile(
        r"^(특약\s*사항|특이\s*사항|비고|조건|상업\s*조건|거래\s*조건|기술\s*특약)"
        r"(?:\s*[:：—–-]\s*(.*))?$"
    )
    boundary = re.compile(
        r"^(?:\| |\||[-=]{3,}$|\d+\s*/\s*\d+$|테스트용|정답\s*파일|"
        r"견적번호|견적일|유효기한|납품\s*및\s*문서\s*적용|부속\s*명세|"
        r"공급가액|부가세|청구\s*합계|합계|공급사\s*[:：]|납품\s*예정일\s*[:：])"
    )
    enumeration = re.compile(r"^(?:[①-⑳]|[A-Za-z0-9]+[.)]|[-•·])\s*")

    def clean(line: str) -> str:
        value = re.sub(r"^#{1,6}\s+", "", line.strip()).replace("**", "")
        return re.sub(r"^[\[【](.*?)[\]】]", r"\1", value).strip()

    lines = document_text.splitlines()
    sections: list[str] = []
    for index, line in enumerate(lines):
        match = heading.fullmatch(clean(line))
        if not match:
            continue
        block = [clean(line)]
        for offset in range(index + 1, len(lines)):
            value = clean(lines[offset])
            if not value:
                # Blank lines between numbered clauses are layout, not an end.
                next_line = next((clean(s) for s in lines[offset + 1:] if clean(s)), "")
                if len(block) == 1 or enumeration.match(next_line):
                    continue
                break
            if (heading.fullmatch(value) or boundary.match(value)
                    or re.match(r"^#{1,6}\s+", lines[offset].strip())):
                break
            block.append(value)
        if len(block) > 1 or match.group(2):
            sections.append("\n".join(block))
    # Payment conditions can occur on a delivery line outside headed sections.
    for line in lines:
        match = re.search(r"(?:^|[/|])\s*((?:지급|결제|지불)\s*[:：].+)", line)
        if match:
            sections.append(match.group(1).strip())
    return list(dict.fromkeys(sections))


def extract_document_fallbacks(document_text: str) -> dict[str, Any]:
    """Read explicit terms/dates that a model may omit from document text.

    PDF text layers commonly insert whitespace between every Korean syllable
    and may split a date across lines.  Compacting whitespace makes labelled
    fields deterministic without guessing values that are absent from the
    document. Scalar values fill only omissions; explicitly headed note
    sections also supplement partial model notes.
    """

    source_text = str(document_text or "")
    compact = re.sub(r"\s+", "", source_text)
    if not compact:
        return {}

    valid_until_values = _labelled_date_values(source_text, VALID_UNTIL_LABELS)
    delivery_values = _labelled_date_values(source_text, DELIVERY_DATE_LABELS)
    lead_time_values = _labelled_lead_time_days(source_text)
    valid_until = valid_until_values[0] if len(valid_until_values) == 1 else None
    expected_delivery_date = delivery_values[0] if len(delivery_values) == 1 else None
    lead_time_days = lead_time_values[0] if len(lead_time_values) == 1 else None

    note_labels = (
        ("예상납품일", "예상 납품일"),
        ("납품장소", "납품 장소"),
        ("사양특기", "사양 특기"),
        ("발주시", "발주 조건"),
    )
    found = [(compact.find(label), label, display) for label, display in note_labels]
    found = sorted((position, label, display) for position, label, display in found if position >= 0)
    notes: list[str] = []
    for index, (position, label, display) in enumerate(found):
        start = position + len(label)
        if start < len(compact) and compact[start] in ":：":
            start += 1
        end = found[index + 1][0] if index + 1 < len(found) else len(compact)
        value = compact[start:end].strip("•·-:：")
        if value:
            notes.append(f"{display}: {value}")

    # The conditional visual recovery pass emits one explicitly labelled line
    # per free-form term. Keep line boundaries so arbitrary special clauses
    # can be recovered without teaching this deterministic parser their text.
    generic_note_pattern = re.compile(
        r"(?im)^\s*(?:[-•·]\s*)?[\[【]?(특약사항|특이사항|비고|조건)"
        r"[\]】]?\s*[:：]\s*(.+?)\s*$"
    )
    for match in generic_note_pattern.finditer(source_text):
        note = f"{match.group(1)}: {match.group(2).strip()}"
        if note not in notes:
            notes.append(note)

    result: dict[str, Any] = {}
    if valid_until:
        result["valid_until"] = valid_until
    if expected_delivery_date:
        result["expected_delivery_date"] = expected_delivery_date
    if lead_time_days is not None:
        result["lead_time_days"] = lead_time_days
    if notes:
        result["notes"] = "\n".join(notes)
    note_sections = _document_note_sections(source_text)
    if note_sections:
        result["note_sections"] = note_sections
    conflicts: dict[str, list[Any]] = {}
    if len(valid_until_values) > 1:
        conflicts["valid_until"] = valid_until_values
    if len(delivery_values) > 1:
        conflicts["expected_delivery_date"] = delivery_values
    if len(lead_time_values) > 1:
        conflicts["lead_time_days"] = lead_time_values
    if conflicts:
        result["conflicts"] = conflicts
    return result


def apply_document_fallbacks(
    payload: dict[str, Any],
    fallbacks: dict[str, Any] | None,
    *,
    include_notes: bool = True,
) -> dict[str, Any]:
    """Fill omitted scalars and retain explicit source-backed note sections."""

    fallbacks = fallbacks or {}
    if not payload.get("valid_until") and fallbacks.get("valid_until"):
        payload["valid_until"] = fallbacks["valid_until"]
    if include_notes and not str(payload.get("notes") or "").strip() and fallbacks.get("notes"):
        payload["notes"] = fallbacks["notes"]
    for section in (fallbacks.get("note_sections") or []) if include_notes else []:
        existing = str(payload.get("notes") or "").strip()
        # Identical OCR/recovery sections must not accumulate on repeated calls.
        compact_existing = re.sub(r"\s+", "", existing)
        missing = [line for line in section.splitlines()
                   if re.sub(r"\s+", "", line) not in compact_existing]
        if missing:
            payload["notes"] = "\n".join(filter(None, [existing, *missing]))
    delivery = fallbacks.get("expected_delivery_date")
    lead_time_days = fallbacks.get("lead_time_days")
    if delivery:
        for item in payload.get("items") or []:
            if isinstance(item, dict) and not item.get("expected_delivery_date"):
                item["expected_delivery_date"] = delivery
    if lead_time_days is not None:
        for item in payload.get("items") or []:
            if isinstance(item, dict) and item.get("lead_time_days") is None:
                item["lead_time_days"] = lead_time_days
    return payload


def validate_document_delivery_evidence(
    payload: dict[str, Any],
    document_text: str,
) -> dict[str, Any]:
    """Discard model delivery values that lack matching labelled source text."""

    fallbacks = extract_document_fallbacks(document_text)
    evidenced_delivery_date = fallbacks.get("expected_delivery_date")
    evidenced_lead_time_days = fallbacks.get("lead_time_days")
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        model_delivery_date = _normalize_date(
            item.get("expected_delivery_date", item.get("delivery_date"))
        )
        if model_delivery_date != evidenced_delivery_date:
            item["expected_delivery_date"] = None
        model_lead_time_days = _normalize_lead_time_days(item.get("lead_time_days"))
        if model_lead_time_days != evidenced_lead_time_days:
            item["lead_time_days"] = None
    return payload


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
    # A delivery-related word alone is not evidence for a model-generated value.
    # Accept it only when one unambiguous labelled value exists in the source.
    delivery_date_values = _labelled_date_values(
        document_text,
        DELIVERY_DATE_LABELS,
    )
    evidenced_delivery_date = (
        delivery_date_values[0] if len(delivery_date_values) == 1 else None
    )
    lead_time_values = _labelled_lead_time_days(document_text)
    evidenced_lead_time_days = (
        lead_time_values[0] if len(lead_time_values) == 1 else None
    )
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
        # _normalize_finetuned_quotation()과 동일한 이유로 null 값 규격 키를
        # 제거한다 - _ParsedItem.specifications가 None을 허용하지 않는다.
        raw_specifications = raw_item.get("specifications")
        specifications = (
            {key: value for key, value in raw_specifications.items() if value is not None}
            if isinstance(raw_specifications, dict) else {}
        )
        raw_description = raw_item.get("raw_description")
        item_name = raw_item.get("item_name")
        if not item_name or item_name == original_item_code:
            item_name = item_code or description or "품목명 미기재"
        unit = raw_item.get("unit")
        if str(unit or "").upper() in {"KRW", "USD", "EUR", "JPY", "CNY"}:
            unit = None
        raw_description_text = str(raw_description or "").strip()
        if (
            raw_description_text.lower() in {"", "none", "null", "n/a"}
            or re.fullmatch(r"(?i)(?:KRW|USD|EUR|JPY|CNY)?\s*[\d,.]+", raw_description_text)
        ):
            raw_description = description
        model_delivery_date = _normalize_date(
            raw_item.get("expected_delivery_date", raw_item.get("delivery_date"))
        )
        expected_delivery_date = (
            model_delivery_date
            if model_delivery_date == evidenced_delivery_date
            else None
        )
        normalized_lead_time_days = _normalize_lead_time_days(
            raw_item.get("lead_time_days")
        )
        lead_time_days = (
            normalized_lead_time_days
            if normalized_lead_time_days == evidenced_lead_time_days
            else None
        )
        items.append({
            "item_code": item_code,
            "item_name": item_name,
            "description": description,
            "quantity": quantity,
            "unit": unit,
            "unit_price": unit_price,
            "amount": amount,
            "expected_delivery_date": expected_delivery_date,
            "lead_time_days": lead_time_days,
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
        "currency": _normalize_currency(payload.get("currency")),
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "items": items,
        "notes": payload.get("notes"),
    }


def _normalize_finetuned_quotation(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the image-to-JSON adapter output without recalculating document values."""
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        specifications = raw_item.get("specifications")
        # 모델이 "이 규격은 문서에 없다"는 뜻으로 값을 null로 채워서 내는 경우가
        # 흔하다(예: {"material": null, "안전기준": "KOSHA인증"}). 근데
        # _ParsedItem/QuotationItem.specifications는 dict[str, str|int|float]로
        # None을 허용하지 않아서, 그런 항목이 하나라도 있으면
        # "SQ registration failed ... 9 validation errors for _ParsedQuotation"
        # 처럼 통째로 검증 실패했다. null은 "값 없음"이니 키 자체를 빼는 게
        # 맞고, 그래야 이 모델의 타입과도 맞는다.
        specifications = (
            {key: value for key, value in specifications.items() if value is not None}
            if isinstance(specifications, dict) else {}
        )
        items.append({
            "item_code": raw_item.get("item_code"),
            "item_name": raw_item.get("item_name"),
            "description": raw_item.get("description"),
            "quantity": _normalize_decimal(raw_item.get("quantity")),
            "unit": raw_item.get("unit"),
            "unit_price": _normalize_decimal(raw_item.get("unit_price")),
            "amount": _normalize_decimal(raw_item.get("amount")),
            "expected_delivery_date": _normalize_date(
                raw_item.get("expected_delivery_date", raw_item.get("delivery_date"))
            ),
            "lead_time_days": raw_item.get("lead_time_days"),
            "specifications": specifications,
            "raw_description": raw_item.get("raw_description"),
        })
    subtotal = _normalize_decimal(payload.get("subtotal"))
    tax_amount = _normalize_decimal(payload.get("tax_amount"))
    total_amount = _normalize_decimal(payload.get("total_amount"))
    if tax_amount is None and subtotal is not None and total_amount is not None:
        subtotal_decimal = Decimal(str(subtotal))
        total_decimal = Decimal(str(total_amount))
        if total_decimal >= subtotal_decimal:
            tax_amount = total_decimal - subtotal_decimal

    return {
        "quotation_id": payload.get("quotation_id"),
        # The application supplies and overwrites the trusted supplier name.
        "supplier_name": None,
        "business_registration_no": payload.get("business_registration_no"),
        "quotation_date": _normalize_date(payload.get("quotation_date")),
        "valid_until": _normalize_date(payload.get("valid_until")),
        "currency": _normalize_currency(payload.get("currency")),
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "items": items,
        "notes": payload.get("notes"),
    }


class LocalHuggingFaceQuotationParser:
    """텍스트 모델과 파인튜닝 비전 adapter를 지연 로딩해 재사용한다."""

    content_sections_separated = True

    def __init__(
        self,
        text_model: str | None = None,
        vision_model: str | None = None,
        vision_adapter: str | None = None,
    ):
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
        self.vision_adapter_name = (
            vision_adapter
            or _project_model_setting("HF_QUOTATION_VISION_ADAPTER")
            or os.getenv("HF_QUOTATION_VISION_ADAPTER")
            or DEFAULT_VISION_ADAPTER
        )
        self.max_new_tokens = int(
            _project_model_setting("HF_QUOTATION_MAX_NEW_TOKENS")
            or os.getenv("HF_QUOTATION_MAX_NEW_TOKENS")
            or "2048"
        )
        self.vision_max_new_tokens = int(
            _project_model_setting("HF_QUOTATION_VISION_MAX_NEW_TOKENS")
            or os.getenv("HF_QUOTATION_VISION_MAX_NEW_TOKENS")
            or "1024"
        )
        self.vision_max_pixels = int(
            _project_model_setting("HF_QUOTATION_VISION_MAX_PIXELS")
            or os.getenv("HF_QUOTATION_VISION_MAX_PIXELS")
            or "802816"
        )
        self.vision_attn_implementation = (
            _project_model_setting("HF_QUOTATION_ATTN_IMPLEMENTATION")
            or os.getenv("HF_QUOTATION_ATTN_IMPLEMENTATION")
            or "sdpa"
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
            "HF_QUOTATION_TEXT_MODEL/HF_QUOTATION_VISION_MODEL/"
            "HF_QUOTATION_VISION_ADAPTER에 사내 로컬 경로를 지정하세요. "
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
        from transformers import AutoProcessor, BitsAndBytesConfig

        try:
            from transformers import AutoModelForMultimodalLM as AutoVisionModel
        except ImportError:  # pragma: no cover - 구버전 Transformers 호환
            from transformers import AutoModelForImageTextToText as AutoVisionModel

        _, device, dtype = self._runtime()
        try:
            self._vision_processor = AutoProcessor.from_pretrained(
                self.vision_model_name,
                local_files_only=True,
                trust_remote_code=False,
                max_pixels=self.vision_max_pixels,
            )
            model_kwargs: dict[str, Any] = {
                "local_files_only": True,
                "trust_remote_code": False,
                "dtype": dtype,
                "low_cpu_mem_usage": True,
                "attn_implementation": self.vision_attn_implementation,
            }
            if device == "cuda":
                model_kwargs.update({
                    "device_map": "auto",
                    "quantization_config": BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_quant_type="nf4",
                        bnb_4bit_use_double_quant=True,
                        bnb_4bit_compute_dtype=dtype,
                    ),
                })
            self._vision_model = AutoVisionModel.from_pretrained(
                self.vision_model_name,
                **model_kwargs,
            )
            if device != "cuda":
                self._vision_model = self._vision_model.to(device)
            from peft import PeftModel

            self._vision_model = PeftModel.from_pretrained(
                self._vision_model,
                self.vision_adapter_name,
                local_files_only=True,
            ).eval()
        except Exception as exc:
            model_label = f"{self.vision_model_name} + {self.vision_adapter_name}"
            raise self._local_model_error(model_label, exc) from exc
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
        try:
            outputs = self._vision_model.generate(
                **inputs,
                max_new_tokens=self.vision_max_new_tokens,
                do_sample=False,
            )
            generated = outputs[:, input_length:]
            return self._vision_processor.batch_decode(
                generated, skip_special_tokens=True
            )[0].strip()
        finally:
            image.close()

    def _extract_images(
        self,
        prepared: PreparedSource,
        reflection_errors: list[str],
    ) -> _ParsedQuotation:
        """Run the quotation-specific Qwen3.5 adapter directly on document images."""
        from PIL import Image

        self._load_vision_model()
        images = [
            Image.open(io.BytesIO(vision_input.data)).convert("RGB")
            for vision_input in prepared.vision_inputs
        ]
        correction = ""
        if reflection_errors:
            correction = (
                "\n\n[이전 검토에서 확인된 오류]\n"
                + "\n".join(f"- {error}" for error in reflection_errors)
                + "\n위 오류를 이미지와 다시 대조해 교정하세요."
            )
        messages = [
            {"role": "system", "content": FINETUNED_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    *({"type": "image", "image": image} for image in images),
                    {"type": "text", "text": FINETUNED_USER_PROMPT + correction},
                ],
            },
        ]
        try:
            prompt = self._vision_processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            inputs = self._vision_processor(
                text=[prompt],
                images=images,
                return_tensors="pt",
            )
            inputs = _move_inputs_to_device(inputs, self._device or "cpu")
            input_length = inputs["input_ids"].shape[-1]
            outputs = self._vision_model.generate(
                **inputs,
                max_new_tokens=self.vision_max_new_tokens,
                do_sample=False,
                use_cache=True,
            )
            generated = outputs[:, input_length:]
            decoded = self._vision_processor.batch_decode(
                generated,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )[0].strip()
            payload = _extract_json_object(decoded)
            return _ParsedQuotation.model_validate(
                _normalize_finetuned_quotation(payload)
            )
        finally:
            for image in images:
                image.close()

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
        if prepared.vision_inputs and prepared.kind in {SourceKind.IMAGE, SourceKind.PDF}:
            return self._extract_images(prepared, reflection_errors)

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
            "quotation_date에는 견적서 발행일자를, valid_until에는 견적 유효기간을 넣으세요. "
            "통화 기호와 천 단위 쉼표는 숫자에서 제거하고 DD-MM-YYYY 날짜는 YYYY-MM-DD로 변환하세요.\n"
            f"specifications에 사용할 수 있는 규격 키: {specification_keys}\n"
            f"이전 검토 오류(재추출 시 교정):\n{reflection}\n"
            "최상위 필수 키: quotation_id, business_registration_no, "
            "quotation_date, valid_until, currency, subtotal, tax_amount, total_amount, items, notes.\n"
            "각 items 원소의 필수 키: item_code, item_name, description, quantity, unit, "
            "unit_price, amount, specifications, raw_description. "
            "품목별 expected_delivery_date와 lead_time_days는 보조 필드이며 원문에 명시된 경우에만 넣으세요.\n"
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


def get_configured_parser() -> QuotationParser:
    """Resolve local/RunPod without leaking provider details into workflows."""

    from backend_logic2.integrations.quotation_extraction.factory import (
        get_configured_quotation_parser,
    )

    return get_configured_quotation_parser(get_local_parser)


def prepare_rfq_specifications(prepared: PreparedSource, requirements: dict[str, Any] | None) -> None:
    """Share prompt preparation between synchronous and durable extraction."""
    keys: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            specifications = value.get("specifications")
            if isinstance(specifications, dict):
                keys.update(str(key) for key in specifications if str(key).strip())
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    if requirements:
        collect(requirements)
        prepared.specification_keys = sorted(keys)


def _extract_prepared_quotation(
    prepared: PreparedSource,
    rfq_name: str,
    *,
    source_filename: str,
    source_path: str | None = None,
    message_id: str | None = None,
    content_type: str | None = None,
    supplier_name: str | None = None,
    supplier_id: str | None = None,
    quotation_id: str | None = None,
    fallback_quotation_id: str | None = None,
    attempt: int = 1,
    reflection_errors: list[str] | None = None,
    rfq_requirements: dict[str, Any] | None = None,
    model_parser: QuotationParser | None = None,
) -> Quotation:
    """준비된 외부 견적 입력을 공통 Quotation 모델로 변환한다."""
    rfq_name = str(rfq_name or "").strip()
    supplier_name = str(supplier_name or "").strip()
    if not rfq_name:
        raise ValueError("rfq_name은 필수 입력값입니다.")
    if not supplier_name:
        raise ValueError("supplier_name은 필수 입력값입니다.")

    if prepared.kind == SourceKind.PORTAL:
        raise ValueError("포털 Supplier Quotation은 외부 견적 추출 대상이 아닙니다.")
    prepare_rfq_specifications(prepared, rfq_requirements)

    parser = model_parser or get_configured_parser()
    parsed_value = parser(prepared, rfq_name, supplier_name, reflection_errors or [])
    parsed = parsed_value if isinstance(parsed_value, _ParsedQuotation) else _ParsedQuotation.model_validate(parsed_value)
    payload = parsed.model_dump()
    separated = bool(getattr(parser, "content_sections_separated", False))
    apply_document_fallbacks(
        payload, extract_document_fallbacks(prepared.text), include_notes=not separated,
    )
    payload["content_sections_separated"] = separated
    if not payload.get("currency"):
        raise ValueError(
            "견적서에서 결제 통화를 확인할 수 없어 자동 등록하지 않습니다."
        )
    parsed_quotation_id = str(parsed.quotation_id or "").strip()
    if parsed_quotation_id.casefold() == rfq_name.casefold():
        parsed_quotation_id = ""
    resolved_fallback_id = fallback_quotation_id or f"EXT-{Path(source_filename).stem}"
    payload["quotation_id"] = (
        str(quotation_id).strip()
        if quotation_id
        else (parsed_quotation_id or resolved_fallback_id)
    )
    payload["rfq_name"] = rfq_name
    payload["supplier_id"] = supplier_id
    payload["supplier_name"] = supplier_name
    payload["items"] = [QuotationItem.model_validate(item) for item in payload["items"]]
    payload["source"] = QuotationSource(
        kind=prepared.kind,
        filename=source_filename,
        path=source_path,
        message_id=message_id,
        content_type=content_type,
    )
    payload["extraction_attempt"] = attempt
    if isinstance(parser, LocalHuggingFaceQuotationParser):
        if prepared.vision_inputs and prepared.kind in {SourceKind.IMAGE, SourceKind.PDF}:
            model_label = (
                f"{parser.vision_model_name} + LoRA {parser.vision_adapter_name}"
            )
        else:
            model_label = parser.text_model_name
    elif callable(getattr(parser, "extraction_evidence", None)):
        model_label = None
    else:
        model_label = "주입 파서"
    if model_label:
        parser_evidence = [f"로컬 HF 모델: {model_label}", "외부 API 전송 없음"]
    else:
        parser_evidence = list(parser.extraction_evidence())
    payload["extraction_evidence"] = [*prepared.evidence, *parser_evidence]
    if not quotation_id and not parsed_quotation_id:
        payload["extraction_evidence"].append(
            f"문서에서 견적번호를 확인하지 못해 대체 번호 사용: {resolved_fallback_id}"
        )
    return Quotation.model_validate(payload)


def extract_quotation_bytes(
    data: bytes,
    filename: str,
    rfq_name: str,
    *,
    supplier_name: str | None = None,
    supplier_id: str | None = None,
    quotation_id: str | None = None,
    fallback_quotation_id: str | None = None,
    attempt: int = 1,
    reflection_errors: list[str] | None = None,
    rfq_requirements: dict[str, Any] | None = None,
    model_parser: QuotationParser | None = None,
    message_id: str | None = None,
    content_type: str | None = None,
) -> Quotation:
    """ERPNext 첨부파일 바이트를 로컬 파일 생성 없이 추출한다."""
    prepared = prepare_source_bytes(data, filename)
    return _extract_prepared_quotation(
        prepared,
        rfq_name,
        source_filename=filename,
        message_id=message_id,
        content_type=content_type,
        supplier_name=supplier_name,
        supplier_id=supplier_id,
        quotation_id=quotation_id,
        fallback_quotation_id=fallback_quotation_id,
        attempt=attempt,
        reflection_errors=reflection_errors,
        rfq_requirements=rfq_requirements,
        model_parser=model_parser,
    )
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
    """한 개 로컬 견적을 추출한다. parser 미지정 시 로컬 HF 모델만 사용한다."""
    path = Path(path)
    return _extract_prepared_quotation(
        prepare_source(path),
        rfq_name,
        source_filename=path.name,
        source_path=str(path.resolve()),
        supplier_name=supplier_name,
        supplier_id=supplier_id,
        quotation_id=quotation_id,
        attempt=attempt,
        reflection_errors=reflection_errors,
        rfq_requirements=rfq_requirements,
        model_parser=model_parser,
    )
