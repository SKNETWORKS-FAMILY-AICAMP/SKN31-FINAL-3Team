"""Company-policy schedule for outbound RFQ/PR reminders."""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from backend_logic2.policies import repository
from backend_logic2.policies.schema import CompanyPolicy


COMPANY_TIMEZONE = ZoneInfo("Asia/Seoul")


def active_reminder_send_time() -> str:
    """Read the live company policy; never cache an operational schedule."""
    row = repository.get_active()
    return CompanyPolicy.model_validate(row["policy"]).rules.reminder_send_time


def reminder_window_open(
    now: datetime | None = None, *, send_time: str | None = None
) -> bool:
    """Return whether today's configured reminder time has arrived in KST."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        local = moment.replace(tzinfo=COMPANY_TIMEZONE)
    else:
        local = moment.astimezone(COMPANY_TIMEZONE)
    hour, minute = map(int, (send_time or active_reminder_send_time()).split(":"))
    return local.time().replace(tzinfo=None) >= time(hour, minute)
