"""FastAPI entry point for the SKN31 purchasing agent."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware


# Load secrets before importing ERP and authentication modules.
ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_FILE)

from auth_service.dependencies import CurrentUser, require_authenticated_user
from auth_service.router import router as auth_router
from backend_logic2.api.mr_substitute_routes import router as mr_substitute_router
from backend_logic2.api.procurement_routes import (
    router as procurement_router,
    webhook_router as erpnext_webhook_router,
)
from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    SITE_URL,
    get_stock_level,
    router as purchase_router,
)
from backend_logic2.nodes.mr.reject_material_request import (
    router as material_request_router,
)
from backend_logic2.nodes.mr.substitute_reply_watcher import (
    run_once as process_substitute_replies,
)
from backend_logic2.services import receipt_service, workflow_service


LOGGER = logging.getLogger(__name__)


def _mr_ingest_mode() -> str:
    """Return the configured MR discovery mode with a safe default."""

    value = os.getenv("MR_INGEST_MODE", "webhook").strip().lower()
    return "polling" if value == "polling" else "webhook"


def _mr_poll_interval_seconds() -> float:
    try:
        configured = float(os.getenv("MR_POLL_INTERVAL_SECONDS", "5"))
    except ValueError:
        configured = 5.0
    return max(2.0, configured)


def _purchase_document_poll_interval_seconds() -> float:
    try:
        configured = float(os.getenv("PURCHASE_DOCUMENT_POLL_INTERVAL_SECONDS", "15"))
    except ValueError:
        configured = 15.0
    return max(5.0, configured)


async def _poll_material_requests() -> None:
    """Discover ERPNext Draft MRs while direct webhooks are unavailable.

    The first pass performs a full reconciliation so deleted/cancelled ERP
    documents cannot remain as active PostgreSQL cases. Later passes use the
    lightweight path and only discover/upsert current Draft MRs.
    """

    reconcile_existing = True
    interval = _mr_poll_interval_seconds()
    while True:
        try:
            await asyncio.to_thread(
                workflow_service.sync_draft_material_requests,
                reconcile_existing=reconcile_existing,
            )
            reconcile_existing = False
        except asyncio.CancelledError:
            raise
        except Exception:
            # A temporary ERP/DB outage must not stop later polling attempts.
            LOGGER.exception("ERPNext MR polling failed; retrying in %.1f seconds", interval)
        await asyncio.sleep(interval)


async def _poll_substitute_decisions() -> None:
    """Consume requester decisions recorded as ERPNext Comments.

    MR creation can switch to a webhook after deployment, but requester-side
    decisions intentionally stay same-origin in ERPNext. This small poller is
    therefore independent from ``MR_INGEST_MODE`` and does not require the
    requester's browser to reach the BiddingFlow backend directly.
    """

    interval = _mr_poll_interval_seconds()
    while True:
        try:
            await asyncio.to_thread(process_substitute_replies)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "ERPNext substitute decision polling failed; retrying in %.1f seconds",
                interval,
            )
        await asyncio.sleep(interval)


async def _poll_purchase_documents(*, continuous: bool) -> None:
    """Reconcile receipt, invoice, and payment documents missed by webhooks.

    Webhook mode still performs one pass at startup, covering events that
    happened while this API was offline. Polling mode continues periodically.
    """

    interval = _purchase_document_poll_interval_seconds()
    while True:
        try:
            await asyncio.to_thread(receipt_service.reconcile_purchase_documents)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "ERPNext purchase-document reconciliation failed; retrying in %.1f seconds",
                interval,
            )
        if not continuous:
            return
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    polling_task: asyncio.Task[None] | None = None
    substitute_task = asyncio.create_task(
        _poll_substitute_decisions(),
        name="erpnext-substitute-decision-poller",
    )
    app.state.substitute_polling_task = substitute_task
    purchase_document_task = asyncio.create_task(
        _poll_purchase_documents(continuous=_mr_ingest_mode() == "polling"),
        name="erpnext-purchase-document-reconciler",
    )
    app.state.purchase_document_polling_task = purchase_document_task
    if _mr_ingest_mode() == "polling":
        polling_task = asyncio.create_task(
            _poll_material_requests(),
            name="erpnext-material-request-poller",
        )
        app.state.mr_polling_task = polling_task
    try:
        yield
    finally:
        if polling_task is not None:
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task
        substitute_task.cancel()
        with suppress(asyncio.CancelledError):
            await substitute_task
        if not purchase_document_task.done():
            purchase_document_task.cancel()
        with suppress(asyncio.CancelledError):
            await purchase_document_task


app = FastAPI(title="SKN31 Purchasing Agent API", lifespan=lifespan)

# SITE_URL(ERPNext 주소)을 CORS 허용 목록에 자동으로 추가함(2026-09-01) -
# MR의 "AI 대체품 확인" Client Script가 ERPNext 페이지 안에서 이 API로
# fetch()를 날리는데, ERPNext 주소가 이 목록에 없으면 브라우저가 CORS로
# 그 요청 자체를 막아버림(배포 위치가 어디든 상관없이 무조건 걸리는
# 문제라 SITE_URL 기준으로 자동 반영되게 함 - .env에 SITE_URL 값이
# 바뀌어도 여기 따로 손댈 필요 없음).
configured_frontend_origins = [
    value.strip().rstrip("/")
    for value in os.getenv("FRONTEND_ORIGINS", "").split(",")
    if value.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        SITE_URL.rstrip("/"),
        *configured_frontend_origins,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Login/refresh remain public. Purchase operations require a valid local session.
app.include_router(auth_router)
app.include_router(
    purchase_router,
    dependencies=[Depends(require_authenticated_user)],
)
app.include_router(
    material_request_router,
    dependencies=[Depends(require_authenticated_user)],
)

# MR 대체품 확인 라우트는 ERPNext Client Script(우리 로그인 세션이 없음)가
# 부르는 거라 require_authenticated_user로 안 막고, 자체 시크릿 헤더
# 검증(mr_substitute_routes.py의 _require_client_script_secret)으로 감.
app.include_router(mr_substitute_router)
app.include_router(
    procurement_router,
    dependencies=[Depends(require_authenticated_user)],
)
app.include_router(erpnext_webhook_router)


@app.get("/api/health")
def health_check():
    """Return API liveness without requiring database or ERP connectivity."""
    polling_task = getattr(app.state, "mr_polling_task", None)
    substitute_task = getattr(app.state, "substitute_polling_task", None)
    purchase_document_task = getattr(app.state, "purchase_document_polling_task", None)
    return {
        "status": "ok",
        "message": "FastAPI server is running properly.",
        "mr_ingest_mode": _mr_ingest_mode(),
        "mr_polling_active": bool(polling_task and not polling_task.done()),
        "substitute_polling_active": bool(
            substitute_task and not substitute_task.done()
        ),
        "purchase_document_reconciliation_active": bool(
            purchase_document_task and not purchase_document_task.done()
        ),
    }


@app.get("/api/erp_test")
def test_erp_connection(
    current_user: CurrentUser,
    item_code: str = "SF-001",
    warehouse: str = "api_test용 - SKN31",
):
    """Exercise the existing ERP client through an authenticated API route."""
    try:
        stock = get_stock_level(item_code, warehouse)
        return {
            "success": True,
            "item_code": item_code,
            "warehouse": warehouse,
            "stock_data": stock,
            "requested_by": current_user["erp_user_id"],
        }
    except ERPNextAPIError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"Unknown error: {exc}"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
