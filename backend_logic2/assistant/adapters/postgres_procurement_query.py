"""Authorization-aware procurement read-model adapter.

The assistant consumes the canonical PostgreSQL projection and never imports an
ERPNext client.  ERP-specific freshness checks belong to a separate adapter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any

from backend_logic2.integrations.assignment_config import is_super_admin
from backend_logic2.repositories import cases as case_repository

from ..models import AssistantRecord, CaseQueryFilters, CaseQueryRecords
from ..stage_presenter import summarize_case
from ..query_routing import matches_waiting, business_today


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


def _search_values(row: dict[str, Any]) -> list[str]:
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    return [
        str(value or "")
        for value in (
            row.get("mr_name"),
            row.get("item_code"),
            row.get("item_name"),
            row.get("requester_id"),
            summary.get("item_code"),
            summary.get("item_name"),
            summary.get("requester"),
            summary.get("department"),
            summary.get("item_group"),
            summary.get("description"),
        )
    ]


def _text_values(row: dict[str, Any]) -> str:
    return " ".join(_search_values(row)).casefold()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _matches_keyword(row: dict[str, Any], keyword: str) -> bool:
    # Ignore presentation spacing within each field, not field boundaries.
    needle = _normalized(keyword)
    return any(needle in _normalized(value) for value in _search_values(row))


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
        exact = (filters.exact_reference or "").strip().casefold()
        keyword = (filters.keyword or "").strip().casefold()
        today = business_today()
        due_until = today + timedelta(days=filters.due_within_days or 0)
        matched: list[dict[str, Any]] = []
        total_count = 0
        has_more = False
        # Only canonical fields are filtered. User text is never interpolated
        # into SQL, and moving ERP-specific fields here remains unnecessary.
        def pages():
            # Do not claim "none" after silently searching only the first 200.
            # Bound work; an incomplete search becomes an explicit query error.
            for offset in range(0, 2000, 200):
                rows = case_repository.list_cases(
                    assigned_user_id=assigned_user_id,
                    include_closed=filters.include_closed,
                    status=filters.status, stage=filters.stage,
                    limit=200, offset=offset,
                )
                yield from rows
                if len(rows) < 200:
                    return
            raise RuntimeError("Assistant query coverage limit reached; narrow the filters")

        for row in pages():
            if exact and str(row.get("mr_name") or "").strip().casefold() != exact:
                continue
            if filters.status and str(row.get("status") or "").upper() != filters.status.upper():
                continue
            if filters.stage and str(row.get("stage") or "").upper() != filters.stage.upper():
                continue
            if filters.waiting_for and not matches_waiting(row, filters.waiting_for):
                continue
            if keyword and not _matches_keyword(row, keyword):
                continue
            if filters.has_attachments is not None and _has_attachments(row) != filters.has_attachments:
                continue
            if filters.due_within_days is not None:
                summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
                schedule_date = _as_date(summary.get("schedule_date"))
                if schedule_date is None or not (today <= schedule_date <= due_until):
                    continue
            total_count += 1
            if total_count <= filters.offset:
                continue
            if len(matched) < filters.limit:
                matched.append(row)
            else:
                has_more = True
            if has_more and not filters.count_requested:
                break

        return CaseQueryRecords(
            [self._record(row) for row in matched],
            total_count=total_count if filters.count_requested else None,
            has_more=has_more,
        )

    @staticmethod
    def _record(row: dict[str, Any]) -> AssistantRecord:
        view = summarize_case(row)
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        schedule_date = _as_date(summary.get("schedule_date"))
        updated_at = row.get("updated_at")
        return AssistantRecord(
            case_id=str(row.get("case_id") or ""),
            reference=view["reference"],
            item_name=view["item_name"],
            stage=view["stage"],
            stage_label=view["stage_label"],
            status=view["status"],
            status_label=view["status_label"],
            schedule_date=schedule_date.isoformat() if schedule_date else None,
            waiting_on=view["waiting_on"],
            next_action=view["next_action"],
            updated_at=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "") or None,
            target=view["target"],
        )
