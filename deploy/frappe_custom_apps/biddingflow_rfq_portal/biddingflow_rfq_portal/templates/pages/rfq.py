"""Reuse ERPNext's secured RFQ portal context without copying core logic."""

from erpnext.templates.pages.rfq import get_context as get_erpnext_rfq_context


def get_context(context):
    return get_erpnext_rfq_context(context)
