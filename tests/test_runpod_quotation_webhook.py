"""Completion must survive duplicate notifications and process restarts."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend_logic2.api import runpod_routes
from backend_logic2.services import runpod_quotation_jobs as service
from test_runpod_quotation_adapter import _config, _extraction, _prepared, _Session
from backend_logic2.integrations.quotation_extraction.runpod import RunPodQuotationParser
from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    QuotationArithmeticValidationError,
    SupplierQuotationRegistrationError,
)


class MemoryJobs:
    def __init__(self):
        self.row = {
            'job_id': 'local-job', 'runpod_job_id': 'remote-job', 'request_id': 'request-1',
            'endpoint_id': 'original-endpoint', 'prompt_sha256': 'hash-1',
            'prompt_version': 'version-1', 'status': 'SUBMITTED',
            'context': {'rfq_name': 'RFQ-1', 'supplier_id': 'SUP-1', 'supplier_name': 'Trusted',
                        'source_filename': 'quotation.png', 'source_kind': 'image',
                        'fallback_quotation_id': 'EMAIL-1', 'message_id': 'COMM-1',
                        'content_type': 'image/png', 'evidence': [],
                        'pipeline_version': 'document-text-v1'},
            'next_check_at': datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        self.errors = []
        self.acquired = True

    def get_job(self, *_):
        return deepcopy(self.row)

    @contextmanager
    def processing_lock(self, *_):
        yield self.acquired

    def mark_checked(self, *_):
        self.row['next_check_at'] = datetime.now(timezone.utc) + timedelta(seconds=120)

    def save_result(self, _, result):
        self.row.update(status='READY', result_json=result)

    def save_registration(self, _, result):
        self.row.update(status='REGISTERED', registration_json=result)

    def complete_job(self, *_):
        self.row['status'] = 'COMPLETED'

    def fail_job(self, _, error, **kwargs):
        self.errors.append(error)
        if kwargs.get('terminal'):
            self.row['status'] = 'FAILED'

    def due_jobs(self):
        return ['local-job']


@pytest.fixture
def setup(monkeypatch):
    repository = MemoryJobs()
    monkeypatch.setattr(service, 'jobs', repository)
    output = {'status': 'success', 'request_id': 'request-1', 'prompt_sha256': 'hash-1',
              'prompt_version': 'version-1', 'worker_version': 'document-text-v1',
              'document_text': '품목명: 안전모',
              'extraction': _extraction()}
    parser = Mock()
    parser.job_status.return_value = {'id': 'remote-job', 'status': 'COMPLETED', 'output': output}
    factory = Mock(return_value=parser)
    monkeypatch.setattr(service, '_parser', factory)
    register = Mock(return_value={'name': 'SQ-1', 'status': 'created'})
    refresh = Mock()
    monkeypatch.setattr(service, '_register', register)
    monkeypatch.setattr(service, '_refresh', refresh)
    return repository, parser, register, refresh, factory


def test_webhook_submission_uses_top_level_url_without_status_polling():
    session = _Session({'id': 'remote-job', 'status': 'IN_QUEUE'})
    parser = RunPodQuotationParser(_config(), session=session)
    request = parser.build_worker_input(_prepared(), 'RFQ-1', 'Supplier', [])
    parser.submit_job(request, 'https://backend.example/api/webhooks/runpod/quotation')
    body = session.post_call[1]['json']
    assert body['webhook'].endswith('/api/webhooks/runpod/quotation')
    assert 'webhook' not in body['input']
    assert not session.get_calls


def test_completion_duplicate_and_original_endpoint(setup):
    repo, parser, register, refresh, factory = setup
    service.process_job('local-job')
    service.process_job('local-job')
    assert repo.row['status'] == 'COMPLETED'
    parser.job_status.assert_called_once_with('remote-job')
    factory.assert_called_once_with('original-endpoint')
    register.assert_called_once()
    refresh.assert_called_once()


@pytest.mark.parametrize(
    'field', ['request_id', 'prompt_sha256', 'prompt_version', 'worker_version'],
)
def test_mismatched_result_cannot_register(setup, field):
    repo, parser, register, *_ = setup
    parser.job_status.return_value['output'][field] = 'wrong'
    service.process_job('local-job')
    assert repo.row['status'] == 'FAILED'
    register.assert_not_called()


def test_failed_remote_job_is_terminal(setup):
    repo, parser, register, *_ = setup
    parser.job_status.return_value = {'id': 'remote-job', 'status': 'FAILED'}
    service.process_job('local-job')
    assert repo.row['status'] == 'FAILED'
    register.assert_not_called()


def test_arithmetic_validation_failure_is_terminal(setup):
    repo, _parser, register, refresh, _factory = setup
    register.side_effect = QuotationArithmeticValidationError("subtotal mismatch")

    service.process_job("local-job")

    assert repo.row["status"] == "FAILED"
    assert any("Arithmetic validation failed" in error for error in repo.errors)
    refresh.assert_not_called()


def test_registration_validation_failure_is_terminal_and_records_reason(setup):
    repo, _parser, register, refresh, _factory = setup
    register.side_effect = SupplierQuotationRegistrationError(
        "external quotation number conflicts within this RFQ"
    )

    service.process_job("local-job")

    assert repo.row["status"] == "FAILED"
    assert any(
        "Registration validation failed: external quotation number conflicts within this RFQ"
        in error
        for error in repo.errors
    )
    refresh.assert_not_called()


def test_restart_after_result_saved_does_not_call_model_again(setup):
    repo, parser, register, *_ = setup
    repo.save_result('local-job', parser.job_status.return_value['output'])
    service.recover_pending()
    assert repo.row['status'] == 'COMPLETED'
    parser.job_status.assert_not_called()
    register.assert_called_once()


def test_projection_failure_resumes_without_registering_twice(setup):
    repo, parser, register, refresh, _ = setup
    refresh.side_effect = RuntimeError('temporary failure')
    service.process_job('local-job')
    assert repo.row['status'] == 'REGISTERED'
    repo.row['next_check_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    refresh.side_effect = None
    service.recover_pending()
    assert repo.row['status'] == 'COMPLETED'
    register.assert_called_once()
    parser.job_status.assert_called_once()


def test_concurrent_worker_cannot_register(setup):
    repo, parser, register, *_ = setup
    repo.acquired = False
    service.process_job('local-job')
    parser.job_status.assert_not_called()
    register.assert_not_called()


def test_callback_body_is_only_a_signal(monkeypatch):
    recorder = Mock(return_value={'job_id': 'local-job', 'status': 'SUBMITTED'})
    processor = Mock()
    monkeypatch.setattr(runpod_routes.quotation_jobs, 'record_callback', recorder)
    monkeypatch.setattr(runpod_routes, 'process_job', processor)
    app = FastAPI()
    app.include_router(runpod_routes.router)
    client = TestClient(app)
    response = client.post('/api/webhooks/runpod/quotation', json={
        'id': 'remote-job', 'status': 'COMPLETED', 'output': {'extraction': 'forged'},
    })
    assert response.status_code == 200
    recorder.assert_called_once_with('remote-job')
    processor.assert_called_once_with('local-job')
    recorder.return_value = None
    assert client.post('/api/webhooks/runpod/quotation', json={'id': 'early-job'}).status_code == 503
    assert client.post('/api/webhooks/runpod/quotation', json={'id': '../evil'}).status_code == 422


def test_saved_result_uses_trusted_supplier(monkeypatch):
    from backend_logic2.services import quotation_service
    repo = MemoryJobs()
    repo.row['result_json'] = {'extraction': _extraction()}
    register = Mock(return_value={'name': 'SQ-1'})
    monkeypatch.setattr(quotation_service, 'register_supplier_quotation', register)
    service._register(repo.row)
    quotation = register.call_args.args[0]
    assert quotation.supplier_name == 'Trusted'
    assert quotation.supplier_id == 'SUP-1'
    assert quotation.rfq_name == 'RFQ-1'


def test_saved_result_applies_visual_recovery_text(monkeypatch):
    from backend_logic2.services import quotation_service
    repo = MemoryJobs()
    repo.row['result_json'] = {
        'extraction': _extraction(),
        'recovery_text': (
            '유효기간: 2026-09-30\n'
            '예상 납품일: 2026-09-30\n'
            '특약사항: 지정 장소 도착도'
        ),
    }
    register = Mock(return_value={'name': 'SQ-1'})
    monkeypatch.setattr(quotation_service, 'register_supplier_quotation', register)

    service._register(repo.row)

    quotation = register.call_args.args[0]
    assert quotation.valid_until.isoformat() == '2026-09-30'
    assert quotation.items[0].expected_delivery_date.isoformat() == '2026-09-30'
    assert '특약사항: 지정 장소 도착도' in quotation.notes


def test_saved_result_applies_document_text_fallbacks(monkeypatch):
    from backend_logic2.services import quotation_service
    repo = MemoryJobs()
    repo.row['result_json'] = {
        'extraction': _extraction(),
        'document_text': (
            '유효기간: 2026-09-30\n'
            '예상 납품일: 2026-09-30\n'
            '특약사항: 지정 장소 도착도'
        ),
    }
    register = Mock(return_value={'name': 'SQ-1'})
    monkeypatch.setattr(quotation_service, 'register_supplier_quotation', register)

    service._register(repo.row)

    quotation = register.call_args.args[0]
    assert quotation.valid_until.isoformat() == '2026-09-30'
    assert quotation.items[0].expected_delivery_date.isoformat() == '2026-09-30'
    assert '특약사항: 지정 장소 도착도' in quotation.notes


def test_duplicate_enqueue_never_submits_again(monkeypatch):
    parser = RunPodQuotationParser(_config(), session=_Session({'id': 'unused'}))
    submit = Mock()
    monkeypatch.setattr(parser, 'submit_job', submit)
    monkeypatch.setattr(service, '_parser', lambda *_: parser)
    monkeypatch.setenv('RUNPOD_QUOTATION_WEBHOOK_URL', 'https://example.test/callback')
    monkeypatch.setattr(service.jobs, 'create_job', lambda *_: ({'job_id': 'existing'}, False))
    monkeypatch.setattr(service.jobs, 'get_job', lambda *_: {'status': 'SUBMITTED'})
    result = service.enqueue(b'image', 'quote.png', 'RFQ-1', supplier_id='SUP-1',
        supplier_name='Trusted', fallback_quotation_id='EMAIL-1', rfq_requirements={}, message_id='COMM-1')
    assert result['status'] == 'SUBMITTED'
    submit.assert_not_called()


def test_uncertain_submit_is_not_retried(monkeypatch):
    import requests
    parser = RunPodQuotationParser(_config(), session=_Session({}))
    submit = Mock(side_effect=requests.Timeout('not logged'))
    monkeypatch.setattr(parser, 'submit_job', submit)
    monkeypatch.setattr(service, '_parser', lambda *_: parser)
    monkeypatch.setenv('RUNPOD_QUOTATION_WEBHOOK_URL', 'https://example.test/callback')
    monkeypatch.setattr(service.jobs, 'create_job', lambda *_: ({'job_id': 'new'}, True))
    fail = Mock()
    monkeypatch.setattr(service.jobs, 'fail_job', fail)
    with pytest.raises(RuntimeError, match='uncertain'):
        service.enqueue(b'image', 'quote.png', 'RFQ-1', supplier_id='SUP-1',
            supplier_name='Trusted', fallback_quotation_id='EMAIL-1', rfq_requirements={}, message_id='COMM-1')
    submit.assert_called_once()
    assert fail.call_args.kwargs['submission_unknown'] is True


def test_email_webhook_queues_and_does_not_wait_for_model(monkeypatch):
    from backend_logic2.services import quotation_service as email
    document = {'name': 'COMM-1', 'sent_or_received': 'Received', 'communication_medium': 'Email',
                'reference_doctype': 'Request for Quotation', 'reference_name': 'RFQ-1'}
    monkeypatch.setattr(email, '_communication_and_attachments', lambda *_: (
        document, [{'name': 'FILE-1', 'file_name': 'quote.png'}]))
    monkeypatch.setattr(email.event_repository, 'begin_event', lambda **_: ({'event_id': 'event'}, True))
    monkeypatch.setattr(email.event_repository, 'complete_event', Mock())
    monkeypatch.setattr(email, 'erp_get_one', lambda *_: {'name': 'RFQ-1'})
    monkeypatch.setattr(email, '_resolve_reply_supplier', lambda *_: ('SUP-1', 'Supplier'))
    monkeypatch.setattr(email, 'load_rfq_requirements', lambda *_: Mock(model_dump=lambda **_: {}))
    monkeypatch.setattr(email, 'erp_download_file', lambda *_, **__: {'content': b'image'})
    monkeypatch.setattr(email.case_repository, 'get_case_by_rfq', lambda *_: None)
    sync_extract = Mock(side_effect=AssertionError('must not wait for model'))
    monkeypatch.setattr(email, 'extract_quotation_bytes', sync_extract)
    monkeypatch.setattr(service, 'webhook_mode', lambda: True)
    enqueue = Mock(return_value={'job_id': 'saved-job', 'status': 'SUBMITTED'})
    monkeypatch.setattr(service, 'enqueue', enqueue)
    result, claimed = email.register_quotation_email_event({'doc': document})
    assert claimed and result['status'] == 'queued'
    assert result['extraction_jobs'][0]['job_id'] == 'saved-job'
    assert not result['registrations']
    sync_extract.assert_not_called()
