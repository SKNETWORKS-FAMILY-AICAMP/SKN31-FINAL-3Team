from scripts.configure_erpnext_webhooks import (
    _managed_existing,
    _payload_template,
    _specs,
    configure,
)


def test_webhook_specs_cover_all_external_document_changes():
    events = {(spec.doctype, spec.event) for spec in _specs()}

    assert ("Material Request", "after_insert") in events
    assert ("Material Request", "on_trash") in events
    assert ("File", "on_update") in events
    assert ("Item", "after_insert") in events
    assert ("Item", "on_update") in events
    assert ("Supplier Quotation", "on_submit") in events
    assert ("Supplier Quotation", "on_cancel") in events
    assert ("Communication", "after_insert") in events
    assert ("Communication", "on_update") in events
    assert ("Purchase Order", "on_submit") in events
    assert ("Purchase Receipt", "on_cancel") in events
    assert ("Purchase Invoice", "on_update_after_submit") in events
    assert ("Payment Entry", "on_submit") in events
    assert len(events) == 29


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


def test_file_webhooks_are_scoped_to_material_requests_or_communications():
    file_specs = [spec for spec in _specs() if spec.doctype == "File"]

    assert file_specs
    assert {spec.endpoint for spec in file_specs} == {
        "material-request-file",
        "quotation-email-file",
    }
    assert {spec.condition for spec in file_specs} == {
        'doc.attached_to_doctype == "Material Request"',
        'doc.attached_to_doctype == "Communication"',
    }
    assert all('"file_url"' in _payload_template(spec) for spec in file_specs)


def test_same_file_event_webhooks_are_matched_by_endpoint():
    specs = [
        spec
        for spec in _specs()
        if spec.doctype == "File" and spec.event == "after_insert"
    ]
    rows = [
        {
            "name": "BiddingFlow - File - after_insert",
            "webhook_doctype": "File",
            "webhook_docevent": "after_insert",
            "request_url": "https://example.test/api/webhooks/erpnext/material-request-file",
        },
        {
            "name": "BiddingFlow - File - after_insert - quotation-email-file",
            "webhook_doctype": "File",
            "webhook_docevent": "after_insert",
            "request_url": "https://example.test/api/webhooks/erpnext/quotation-email-file",
        },
    ]

    matched = {
        spec.endpoint: _managed_existing(rows, spec)["request_url"]
        for spec in specs
    }

    assert matched["material-request-file"].endswith("/material-request-file")
    assert matched["quotation-email-file"].endswith("/quotation-email-file")


def test_quotation_email_payload_contains_rfq_link_and_sender():
    spec = next(
        spec
        for spec in _specs()
        if spec.doctype == "Communication" and spec.event == "after_insert"
    )

    template = _payload_template(spec)

    assert '"sent_or_received"' in template
    assert '"communication_medium"' in template
    assert '"sender"' in template
    assert '"reference_doctype"' in template
    assert '"reference_name"' in template


def test_configure_rejects_unknown_endpoint_before_reading_erpnext(monkeypatch):
    monkeypatch.setenv("ERPNEXT_WEBHOOK_SECRET", "test-secret")

    try:
        configure(
            base_url="https://example.test",
            apply=False,
            disable=False,
            endpoints={"unknown-endpoint"},
        )
    except RuntimeError as exc:
        assert "unknown-endpoint" in str(exc)
    else:
        raise AssertionError("unknown endpoint must be rejected")
