from scripts.configure_erpnext_webhooks import _payload_template, _specs


def test_webhook_specs_cover_all_external_document_changes():
    events = {(spec.doctype, spec.event) for spec in _specs()}

    assert ("Material Request", "after_insert") in events
    assert ("Material Request", "on_trash") in events
    assert ("File", "on_update") in events
    assert ("Item", "after_insert") in events
    assert ("Item", "on_update") in events
    assert ("Supplier Quotation", "on_submit") in events
    assert ("Supplier Quotation", "on_cancel") in events
    assert ("Purchase Order", "on_submit") in events
    assert ("Purchase Receipt", "on_cancel") in events
    assert ("Purchase Invoice", "on_update_after_submit") in events
    assert ("Payment Entry", "on_submit") in events
    assert len(events) == 27


def test_webhook_payloads_are_identifier_only_and_refetched_by_backend():
    forbidden_business_fields = (
        '"items"',
        '"qty"',
        '"rate"',
        '"amount"',
        '"grand_total"',
        '"description"',
    )

    for spec in _specs():
        template = _payload_template(spec)
        assert '"doctype"' in template
        assert '"name"' in template
        assert '"modified"' in template
        assert all(field not in template for field in forbidden_business_fields)


def test_file_webhooks_are_limited_to_material_request_attachments():
    file_specs = [spec for spec in _specs() if spec.doctype == "File"]

    assert file_specs
    assert all(
        spec.condition == 'doc.attached_to_doctype == "Material Request"'
        for spec in file_specs
    )
    assert all('"file_url"' in _payload_template(spec) for spec in file_specs)
