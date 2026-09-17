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


def _lock_rfq_and_find_quotation(doc, supplier):
    """Serialize portal submissions until Frappe commits the POST transaction."""
    items = doc.get("items") or []
    rfqs = {item.get("parent") for item in items}
    if not items or len(rfqs) != 1 or not all(rfqs):
        frappe.throw(_("A quotation must belong to one Request for Quotation."))
    rfq = next(iter(rfqs))
    if not frappe.db.exists("Request for Quotation Supplier", {"parent": rfq, "supplier": supplier}):
        frappe.throw(_("Not Permitted"), frappe.PermissionError)
    locked = frappe.db.sql(
        "SELECT name, docstatus FROM `tabRequest for Quotation` WHERE name=%s FOR UPDATE",
        (rfq,), as_dict=True,
    )
    if not locked or locked[0].docstatus != 1:
        frappe.throw(_("This Request for Quotation is not open for submission."))
    for item in items:
        if not frappe.db.exists("Request for Quotation Item", {
            "parent": rfq, "name": item.get("name"), "item_code": item.get("item_code"),
        }):
            frappe.throw(_("Invalid Request for Quotation item."))
    # A locking read sees the preceding request's committed draft even with
    # MariaDB REPEATABLE READ. Drafts count as received; cancelled quotes do not.
    existing = frappe.db.sql(
        """SELECT sq.name FROM `tabSupplier Quotation` sq
           INNER JOIN `tabSupplier Quotation Item` item ON item.parent=sq.name
           WHERE sq.supplier=%s AND item.request_for_quotation=%s
             AND sq.docstatus < 2
           ORDER BY sq.creation ASC LIMIT 1 FOR UPDATE""",
        (supplier, rfq), as_dict=True,
    )
    return existing[0].name if existing else None


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
    existing = _lock_rfq_and_find_quotation(doc, supplier)
    if existing:
        frappe.msgprint(_("A quotation has already been received for this request. Opening the existing quotation."))
        return existing
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
