"""자동 진행 정책값 - 자동화 v2의 3단계.

여기서 지키는 것은 하나로 요약된다. **모르면 꺼진다.**
진행 중인 케이스에 고정된 옛 정책에는 이 키들이 아예 없다. 그때 기본값이
'켜짐'이면 배포하는 순간 운영 중인 건들이 사람 없이 움직인다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend_logic2.policies.schema import CompanyPolicy, PurchasingRules


def _legacy_rules() -> dict:
    """자동화가 생기기 전에 저장된 정책. 자동 진행 키가 하나도 없다."""
    return {
        "urgent_lead_days": 7,
        "bidding_amount": 20_000_000,
        "pattern_min_orders": 3,
        "irregular_cv": 0.5,
        "cycle_overdue_multiplier": 1.5,
        "inactive_months": 12,
        "min_competing_suppliers": 3,
        "supplier_refresh_years": 3,
        "quotation_priority": "price_then_delivery",
    }


def test_automation_is_off_unless_someone_turns_it_on() -> None:
    assert PurchasingRules().automation_mode == "off"


def test_a_policy_saved_before_automation_existed_stays_off() -> None:
    """⚠️ 이게 깨지면 배포하는 순간 진행 중인 건들이 사람 없이 움직인다."""
    rules = PurchasingRules.model_validate(_legacy_rules())

    assert rules.automation_mode == "off"


def test_a_legacy_policy_still_loads_at_all() -> None:
    """extra='forbid'라 키가 하나만 어긋나도 정책 로드 전체가 죽는다."""
    policy = CompanyPolicy.model_validate({"rules": _legacy_rules(), "guidance": {}})

    assert policy.rules.min_competing_suppliers == 3


def test_single_bid_can_never_be_auto_selected_by_any_setting() -> None:
    """단독 응찰 자동 선정은 설정으로도 열리면 안 된다."""
    with pytest.raises(ValidationError):
        PurchasingRules(auto_selection_min_quotations=1)


def test_the_rfq_threshold_is_separate_from_the_search_threshold() -> None:
    """같은 값을 쓰면 한쪽을 조정할 때 다른 쪽이 조용히 따라 움직인다."""
    rules = PurchasingRules(min_competing_suppliers=5)

    assert rules.auto_rfq_min_existing_suppliers == 3


def test_the_trade_history_window_is_separate_from_the_pool_refresh_window() -> None:
    rules = PurchasingRules(supplier_refresh_years=10)

    assert rules.auto_known_supplier_years == 3


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("automation_mode", "maybe"),
        ("auto_rfq_deadline_days", 0),
        ("auto_rfq_min_existing_suppliers", 0),
        ("auto_selection_score_gap", -1),
        ("auto_selection_max_amount", 0),
        ("auto_known_supplier_years", 0),
    ],
)
def test_a_nonsensical_setting_is_refused(field: str, bad) -> None:
    with pytest.raises(ValidationError):
        PurchasingRules(**{field: bad})


def test_an_unknown_automation_key_is_refused() -> None:
    """오타 난 설정이 조용히 무시되면 켠 줄 알았는데 안 켜져 있다."""
    with pytest.raises(ValidationError):
        PurchasingRules(automation_modes="on")


def test_settings_survive_a_round_trip() -> None:
    """정책은 JSON으로 저장됐다가 다시 읽힌다."""
    saved = PurchasingRules(
        automation_mode="shadow",
        auto_rfq_deadline_days=7,
        auto_selection_score_gap=12.5,
    )

    restored = PurchasingRules.model_validate(saved.model_dump())

    assert restored.automation_mode == "shadow"
    assert restored.auto_rfq_deadline_days == 7
    assert restored.auto_selection_score_gap == 12.5
