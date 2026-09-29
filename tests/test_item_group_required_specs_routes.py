"""Exercise real routes without calling ERPNext, the database or a model."""

from urllib.parse import quote
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_service.dependencies import require_authenticated_user
from backend_logic2.api import procurement_routes as routes


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ERPNEXT_WEBHOOK_SECRET", "route-test-only")
    application = FastAPI()
    application.include_router(routes.router)
    application.include_router(routes.webhook_router)
    return application


@pytest.mark.parametrize("prefix", ["/api/procurement", "/api/webhooks/erpnext"])
@pytest.mark.parametrize("group", [
    "사무용품", "사무용품-용지-복사지/인쇄용지", "분류/중분류/세부 품목", "용지 & 문구+#",
])
@pytest.mark.parametrize("escape_slash", [False, True])
def test_group_name_is_preserved(app, prefix, group, escape_slash):
    app.dependency_overrides[require_authenticated_user] = lambda: {"erp_user_id": "test-user"}
    expected = {"item_group": group, "required_specs": ["크기", "재질"], "reason": "test"}
    encoded = quote(group, safe="" if escape_slash else "/")
    with patch.object(routes, "get_or_create_group_requirements", return_value=expected) as lookup:
        with TestClient(app) as client:
            response = client.get(
                f"{prefix}/item-groups/{encoded}/required-specs",
                headers={"X-ERPNext-Webhook-Secret": "route-test-only"},
            )
    assert response.status_code == 200
    assert response.json()["item_group"] == group
    assert response.json()["required_specs"] == expected["required_specs"]
    lookup.assert_called_once_with(group)


@pytest.mark.parametrize("prefix", ["/api/procurement", "/api/webhooks/erpnext"])
def test_slash_route_still_requires_authentication(app, prefix):
    with patch.object(routes, "get_or_create_group_requirements") as lookup:
        with TestClient(app) as client:
            response = client.get(f"{prefix}/item-groups/parent%2Fchild/required-specs")
    assert response.status_code == 401
    lookup.assert_not_called()


@pytest.mark.parametrize("prefix", ["/api/procurement", "/api/webhooks/erpnext"])
def test_path_still_requires_required_specs_suffix(app, prefix):
    with TestClient(app) as client:
        assert client.get(f"{prefix}/item-groups/parent/child/unrelated").status_code == 404
