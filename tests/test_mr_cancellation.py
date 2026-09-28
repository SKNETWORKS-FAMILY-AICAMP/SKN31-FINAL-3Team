from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock

import pytest

from backend_logic2.services import mr_cancellation as service


@pytest.fixture
def scenario(monkeypatch):
    case = {"mr_name": "MR-1", "status": "WAITING_INPUT", "workflow_snapshot": {"values": {
        "rfq_name": "R2", "rfq_rounds": [{"rfq_name": "R1"}, {"rfq_name": "R2"}],
    }}}
    documents = {
        "R1": {"name": "R1", "docstatus": 1, "items": [{"material_request": "MR-1"}]},
        "R2": {"name": "R2", "docstatus": 0, "items": [{"material_request": "MR-1"}]},
        "R3": {"name": "R3", "docstatus": 1, "items": [{"material_request": "MR-1"}]},
        "S1": {"name": "S1", "docstatus": 1, "items": [{"request_for_quotation": "R1"}]},
    }
    def listing(doctype, **kwargs):
        return [{"name": name} for name in ({"Request for Quotation": ["R3"], "Supplier Quotation": ["S1"]}.get(doctype, []))]
    writes = []
    monkeypatch.setattr(service, "erp_get", listing)
    monkeypatch.setattr(service, "erp_get_one", lambda doctype, name: deepcopy(documents.get(name)))
    monkeypatch.setattr(service, "erp_cancel", lambda dt, n: writes.append(("cancel", dt, n)))
    monkeypatch.setattr(service, "erp_discard_draft", lambda dt, n: writes.append(("discard", dt, n)))
    return case, documents, writes


def test_all_rounds_and_erp_only_links_cancel_children_first(scenario):
    case, _, writes = scenario
    service.cancel_linked_rfq_documents(case)
    assert writes == [("cancel", "Supplier Quotation", "S1"), ("cancel", "Request for Quotation", "R1"),
                      ("discard", "Request for Quotation", "R2"), ("cancel", "Request for Quotation", "R3")]


@pytest.mark.parametrize("shared", ["R3", "S1"])
def test_shared_document_prevents_all_writes(scenario, shared):
    case, documents, writes = scenario
    documents[shared]["items"].append({"material_request": "MR-OTHER", "request_for_quotation": "R-OTHER"})
    with pytest.raises(ValueError):
        service.cancel_linked_rfq_documents(case)
    assert writes == []


@pytest.mark.parametrize("field", ["material_request", "supplier_quotation"])
def test_existing_po_prevents_all_writes(scenario, monkeypatch, field):
    case, _, writes = scenario
    original = service.erp_get
    def listing(doctype, **kwargs):
        if doctype == "Purchase Order" and kwargs["filters"][0][1] == field:
            return [{"name": "PO-1"}]
        return original(doctype, **kwargs)
    monkeypatch.setattr(service, "erp_get", listing)
    with pytest.raises(ValueError, match="발주서"):
        service.cancel_linked_rfq_documents(case)
    assert writes == []


def test_cancelled_documents_skipped(scenario):
    case, documents, writes = scenario
    documents["R1"]["docstatus"] = 2
    documents["S1"]["docstatus"] = 2
    service.cancel_linked_rfq_documents(case)
    assert not any(row[2] in {"R1", "S1"} for row in writes)


@pytest.mark.parametrize("status", ["RUNNING", "QUEUED", "COMPLETED"])
def test_busy_or_completed_case_is_protected(scenario, status):
    case, _, writes = scenario
    case["status"] = status
    with pytest.raises(ValueError):
        service.cancel_linked_rfq_documents(case)
    assert writes == []


def test_case_rejection_updates_state_only_after_erp_success(monkeypatch, scenario):
    from backend_logic2.services import workflow_service as workflow, graph_worker
    case, _, _ = scenario
    monkeypatch.setattr(workflow.case_repository, "get_case", lambda _: case)
    monkeypatch.setattr(graph_worker, "case_lock", lambda _: nullcontext())
    cancel = Mock()
    transition = Mock(return_value={"status": "CANCELLED"})
    reject = Mock()
    monkeypatch.setattr(workflow, "reject_material_request", reject)
    monkeypatch.setattr(workflow.task_repository, "cancel_pending_tasks", cancel)
    monkeypatch.setattr(workflow, "_delete_case_notifications_safely", Mock())
    monkeypatch.setattr(workflow.case_repository, "transition_case", transition)
    assert workflow.reject_case("C1", reason="중복 요청", rejected_by="buyer")["status"] == "CANCELLED"
    reject.assert_called_once_with("MR-1", "중복 요청", reason_code="BUYER_REJECTED")
    cancel.assert_called_once()
    reject.side_effect = ValueError("ERP link changed")
    transition.reset_mock()
    with pytest.raises(ValueError):
        workflow.reject_case("C1", reason="반려", rejected_by="buyer")
    transition.assert_not_called()
