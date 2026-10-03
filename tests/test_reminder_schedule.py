from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend_logic2.policies.reminder_schedule import reminder_window_open
from backend_logic2.policies.schema import CompanyPolicy


def test_company_reminder_time_is_kst_and_opens_for_the_rest_of_the_day():
    assert not reminder_window_open(
        datetime(2026, 10, 3, 0, 59, tzinfo=timezone.utc), send_time="10:00"
    )
    assert reminder_window_open(
        datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc), send_time="10:00"
    )
    assert reminder_window_open(
        datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc), send_time="10:00"
    )


def test_policy_rejects_invalid_reminder_time():
    with pytest.raises(ValidationError):
        CompanyPolicy(rules={"reminder_send_time": "24:00"})
