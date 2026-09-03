from unittest.mock import Mock, patch

import pytest

from backend_logic2.integrations import erp_client


def _response(content=b"file-bytes", content_type="application/pdf"):
    response = Mock()
    response.status_code = 200
    response.content = content
    response.headers = {"Content-Type": content_type}
    return response


def test_public_mr_attachment_uses_same_site_file_url():
    document = {
        "name": "FILE-1",
        "file_name": "drawing.pdf",
        "file_url": "/files/drawing.pdf",
        "is_private": 0,
        "attached_to_doctype": "Material Request",
    }
    with patch.object(erp_client, "erp_get_one", return_value=document), patch.object(
        erp_client.requests, "get", return_value=_response()
    ) as request:
        result = erp_client.erp_download_file(
            "FILE-1", expected_attached_to_doctype="Material Request"
        )

    assert result["content"] == b"file-bytes"
    assert request.call_args.args[0] == f"{erp_client.SITE_URL.rstrip('/')}/files/drawing.pdf"


def test_private_mr_attachment_uses_frappe_download_method():
    document = {
        "name": "FILE-2",
        "file_name": "secret.pdf",
        "file_url": "/private/files/secret.pdf",
        "is_private": 1,
        "attached_to_doctype": "Material Request",
    }
    with patch.object(erp_client, "erp_get_one", return_value=document), patch.object(
        erp_client.requests, "get", return_value=_response()
    ) as request:
        erp_client.erp_download_file(
            "FILE-2", expected_attached_to_doctype="Material Request"
        )

    assert request.call_args.args[0].endswith(
        "/api/method/frappe.utils.file_manager.download_file"
    )
    assert request.call_args.kwargs["params"] == {
        "file_url": "/private/files/secret.pdf"
    }


def test_attachment_download_rejects_other_doctype_before_file_request():
    document = {
        "name": "FILE-3",
        "file_name": "invoice.pdf",
        "file_url": "/private/files/invoice.pdf",
        "is_private": 1,
        "attached_to_doctype": "Purchase Invoice",
    }
    with patch.object(erp_client, "erp_get_one", return_value=document), patch.object(
        erp_client.requests, "get"
    ) as request:
        with pytest.raises(PermissionError):
            erp_client.erp_download_file(
                "FILE-3", expected_attached_to_doctype="Material Request"
            )

    request.assert_not_called()


def test_attachment_download_rejects_external_host():
    document = {
        "name": "FILE-4",
        "file_name": "external.pdf",
        "file_url": "https://malicious.example/external.pdf",
        "is_private": 0,
        "attached_to_doctype": "Material Request",
    }
    with patch.object(erp_client, "erp_get_one", return_value=document):
        with pytest.raises(erp_client.ERPNextAPIError):
            erp_client.erp_download_file(
                "FILE-4", expected_attached_to_doctype="Material Request"
            )
