from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    QuotationArithmeticValidationError,
    SupplierQuotationRegistrationError,
)
from backend_logic2.services.quotation_service import (
    classify_intake_failure,
    record_intake_failure,
)


def test_intake_failures_are_classified_for_the_red_notice() -> None:
    assert classify_intake_failure(ValueError("JSON 없음")) == "parse"
    assert classify_intake_failure(QuotationArithmeticValidationError("합계 불일치")) == "arithmetic"
    assert classify_intake_failure(TimeoutError("RunPod timeout")) == "extraction"
    # 중복 제출 같은 등록 규칙 위반은 '읽기 실패'가 아니므로 기록하지 않는다.
    assert classify_intake_failure(SupplierQuotationRegistrationError("중복")) is None


def test_recording_failure_never_breaks_the_caller(monkeypatch) -> None:
    calls = []

    def broken_record(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "backend_logic2.repositories.quotation_intake_failures.record_failure",
        broken_record,
    )
    record_intake_failure(
        ValueError("bad json"),
        rfq_name="RFQ-1",
        supplier_id="SUP-1",
        supplier_name="SUP-1",
        source_filename="quote.pdf",
        file_id="FILE-1",
        communication_name="COMM-1",
    )
    assert calls and calls[0]["failure_kind"] == "parse"
