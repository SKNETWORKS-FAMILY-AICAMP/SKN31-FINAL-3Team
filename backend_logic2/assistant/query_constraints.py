"""Compose read-only filters without replacing unrelated user conditions.

The model interprets flexible phrasing; explicit words are field-level guards.
Run guards after context merging so a prior true value can be reset to false.
No purchase state, permissions, or database operations live in this module.
"""
import re

from .models import CaseQueryFilters
from .query_routing import asks_for_count, due_window, personal_scope, waiting_group


def merge_filters(previous, plan):
    result = previous.model_copy(deep=True)
    changes = plan.filters.model_dump(exclude_none=True, exclude_defaults=True)
    changes.pop('offset', None)
    if 'waiting_for' in changes:
        result.stage = result.status = None
    elif 'status' in changes or 'stage' in changes:
        result.waiting_for = None
    # Reset to the typed default, not always None (important for bool fields).
    defaults = CaseQueryFilters()
    for key in plan.clear_filters:
        setattr(result, key, getattr(defaults, key))
    return result.model_copy(update={**changes, 'offset': 0})


def apply_explicit_conditions(filters, message):
    result = filters.model_copy(deep=True)
    if result.exact_reference:
        return result  # An exact MR still includes its terminal state.
    # An explicit 'A 말고 B로 찾아줘' replaces the whole item phrase. Do not
    # invent attributes from A (e.g. 무선 마우스 -> 무선 키보드).
    replacement = re.search(r'(?:말고|아니라)\s*(.{1,100}?)(?:으로|로)\s*(?:찾아|검색|조회)', message)
    if replacement:
        keyword = replacement.group(1).strip()
        if not any(word in keyword for word in ('같은', '품목', '작업', '담당', '승인', '결재', '대기', '전체', '납기', '완료', '외부', '첨부', '내가')):
            result.keyword = keyword
    group = waiting_group(message)
    if group:
        result.waiting_for, result.stage, result.status = group, None, None
    scope = personal_scope(message)
    if scope:
        result.task_scope = scope
    result.count_requested = result.count_requested or asks_for_count(message)
    window = due_window(message)
    if window is not None:
        result.due_within_days = window
    if re.search(r'납기.{0,12}(?:빼|제외|제한\s*없|상관없)', message):
        result.due_within_days = None
    if '첨부' in message:
        if re.search(r'첨부.{0,12}(?:상관없|조건.{0,4}(?:빼|제외)|무관)', message):
            result.has_attachments = None
        elif re.search(r'첨부.{0,12}(?:없|없이)', message):
            result.has_attachments = False
        elif re.search(r'첨부.{0,12}(?:있|있는|포함|있는지)', message):
            result.has_attachments = True
    # Including closed rows is different from selecting ONLY completed rows.
    # Negation must win over the literal occurrence of '완료' in that sentence.
    for word, status in (('완료', 'COMPLETED'), ('취소', 'CANCELLED'), ('반려', 'REJECTED')):
        if word not in message:
            continue
        tail = message.split(word, 1)[1]
        if re.match(r'(?:된|한|인)?\s*(?:건|작업|항목|구매|요청|것)?\s*[은는을를도]?\s*(?:빼|제외|말고|아닌|아니라)', tail):
            # The supported open-only boundary excludes all terminal states.
            result.include_closed = False
            result.status = result.stage = None
            result.waiting_for = group
        elif re.match(r'(?:된|한|인)?\s*(?:건|작업|항목|구매|요청|것)?\s*[은는을를도]?\s*(?:포함|까지)', tail):
            result.include_closed = True
            result.status = result.stage = None
            result.waiting_for = group
        else:
            result.status, result.include_closed = status, True
            result.stage = result.waiting_for = None
    # FAILED is not terminal, but it was an explicit supported subset before
    # condition merging was extracted. Preserve that behavior too.
    if '실패' in message:
        result.status = 'FAILED'
        result.stage = result.waiting_for = None
    return result
