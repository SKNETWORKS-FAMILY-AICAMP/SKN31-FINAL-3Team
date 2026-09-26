"""Submit once, persist results, and resume quotation registration after callback.

Callbacks are untrusted wakeups. Only authenticated RunPod status responses
may produce an ERP quotation. The recovery loop uses the same completion path.
"""

import logging
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from backend_logic2.integrations.quotation_extraction.runpod import (
    RunPodQuotationConfig, RunPodQuotationParser, TERMINAL_FAILURE_STATUSES,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
    PreparedSource, _extract_prepared_quotation, _normalize_finetuned_quotation,
    _normalize_currency,
    apply_document_fallbacks, extract_document_fallbacks, prepare_source_bytes,
    prepare_rfq_specifications,
    validate_document_delivery_evidence,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_models import SourceKind
from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    QuotationArithmeticValidationError,
    SupplierQuotationRegistrationError,
)
from backend_logic2.repositories import quotation_jobs as jobs


LOGGER = logging.getLogger(__name__)


def webhook_mode():
    return (
        os.getenv('QUOTATION_EXTRACTOR_PROVIDER', 'runpod').strip().lower() == 'runpod'
        and os.getenv('RUNPOD_QUOTATION_DELIVERY_MODE', 'polling').strip().lower() == 'webhook'
    )


def callback_url():
    url = os.getenv('RUNPOD_QUOTATION_WEBHOOK_URL', '').strip()
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('RUNPOD_QUOTATION_WEBHOOK_URL must be a public HTTPS URL')
    return url


def _parser(endpoint_id=None):
    config = RunPodQuotationConfig.from_env()
    # Pending jobs retain their original endpoint if deployment settings change.
    if endpoint_id:
        config = replace(config, endpoint_id=endpoint_id)
    return RunPodQuotationParser(config)


def enqueue(data, filename, rfq_name, *, supplier_id, supplier_name,
            fallback_quotation_id, rfq_requirements, message_id, content_type=None):
    url = callback_url()  # Validate configuration before recording/submitting work.
    parser = _parser()
    prepared = prepare_source_bytes(data, filename)
    prepare_rfq_specifications(prepared, rfq_requirements)
    document_fallbacks = extract_document_fallbacks(prepared.text)
    worker_input = parser.build_worker_input(prepared, rfq_name, supplier_name, [])
    # Application request_id deduplicates overlapping Communication/File events.
    # RunPod itself does not promise idempotency for repeated POST /run calls.
    job, created = jobs.create_job(
        worker_input['request_id'], parser.config.endpoint_id,
        {
            'rfq_name': rfq_name, 'supplier_id': supplier_id, 'supplier_name': supplier_name,
            'source_filename': filename, 'fallback_quotation_id': fallback_quotation_id,
            'message_id': message_id, 'content_type': content_type,
            'source_kind': prepared.kind.value, 'evidence': prepared.evidence,
            'document_fallbacks': document_fallbacks,
            'pipeline_version': parser.config.pipeline_version,
        }, worker_input['prompt_sha256'], worker_input['prompt_version'],
    )
    if created:
        try:
            response = parser.submit_job(worker_input, url)
            remote_id = str(response.get('id') or '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', remote_id):
                raise ValueError('RunPod submission returned no valid job ID')
            jobs.mark_submitted(str(job['job_id']), remote_id)
        except Exception as exc:
            jobs.fail_job(str(job['job_id']), f'Submission uncertain: {type(exc).__name__}',
                          submission_unknown=True)
            raise RuntimeError('RunPod submission uncertain; inspect job before retry') from None
    current = jobs.get_job(str(job['job_id']))
    return {'job_id': str(job['job_id']), 'status': current['status']}


class _RecordedParser:
    def __init__(self, extraction):
        self.extraction = extraction

    def __call__(self, *_args):
        return _normalize_finetuned_quotation(self.extraction)

    def extraction_evidence(self):
        return ['RunPod Serverless: authenticated result, durable webhook delivery']


def _register(job):
    # Import lazily to avoid the existing quotation_service dependency cycle.
    from backend_logic2.services import quotation_service
    context = dict(job['context'])
    kind = context.pop('source_kind')
    evidence = context.pop('evidence', [])
    document_fallbacks = context.pop('document_fallbacks', {})
    context.pop('pipeline_version', None)
    prepared = PreparedSource(kind=SourceKind(kind), text='', evidence=evidence)
    extraction = dict(job['result_json']['extraction'])
    apply_document_fallbacks(extraction, document_fallbacks)
    extracted_document_text = job['result_json'].get('document_text')
    if extracted_document_text is not None:
        max_text_chars = int(os.getenv('RUNPOD_QUOTATION_MAX_TEXT_CHARS', '60000'))
        if (not isinstance(extracted_document_text, str)
                or len(extracted_document_text) > max_text_chars):
            raise ValueError('RunPod document_text is invalid')
        extracted_fallbacks = extract_document_fallbacks(extracted_document_text)
        if extracted_fallbacks.get('conflicts'):
            raise ValueError('RunPod document_text contains conflicting date or lead-time values')
        validate_document_delivery_evidence(extraction, extracted_document_text)
        apply_document_fallbacks(extraction, extracted_fallbacks)
    recovery_text = job['result_json'].get('recovery_text')
    if recovery_text is not None:
        max_text_chars = int(os.getenv('RUNPOD_QUOTATION_MAX_TEXT_CHARS', '60000'))
        if not isinstance(recovery_text, str) or len(recovery_text) > max_text_chars:
            raise ValueError('RunPod recovery_text is invalid')
        apply_document_fallbacks(
            extraction,
            extract_document_fallbacks(recovery_text),
        )
    quotation = _extract_prepared_quotation(
        prepared, model_parser=_RecordedParser(extraction), **context,
    )
    return quotation_service.register_supplier_quotation(quotation)


def _refresh(job):
    from backend_logic2.services import quotation_service
    case = quotation_service.case_repository.get_case_by_rfq(job['context']['rfq_name'])
    if case:
        quotation_service.refresh_case_quotations(
            case, rfq_name=job['context']['rfq_name'], notify=True,
        )


def _record_final_failure(job_id, exc, *, kind=None):
    """잡이 최종 실패(FAILED)로 끝났을 때만 화면 안내용으로 기록한다."""
    from backend_logic2.services import quotation_service

    current = jobs.get_job(job_id)
    if not current or current['status'] != 'FAILED':
        return
    context = current.get('context') or {}
    quotation_service.record_intake_failure(
        exc,
        rfq_name=context.get('rfq_name') or '',
        supplier_id=context.get('supplier_id'),
        supplier_name=context.get('supplier_name'),
        source_filename=context.get('source_filename'),
        file_id=None,
        communication_name=context.get('message_id'),
        kind=kind,
    )


def process_job(job_id):
    job = jobs.get_job(job_id)
    if not job or job['status'] not in {'SUBMITTED', 'READY', 'REGISTERED'}:
        return
    key = f"{job['context']['rfq_name']}:{job['context']['supplier_id']}"
    with jobs.processing_lock(job_id, key) as acquired:
        if not acquired:
            return
        job = jobs.get_job(job_id)
        if job['status'] not in {'SUBMITTED', 'READY', 'REGISTERED'}:
            return
        if job['next_check_at'] > datetime.now(timezone.utc):
            return
        try:
            if job['status'] == 'SUBMITTED':
                jobs.mark_checked(job_id)
                response = _parser(job['endpoint_id']).job_status(job['runpod_job_id'])
                if response.get('id') != job['runpod_job_id']:
                    raise ValueError('RunPod response job ID mismatch')
                status = response.get('status')
                if status in TERMINAL_FAILURE_STATUSES:
                    jobs.fail_job(job_id, f'RunPod terminal status: {status}', terminal=True)
                    _record_final_failure(
                        job_id, RuntimeError(f'RunPod terminal status: {status}'), kind='extraction',
                    )
                    return
                if status != 'COMPLETED':
                    return
                output = response.get('output') or {}
                output_extraction = output.get('extraction')
                expected_pipeline = job['context'].get('pipeline_version')
                output_document_text = output.get('document_text')
                max_text_chars = int(
                    os.getenv('RUNPOD_QUOTATION_MAX_TEXT_CHARS', '60000')
                )
                document_conflicts = (
                    extract_document_fallbacks(output_document_text).get('conflicts')
                    if (isinstance(output_document_text, str)
                        and len(output_document_text) <= max_text_chars) else None
                )
                if (output.get('status') != 'success'
                        or output.get('request_id') != job['request_id']
                        or output.get('prompt_sha256') != job['prompt_sha256']
                        or output.get('prompt_version') != job['prompt_version']
                        or (expected_pipeline
                            and output.get('worker_version') != expected_pipeline)
                        or (expected_pipeline == 'document-text-v1'
                            and (not isinstance(output_document_text, str)
                                 or not output_document_text.strip()
                                 or len(output_document_text) > max_text_chars))
                        or bool(document_conflicts)
                        or not isinstance(output_extraction, dict)
                        or (isinstance(output_extraction, dict)
                            and not _normalize_currency(output_extraction.get('currency')))):
                    jobs.fail_job(job_id, 'RunPod result identity/schema mismatch', terminal=True)
                    _record_final_failure(
                        job_id, ValueError('RunPod result identity/schema mismatch'), kind='parse',
                    )
                    return
                # Save before ERP I/O: recovery no longer depends on RunPod retention.
                jobs.save_result(job_id, output)
                job = jobs.get_job(job_id)
            if job['status'] == 'READY':
                registration = _register(job)
                jobs.save_registration(job_id, registration)
                job = jobs.get_job(job_id)
            _refresh(job)
            jobs.complete_job(job_id)
        except QuotationArithmeticValidationError as exc:
            jobs.fail_job(
                job_id,
                f'Arithmetic validation failed: {exc}',
                terminal=True,
            )
            LOGGER.warning('RunPod quotation job %s rejected by arithmetic validation', job_id)
            _record_final_failure(job_id, exc, kind='arithmetic')
        except SupplierQuotationRegistrationError as exc:
            # These are deterministic RFQ mapping, policy, or duplicate conflicts.
            # The messages are application-controlled and contain no provider
            # response bodies, so retaining them is safe and useful to operators.
            jobs.fail_job(
                job_id,
                f'Registration validation failed: {exc}',
                terminal=True,
            )
            LOGGER.warning('RunPod quotation job %s rejected by registration validation', job_id)
        except Exception as exc:
            jobs.fail_job(job_id, f'Completion failed: {type(exc).__name__}')
            LOGGER.warning('RunPod quotation job %s deferred (%s)', job_id, type(exc).__name__)
            # 재시도 한도를 넘겨 FAILED가 된 경우에만 기록된다.
            _record_final_failure(job_id, exc)


def recover_pending():
    for job_id in jobs.due_jobs():
        try:
            process_job(job_id)
        except Exception as exc:
            LOGGER.warning('RunPod job recovery deferred: %s (%s)', job_id, type(exc).__name__)
