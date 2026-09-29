"""Resolve original SQ/mail files within an already-authorized procurement case."""
from backend_logic2.integrations.erp_client import erp_get, erp_get_one, erp_get_communication_attachments
from procurement_db import get_connection


def originals(case: dict, quotation_id: str) -> list[dict]:
    values = (case.get('workflow_snapshot') or {}).get('values') or {}
    known = {str(values.get('rfq_name') or '')}
    known.update(str(r.get('rfq_name') or '') for r in values.get('rfq_rounds') or [] if isinstance(r, dict))
    known.discard('')
    sq = erp_get_one('Supplier Quotation', quotation_id) or {}
    linked = {str(sq.get('request_for_quotation') or '')}
    linked.update(str(item.get('request_for_quotation') or '') for item in sq.get('items') or [])
    if not known.intersection(linked):
        raise PermissionError('이 구매 건의 견적서가 아닙니다.')
    files = list(erp_get('File', filters={'attached_to_doctype': 'Supplier Quotation', 'attached_to_name': quotation_id},
                         fields=['name', 'file_name', 'attached_to_doctype', 'attached_to_name'], limit=200) or [])
    with get_connection() as conn:
        submission = conn.execute('''SELECT communication_name, rfq_name FROM procurement.quotation_submission
            WHERE quotation_name = %s AND source = 'email' ''', (quotation_id,)).fetchone()
    if submission and submission.get('communication_name') and submission.get('rfq_name') in known:
        name = submission['communication_name']
        communication = erp_get_one('Communication', name) or {}
        if (communication.get('reference_doctype') == 'Request for Quotation'
                and communication.get('reference_name') == submission['rfq_name']
                and communication.get('sent_or_received') == 'Received'):
            files.extend({**file, 'attached_to_doctype': 'Communication', 'attached_to_name': name}
                         for file in erp_get_communication_attachments(name) or [])
    return list({str(f['name']): f for f in files if f.get('name')}.values())
