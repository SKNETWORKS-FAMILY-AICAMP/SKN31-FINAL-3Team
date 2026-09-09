"""Authorization-aware procurement read-model adapter.

The assistant consumes the canonical PostgreSQL projection and never imports an
ERPNext client.  ERP-specific freshness checks belong to a separate adapter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from backend_logic2.integrations.assignment_config import is_super_admin
from backend_logic2.repositories import cases as case_repository

from ..models import AssistantRecord, CaseQueryFilters
from ..stage_presenter import summarize_case


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text_values(row: dict[str, Any]) -> str:
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            row.get("mr_name"),
            summary.get("item_code"),
            summary.get("item_name"),
            summary.get("requester"),
            summary.get("department"),
            summary.get("item_group"),
            summary.get("description"),
        )
    ).casefold()


def _has_attachments(row: dict[str, Any]) -> bool:
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    attachments = summary.get("attachments") or row.get("attachments") or []
    if isinstance(attachments, (list, tuple, dict)):
        return bool(attachments)
    try:
        return Decimal(str(attachments)) > 0
    except (InvalidOperation, ValueError):
        return bool(attachments)


class PostgresProcurementQuery:
    def query_cases(
        self,
        filters: CaseQueryFilters,
        *,
        actor: str,
    ) -> list[AssistantRecord]:
        # Administrators may inspect all cases; ordinary buyers are restricted
        # to the assignment already projected by the procurement backend.
        assigned_user_id = None if is_super_admin(actor) else actor
        rows = case_repository.list_cases(
            assigned_user_id=assigned_user_id,
            include_closed=filters.include_closed,
            limit=200,
            offset=0,
        )
        exact = (filters.exact_reference or "").strip().casefold()
        keyword = (filters.keyword or "").strip().casefold()
        today = date.today()
        due_until = today + timedelta(days=filters.due_within_days or 0)
        matched: list[dict[str, Any]] = []
        # Only canonical fields are filtered. User text is never interpolated
        # into SQL, and moving ERP-specific fields here remains unnecessary.
        for row in rows:
            if exact and str(row.get("mr_name") or "").strip().casefold() != exact:
                continue
            if filters.status and str(row.get("status") or "").upper() != filters.status.upper():
                continue
            if filters.stage and str(row.get("stage") or "").upper() != filters.stage.upper():
                continue
            if keyword and keyword not in _text_values(row):
                continue
            if filters.has_attachments is not None and _has_attachments(row) != filters.has_attachments:
                continue
            if filters.due_within_days is not None:
                summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
                schedule_date = _as_date(summary.get("schedule_date"))
                if schedule_date is None or not (today <= schedule_date <= due_until):
                    continue
            matched.append(row)

        return [self._record(row) for row in matched[: filters.limit]]

    @staticmethod
    def _record(row: dict[str, Any]) -> AssistantRecord:
        view = summarize_case(row)
        updated_at = row.get("updated_at")
        return AssistantRecord(
            case_id=str(row.get("case_id") or ""),
            reference=view["reference"],
            item_name=view["item_name"],
            stage=view["stage"],
            stage_label=view["stage_label"],
            status=view["status"],
            status_label=view["status_label"],
            waiting_on=view["waiting_on"],
            next_action=view["next_action"],
            updated_at=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "") or None,
            target=view["target"],
        )
