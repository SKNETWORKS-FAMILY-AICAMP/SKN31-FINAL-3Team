from __future__ import annotations

import io
from decimal import Decimal
from email.message import EmailMessage
from unittest.mock import Mock

from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
    DEFAULT_VISION_ADAPTER,
    DEFAULT_VISION_MODEL,
    FINETUNED_USER_PROMPT,
    LocalHuggingFaceQuotationParser,
    PreparedSource,
    SourceKind,
    VisionInput,
    _ParsedQuotation,
    _normalize_finetuned_quotation,
    classify_source,
    extract_quotation,
    extract_quotation_bytes,
    prepare_source,
)


def _parsed_quotation() -> _ParsedQuotation:
    return _ParsedQuotation.model_validate(
        {
            "quotation_id": "EST-001",
            "currency": "KRW",
            "subtotal": 1000,
            "tax_amount": 100,
            "total_amount": 1100,
            "items": [
                {
                    "item_name": "테스트 품목",
                    "quantity": 1,
                    "unit_price": 1000,
                    "amount": 1000,
                }
            ],
        }
    )


def test_finetuned_defaults_match_uploaded_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend_logic2.nodes.quotation.quotation_filter.quotation_extractor._project_model_setting",
        lambda _name: None,
    )
    for name in (
        "HF_QUOTATION_VISION_MODEL",
        "HF_QUOTATION_VISION_ADAPTER",
        "HF_QUOTATION_VISION_MAX_PIXELS",
        "HF_QUOTATION_VISION_MAX_NEW_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)

    parser = LocalHuggingFaceQuotationParser()

    assert parser.vision_model_name == DEFAULT_VISION_MODEL == "Qwen/Qwen3.5-9B"
    assert parser.vision_adapter_name == DEFAULT_VISION_ADAPTER
    assert parser.vision_max_pixels == 802816
    assert parser.vision_max_new_tokens == 512
    assert "expected_delivery_date" in FINETUNED_USER_PROMPT


def test_image_source_uses_direct_finetuned_extraction() -> None:
    parser = LocalHuggingFaceQuotationParser()
    expected = _parsed_quotation()
    parser._extract_images = Mock(return_value=expected)
    parser._structure_text = Mock(side_effect=AssertionError("text path must not run"))
    prepared = PreparedSource(
        kind=SourceKind.IMAGE,
        text="[견적서 이미지]",
        vision_inputs=[VisionInput(data=b"image", filename="quote.png")],
    )

    actual = parser(prepared, "RFQ-1", "공급사", [])

    assert actual is expected
    parser._extract_images.assert_called_once_with(prepared, [])


def test_text_source_keeps_existing_text_model_path() -> None:
    parser = LocalHuggingFaceQuotationParser()
    expected = _parsed_quotation()
    parser._structure_text = Mock(return_value=expected)
    parser._extract_images = Mock(side_effect=AssertionError("vision path must not run"))
    prepared = PreparedSource(kind=SourceKind.TEXT, text="견적 원문")

    actual = parser(prepared, "RFQ-1", "공급사", [])

    assert actual is expected
    parser._structure_text.assert_called_once()


def test_docx_source_preserves_paragraphs_and_table_structure(tmp_path) -> None:
    from docx import Document

    docx_path = tmp_path / "quote.docx"
    document = Document()
    document.add_paragraph("공급자: 테스트상사")
    table = document.add_table(rows=2, cols=3)
    table.rows[0].cells[0].text = "품목명"
    table.rows[0].cells[1].text = "수량"
    table.rows[0].cells[2].text = "단가"
    table.rows[1].cells[0].text = "시약 A"
    table.rows[1].cells[1].text = "2"
    table.rows[1].cells[2].text = "1,000"
    document.save(docx_path)

    prepared = prepare_source(docx_path)

    assert classify_source(docx_path) == SourceKind.DOCX
    assert prepared.kind == SourceKind.DOCX
    assert prepared.vision_inputs == []
    assert "[본문]\n공급자: 테스트상사" in prepared.text
    assert "품목명 | 수량 | 단가" in prepared.text
    assert "시약 A | 2 | 1,000" in prepared.text
    assert any("OCR 미사용" in item for item in prepared.evidence)


def test_email_source_extracts_attached_docx_without_ocr(tmp_path) -> None:
    from docx import Document

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph("공급자: 첨부상사")
    document.save(buffer)

    message = EmailMessage()
    message["From"] = "vendor@example.com"
    message["To"] = "buyer@example.com"
    message["Subject"] = "견적서 송부"
    message.set_content("첨부 견적서를 확인해 주세요.")
    message.add_attachment(
        buffer.getvalue(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="quote.docx",
    )
    email_path = tmp_path / "quote.eml"
    email_path.write_bytes(message.as_bytes())

    prepared = prepare_source(email_path)

    assert prepared.kind == SourceKind.EMAIL
    assert "[attachment: quote.docx]" in prepared.text
    assert "공급자: 첨부상사" in prepared.text
    assert any("OCR 미사용" in item for item in prepared.evidence)


def test_finetuned_output_normalizes_adapter_schema_without_recalculation() -> None:
    normalized = _normalize_finetuned_quotation(
        {
            "quotation_id": "EST-001",
            "supplier_name": "모델이 읽은 공급사",
            "currency": None,
            "subtotal": "1,000",
            "tax_amount": "100",
            # Deliberately inconsistent: image values must be preserved.
            "total_amount": "1,500",
            "items": [
                {
                    "item_code": "ITEM-1",
                    "item_name": "테스트 품목",
                    "quantity": "1",
                    "unit_price": "1,000",
                    "amount": "1,000",
                    "expected_delivery_date": "2026.09.20",
                }
            ],
        }
    )
    parsed = _ParsedQuotation.model_validate(normalized)

    assert parsed.supplier_name is None
    assert parsed.currency == "KRW"
    assert parsed.total_amount == Decimal("1500")
    assert parsed.items[0].expected_delivery_date.isoformat() == "2026-09-20"


def test_expected_delivery_date_reaches_public_quotation_model(tmp_path) -> None:
    image_path = tmp_path / "quote.png"
    image_path.write_bytes(b"test image bytes")

    def injected_parser(*_args):
        return {
            "quotation_id": "EST-001",
            "currency": "KRW",
            "subtotal": 1000,
            "tax_amount": 100,
            "total_amount": 1100,
            "items": [
                {
                    "item_name": "테스트 품목",
                    "quantity": 1,
                    "unit_price": 1000,
                    "amount": 1000,
                    "expected_delivery_date": "2026-09-20",
                }
            ],
        }

    quotation = extract_quotation(
        image_path,
        "RFQ-001",
        supplier_name="공급사",
        model_parser=injected_parser,
    )

    assert quotation.items[0].expected_delivery_date.isoformat() == "2026-09-20"


def test_erp_attachment_bytes_are_extracted_without_local_path() -> None:
    def injected_parser(*_args):
        return {
            "quotation_id": None,
            "currency": "KRW",
            "subtotal": 1000,
            "tax_amount": 100,
            "total_amount": 1100,
            "items": [{
                "item_name": "테스트 품목",
                "quantity": 1,
                "unit_price": 1000,
                "amount": 1000,
            }],
        }

    quotation = extract_quotation_bytes(
        b"not-decoded-by-injected-parser",
        "quotation.jpg",
        "RFQ-001",
        supplier_name="공급사",
        supplier_id="SUP-001",
        fallback_quotation_id="EMAIL-COMM-1-FILE-1",
        message_id="COMM-1",
        content_type="image/jpeg",
        model_parser=injected_parser,
    )

    assert quotation.quotation_id == "EMAIL-COMM-1-FILE-1"
    assert quotation.source.path is None
    assert quotation.source.message_id == "COMM-1"
    assert quotation.source.content_type == "image/jpeg"
