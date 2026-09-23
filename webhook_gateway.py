"""Minimal public-facing app for local ERPNext webhook experiments.

Only the secret-authenticated ERPNext webhook router is mounted here.  The
main authentication, procurement and purchase APIs remain on localhost:8000
and are never exposed by the quick tunnel.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from backend_logic2.logging_config import configure_logging

# 이 프로세스는 webhook_dev.ps1이 -WindowStyle Hidden으로 띄워서 콘솔이 아예
# 없다. 로그가 어딘가에 파일로 남지 않으면 완전히 사라지므로, 다른 무엇보다
# 먼저 설정한다(main.py와 동일한 .runtime/app.log를 공유).
configure_logging()

load_dotenv(Path(__file__).resolve().parent / ".env")

from backend_logic2.api.procurement_routes import webhook_router  # noqa: E402


app = FastAPI(
    title="BiddingFlow ERPNext Webhook Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(webhook_router)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "biddingflow-webhook-gateway"}
