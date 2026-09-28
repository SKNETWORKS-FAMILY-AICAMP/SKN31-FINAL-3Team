from __future__ import annotations


# BiddingFlow Item Group assignment helpers.

# 모든 MR 조회/처리가 가능한 관리자
SUPER_ADMINS = {
    "administrator",
    "admin@example.com",
}


def normalize_user_id(value: str | None) -> str:
    """사용자 ID/이메일 비교용 정규화."""
    return str(value or "").strip().lower()


def get_category_manager(item_group: str | None) -> str | None:
    """Return the administrator-configured BiddingFlow manager for a group."""
    from backend_logic2.repositories.item_group_assignments import get_manager

    return get_manager(item_group)


def is_super_admin(user_id: str | None) -> bool:
    return normalize_user_id(user_id) in SUPER_ADMINS


def is_same_user(
    left: str | None,
    right: str | None,
) -> bool:
    if not left or not right:
        return False

    return normalize_user_id(left) == normalize_user_id(right)
