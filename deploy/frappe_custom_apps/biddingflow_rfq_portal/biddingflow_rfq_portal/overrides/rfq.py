"""Server-side override for Supplier Quotation creation from the RFQ portal."""

import json

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from erpnext.accounts.party import get_party_account_currency


def _validate_portal_supplier(supplier: str) -> None:
    """Preserve ERPNext's portal-user-to-supplier authorization boundary."""
    if not supplier or frappe.session.user not in frappe.get_all(
        "Portal User", {"parent": supplier}, pluck="user"
    ):
        frappe.throw(_("Not Permitted"), frappe.PermissionError)


def _validate_dates(doc: frappe._dict) -> None:
    """Reject incomplete or already-expired promises before creating a draft."""
    today = getdate(nowdate())
    valid_till = doc.get("valid_till")

    if not valid_till:
        frappe.throw(_("Valid Till is required."))
    if getdate(valid_till) < today:
        frappe.throw(_("Valid Till cannot be earlier than today."))

    for item in doc.get("items") or []:
        item = frappe._dict(item)
        expected_date = item.get("expected_delivery_date")
        item_label = item.get("item_code") or item.get("item_name") or item.get("idx")

        if not expected_date:
            frappe.throw(
                _("Expected Delivery Date is required for item {0}.").format(
                    frappe.bold(item_label)
                )
            )
        if getdate(expected_date) < today:
            frappe.throw(
                _("Expected Delivery Date for item {0} cannot be earlier than today.").format(
                    frappe.bold(item_label)
                )
            )


def _append_items(sq_doc, supplier: str, items: list[dict]) -> None:
    """Map RFQ portal rows to Supplier Quotation Item rows."""
    copied_fields = (
        "item_code",
        "item_name",
        "description",
        "qty",
        "rate",
        "expected_delivery_date",
        "conversion_factor",
        "warehouse",
        "material_request",
        "material_request_item",
        "stock_qty",
        "uom",
    )

    for raw_item in items:
        item = frappe._dict(raw_item)
        args = {field: item.get(field) for field in copied_fields}
        args.update(
            {
                "request_for_quotation_item": item.get("name"),
                "request_for_quotation": item.get("parent"),
                "supplier_part_no": frappe.db.get_value(
                    "Item Supplier",
                    {"parent": item.get("item_code"), "supplier": supplier},
                    "supplier_part_no",
                ),
            }
        )
        sq_doc.append("items", args)


@frappe.whitelist()
def create_supplier_quotation(doc):
    """Create a Supplier Quotation Draft with portal-entered promise dates."""
    if isinstance(doc, str):
        doc = json.loads(doc)
    doc = frappe._dict(doc)

    supplier = doc.get("supplier")
    _validate_portal_supplier(supplier)
    _validate_dates(doc)

    sq_doc = frappe.get_doc(
        {
            "doctype": "Supplier Quotation",
            "supplier": supplier,
            "terms": doc.get("terms"),
            "company": doc.get("company"),
            "valid_till": doc.get("valid_till"),
            "currency": doc.get("currency")
            or get_party_account_currency("Supplier", supplier, doc.get("company")),
            "buying_price_list": doc.get("buying_price_list")
            or frappe.db.get_single_value("Buying Settings", "buying_price_list"),
        }
    )
    _append_items(sq_doc, supplier, doc.get("items") or [])
    sq_doc.flags.ignore_permissions = True
    sq_doc.run_method("set_missing_values")
    sq_doc.save()

    frappe.msgprint(_("Supplier Quotation {0} Created").format(sq_doc.name))
    return sq_doc.name
