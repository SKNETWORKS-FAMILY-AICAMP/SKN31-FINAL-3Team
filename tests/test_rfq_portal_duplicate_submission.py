import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class AttrDict(dict):
    __getattr__ = dict.get


@pytest.fixture
def portal():
    frappe = ModuleType("frappe")
    frappe._ = lambda text: text
    frappe._dict = AttrDict
    frappe.whitelist = lambda: lambda function: function
    frappe.PermissionError = PermissionError
    frappe.throw = MagicMock(side_effect=ValueError("Rejected"))
    frappe.db = MagicMock()
    frappe.db.exists.return_value = True
    frappe.session = SimpleNamespace(user="supplier-user")
    frappe.get_all = MagicMock(return_value=["supplier-user"])
    frappe.get_doc = MagicMock()
    frappe.msgprint = MagicMock()
    utilities = ModuleType("frappe.utils")
    utilities.getdate = lambda value: date.fromisoformat(value)
    utilities.nowdate = lambda: "2026-09-16"
    party = ModuleType("erpnext.accounts.party")
    party.get_party_account_currency = lambda *args: "KRW"
    path = Path(__file__).resolve().parents[1] / "deploy/frappe_custom_apps/biddingflow_rfq_portal/biddingflow_rfq_portal/overrides/rfq.py"
    spec = importlib.util.spec_from_file_location("portal_rfq_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utilities,
                                  "erpnext.accounts.party": party}):
        spec.loader.exec_module(module)
    return module, frappe


def document(rfq="RFQ-1"):
    return {"supplier": "SUP-1", "company": "Company", "valid_till": "2026-09-30",
            "items": [{"parent": rfq, "name": "RFQ-ITEM-1", "item_code": "ITEM",
                       "expected_delivery_date": "2026-09-20", "qty": 1, "rate": 100}]}


def test_existing_draft_or_submitted_quote_returns_existing_without_creation(portal):
    module, frappe = portal
    frappe.db.sql.side_effect = [[AttrDict(docstatus=1)], [AttrDict(name="SQ-EXISTING")]]
    assert module.create_supplier_quotation(document()) == "SQ-EXISTING"
    frappe.get_doc.assert_not_called()
    queries = [call.args[0] for call in frappe.db.sql.call_args_list]
    assert "FOR UPDATE" in queries[0]
    assert "sq.docstatus < 2" in queries[1]
    assert "FOR UPDATE" in queries[1]


def test_first_submission_and_new_rfq_are_allowed(portal):
    module, frappe = portal
    frappe.db.sql.side_effect = [[AttrDict(docstatus=1)], []]
    quote = frappe.get_doc.return_value
    quote.name = "SQ-NEW"
    assert module.create_supplier_quotation(document("RFQ-NEW")) == "SQ-NEW"
    quote.save.assert_called_once()
    assert frappe.db.sql.call_args_list[1].args[1] == ("SUP-1", "RFQ-NEW")


def test_other_portal_user_cannot_probe_or_submit(portal):
    module, frappe = portal
    frappe.get_all.return_value = []
    with pytest.raises(ValueError):
        module.create_supplier_quotation(document())
    frappe.db.sql.assert_not_called()


@pytest.mark.parametrize("status", [0, 2])
def test_non_submitted_rfq_rejected(portal, status):
    module, frappe = portal
    frappe.db.sql.return_value = [AttrDict(docstatus=status)]
    with pytest.raises(ValueError):
        module.create_supplier_quotation(document())
    frappe.get_doc.assert_not_called()


def test_rfq_membership_required(portal):
    module, frappe = portal
    frappe.db.exists.return_value = False
    with pytest.raises(ValueError):
        module.create_supplier_quotation(document())
    frappe.get_doc.assert_not_called()


def test_mixed_rfqs_rejected(portal):
    module, frappe = portal
    doc = document()
    doc["items"].append({"parent": "ANOTHER"})
    with pytest.raises(ValueError):
        module.create_supplier_quotation(doc)
    frappe.db.sql.assert_not_called()


def test_sequential_retry_creates_only_one_document(portal):
    module, frappe = portal
    frappe.db.sql.side_effect = [[AttrDict(docstatus=1)], [],
                                [AttrDict(docstatus=1)], [AttrDict(name="SQ-NEW")]]
    frappe.get_doc.return_value.name = "SQ-NEW"
    assert module.create_supplier_quotation(document()) == "SQ-NEW"
    assert module.create_supplier_quotation(document()) == "SQ-NEW"
    frappe.get_doc.return_value.save.assert_called_once()
