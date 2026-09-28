from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend_logic2.api import policy_routes, procurement_routes
from backend_logic2.integrations import assignment_config


def test_category_manager_uses_database_assignment_not_code_map():
    assert not hasattr(assignment_config, "CATEGORY_MANAGER_MAP")
    with patch(
        "backend_logic2.repositories.item_group_assignments.get_manager",
        return_value="buyer@example.com",
    ) as get_manager:
        assert assignment_config.get_category_manager("Safety") == "buyer@example.com"
    get_manager.assert_called_once_with("Safety")


def test_non_admin_cannot_disable_assigned_case_filter():
    with (
        patch.object(procurement_routes, "_is_global_case_admin", return_value=False),
        patch.object(procurement_routes.case_repository, "list_cases", return_value=[]) as list_cases,
        patch("backend_logic2.services.supplier_recommendations.attach_supplier_recommendations"),
    ):
        response = procurement_routes.get_cases(
            current_user={"erp_user_id": "buyer@example.com"},
            assigned_to_me=False,
            include_closed=True,
            limit=200,
            offset=0,
        )

    assert list_cases.call_args.kwargs["assigned_user_id"] == "buyer@example.com"
    assert response["assigned_to_me"] is True


def test_case_detail_denies_a_different_group_manager():
    with (
        patch.object(
            procurement_routes.case_repository,
            "get_case",
            return_value={"case_id": "case-1", "assigned_user_id": "other@example.com"},
        ),
        patch.object(procurement_routes, "_is_global_case_admin", return_value=False),
    ):
        with pytest.raises(HTTPException) as error:
            procurement_routes.get_case("case-1", {"erp_user_id": "buyer@example.com"})

    assert error.value.status_code == 403


def test_task_list_is_scoped_to_the_current_manager():
    with (
        patch.object(procurement_routes, "_is_global_case_admin", return_value=False),
        patch.object(procurement_routes.task_repository, "list_tasks", return_value=[]) as list_tasks,
    ):
        procurement_routes.get_tasks(
            {"erp_user_id": "buyer@example.com"}, case_id=None, task_status="PENDING"
        )

    assert list_tasks.call_args.kwargs["assigned_user_id"] == "buyer@example.com"


def test_policy_manager_role_has_global_case_access():
    with (
        patch.object(procurement_routes, "is_super_admin", return_value=False),
        patch.object(
            procurement_routes,
            "read_policy_access",
            return_value={"can_manage": True},
        ),
    ):
        assert procurement_routes._is_global_case_admin("policy-manager@example.com")


def test_assignment_save_rejects_groups_not_in_erp():
    body = policy_routes.ItemGroupAssignmentsCommand(
        expected_revision=1,
        assignments=[{"item_group": "Unknown", "manager_user_id": "buyer@example.com"}],
    )
    with (
        patch.object(policy_routes, "_require_admin", return_value="admin@example.com"),
        patch.object(
            policy_routes,
            "_load_item_group_assignment_options",
            return_value=({"Safety"}, {"buyer@example.com"}, []),
        ),
        patch.object(policy_routes.item_group_assignments, "replace_assignments") as replace,
    ):
        with pytest.raises(HTTPException) as error:
            policy_routes.save_category_assignments(body, {})

    assert error.value.status_code == 422
    replace.assert_not_called()