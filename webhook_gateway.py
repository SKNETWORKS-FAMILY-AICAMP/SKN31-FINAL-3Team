"""Minimal public-facing app for local ERPNext webhook experiments.

Only the secret-authenticated ERPNext webhook router is mounted here.  The
main authentication, procurement and purchase APIs remain on localhost:8000
and are never exposed by the quick tunnel.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI


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
