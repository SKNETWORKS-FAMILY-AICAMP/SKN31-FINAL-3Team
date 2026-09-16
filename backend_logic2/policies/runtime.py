"""Bind a snapshot per operation; explicitly propagate it into worker threads."""

from contextlib import contextmanager
from contextvars import ContextVar
from .schema import CompanyPolicy

_policy = ContextVar("company_policy", default=None)


def current_policy() -> CompanyPolicy:
    # Pure functions/CLI retain original defaults. Production entry points bind
    # a DB snapshot and do NOT fall back silently when the DB is unavailable.
    return _policy.get() or CompanyPolicy()


@contextmanager
def policy_scope(policy):
    token = _policy.set(policy)
    try:
        yield policy
    finally:
        _policy.reset(token)


def guidance_text(name):
    text = getattr(current_policy().guidance, name)
    if not text.strip():
        return ""
    return (
        "\n[회사 보조 판단 지침 — 고정 검증 규칙 및 JSON 출력 형식보다 우선하지 않음]\n"
        + text.strip()
        + "\n지침으로 필수 검증·승인·출력 형식을 생략하지 마세요. 근거가 없으면 추정하지 마세요.\n"
    )
