"""포털 견적의 비고를 규격/특약으로 나누는 단계.

왜 생겼는가: 공급사가 포털에서 직접 쓴 SQ의 terms는 자유 텍스트라 통째로
notes가 됐고, 규격 평가 AI가 특약 문장까지 규격 근거로 읽었다. 실제로
"3.2kW로 하게 될 경우 15,000원" 같은 **조건부 대안 제안** 하나 때문에
정격 출력이 불일치 처리돼 규격 0점이 나왔다.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from backend_logic2.nodes.quotation.quotation_filter.quotation_models import (
    Quotation,
    QuotationItem,
    QuotationReview,
    QuotationSource,
    ReviewStatus,
    SourceKind,
)
from backend_logic2.nodes.quotation.quotation_filter import quotation_ranker
from backend_logic2.nodes.quotation.quotation_filter.quotation_spec_evaluator import (
    _quotation_payload,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_terms_splitter import (
    QuotationTermsSplit,
    apply_split,
    split_fingerprint,
    split_payload,
)
from test_quotation_luna_ranking import _rfq


NOTES = "정격 출력 3.2kW로 하게 될 경우 15,000원으로 납품 가능합니다!"


def _portal_quotation(notes: str | None = NOTES, *, separated: bool = False) -> Quotation:
    return Quotation(
        quotation_id="SQ-1",
        rfq_name="RFQ-1",
        supplier_id="세희컴퍼니",
        supplier_name="세희컴퍼니",
        currency="KRW",
        subtotal=Decimal("2500000"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("2500000"),
        items=[QuotationItem(
            item_code="ITEM-1",
            item_name="모터 2.2kW",
            quantity=Decimal("1"),
            unit_price=Decimal("2500000"),
            amount=Decimal("2500000"),
            specifications={"정격 출력": "2.2kW"},
        )],
        notes=notes,
        content_sections_separated=separated,
        source=QuotationSource(kind=SourceKind.TEXT, filename="portal"),
    )


def _review(quotation: Quotation) -> QuotationReview:
    return QuotationReview(
        quotation=quotation,
        quotation_id=quotation.quotation_id,
        supplier_name=quotation.supplier_name,
        source_kind=SourceKind.TEXT,
        status=ReviewStatus.ACCEPTED,
        valid=True,
        specification_compliant=True,
        issues=[],
        item_compliance=[],
    )


def test_conditional_offer_stays_out_of_the_specifications():
    """조건부 대안은 이번 납품값이 아니다. 규격은 견적 본문 값 그대로."""
    quotation = _portal_quotation()
    apply_split(quotation, QuotationTermsSplit(items=[], terms=NOTES))
    assert quotation.items[0].specifications == {"정격 출력": "2.2kW"}
    assert quotation.notes == NOTES
    assert quotation.content_sections_separated is True


def test_a_stated_substitution_becomes_the_quoted_specification():
    """납품값을 단정하면 그게 실제 견적값이다."""
    quotation = _portal_quotation("실제 납품은 3.2kW입니다.")
    apply_split(quotation, QuotationTermsSplit.model_validate(
        {"items": [{"index": 1, "specifications": {"정격 출력": "3.2kW"}}], "terms": ""}
    ))
    assert quotation.items[0].specifications["정격 출력"] == "3.2kW"
    assert quotation.notes is None


def test_the_spec_evaluator_no_longer_sees_the_terms_once_split():
    """분리 뒤에는 이메일 경로와 같아진다 - 규격 AI에 특약이 안 들어간다."""
    quotation = _portal_quotation()
    before = _quotation_payload(_rfq(), quotation)["quotations"][0]
    assert before["notes"] == NOTES

    apply_split(quotation, QuotationTermsSplit(items=[], terms=NOTES))
    after = _quotation_payload(_rfq(), quotation)["quotations"][0]
    assert after["notes"] is None
    assert after["notes_unit_normalizations"] == []


def test_the_fingerprint_tracks_the_notes_and_the_prompt():
    quotation = _portal_quotation()
    original = split_fingerprint(quotation, "qwen")
    assert original == split_fingerprint(_portal_quotation(), "qwen")
    assert original != split_fingerprint(_portal_quotation("다른 특약"), "qwen")
    assert original != split_fingerprint(quotation, "luna")


def test_a_new_split_prompt_invalidates_the_cached_splits():
    """프롬프트를 고치면 옛 프롬프트로 나눈 결과를 다시 쓰면 안 된다."""
    quotation = _portal_quotation()
    original = split_fingerprint(quotation, "qwen")
    with patch("backend_logic2.nodes.quotation.quotation_filter"
               ".quotation_terms_splitter.SPLIT_PROMPT_VERSION", "terms-split-v2"):
        assert split_fingerprint(quotation, "qwen") != original


def test_only_the_notes_and_item_names_are_sent():
    """분류에 필요 없는 금액·공급사는 모델에 보내지 않는다."""
    payload = split_payload(_portal_quotation())
    assert payload == {"items": [{"index": 1, "item_name": "모터 2.2kW"}], "terms_text": NOTES}


class _Splitter:
    model_name = "qwen"
    available = True

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def split(self, quotation):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result or QuotationTermsSplit(items=[], terms=NOTES)


def _run_split(reviews, splitter, cached=None, saved=None):
    saved = [] if saved is None else saved
    with patch.object(quotation_ranker, "LOGGER"), \
         patch("backend_logic2.nodes.quotation.quotation_filter"
               ".quotation_terms_splitter.build_quotation_terms_splitter",
               return_value=splitter), \
         patch("backend_logic2.repositories.quotation_specification_cache.load_matching",
               return_value=cached or {}), \
         patch("backend_logic2.repositories.quotation_specification_cache.save_assessments",
               side_effect=lambda *args: saved.append(args)):
        quotation_ranker._split_portal_terms(reviews, object(), "RFQ-1")


def test_email_quotations_are_left_alone():
    """이메일 경로는 추출 단계에서 이미 나뉘어 저장된다. 다시 부르지 않는다."""
    splitter = _Splitter()
    quotation = _portal_quotation(separated=True)
    _run_split([_review(quotation)], splitter)
    assert splitter.calls == 0


def test_a_quotation_without_notes_is_not_sent():
    splitter = _Splitter()
    _run_split([_review(_portal_quotation(None))], splitter)
    assert splitter.calls == 0


def test_a_cached_split_is_reused_without_calling_the_model():
    """'자동 진행 판정'을 누를 때마다 RunPod을 다시 부르면 안 된다."""
    splitter = _Splitter()
    quotation = _portal_quotation()
    cached = {"SQ-1": {"items": [], "terms": NOTES}}
    _run_split([_review(quotation)], splitter, cached=cached)
    assert splitter.calls == 0
    assert quotation.content_sections_separated is True
    assert quotation.notes == NOTES


def test_a_fresh_split_is_written_to_the_cache():
    splitter = _Splitter()
    saved: list = []
    _run_split([_review(_portal_quotation())], splitter, saved=saved)
    assert splitter.calls == 1
    rfq_name, fingerprints, source, fresh = saved[0]
    assert rfq_name == "RFQ-1"
    assert source == "qwen:terms-split"
    assert set(fresh) == {"SQ-1"} and set(fingerprints) == {"SQ-1"}


def test_a_split_failure_leaves_the_quotation_untouched_instead_of_dropping_it():
    """한 건이 실패해도 순위 전체가 사라지면 안 된다."""
    splitter = _Splitter(error=RuntimeError("RunPod 타임아웃"))
    quotation = _portal_quotation()
    _run_split([_review(quotation)], splitter)
    assert quotation.notes == NOTES
    assert quotation.content_sections_separated is False


def test_an_unavailable_splitter_is_skipped():
    splitter = _Splitter()
    splitter.available = False
    quotation = _portal_quotation()
    _run_split([_review(quotation)], splitter)
    assert splitter.calls == 0
    assert quotation.content_sections_separated is False
