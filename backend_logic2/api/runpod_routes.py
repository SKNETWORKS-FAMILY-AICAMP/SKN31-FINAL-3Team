"""RunPod notification inbox; never accept quotation values from the caller."""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from backend_logic2.repositories import quotation_jobs
from backend_logic2.services.runpod_quotation_jobs import process_job


router = APIRouter(prefix='/api/webhooks/runpod', tags=['RunPod Webhooks'])


class RunPodNotification(BaseModel):
    # Ignore untrusted output/status. Only the saved job identity can wake work.
    id: str = Field(min_length=1, max_length=200, pattern=r'^[A-Za-z0-9_-]+$')


@router.post('/quotation')
def quotation_completed(payload: RunPodNotification, background_tasks: BackgroundTasks):
    job = quotation_jobs.record_callback(payload.id)
    if job is None:
        # A fast worker can callback before the submit response is committed.
        # Ask RunPod to retry; a durable recovery pass covers lost callbacks.
        raise HTTPException(status_code=503, detail='Job is not yet recorded')
    if job['status'] in {'SUBMITTED', 'READY', 'REGISTERED'}:
        background_tasks.add_task(process_job, str(job['job_id']))
    # Wakeup has committed before ACK. Crashing after ACK is recoverable.
    return {'status': 'accepted'}
