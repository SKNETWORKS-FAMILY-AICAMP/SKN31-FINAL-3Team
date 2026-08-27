"""FastAPI entry point for the SKN31 purchasing agent."""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware


# Load secrets before importing ERP and authentication modules.
ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_FILE)

from auth_service.dependencies import CurrentUser, require_authenticated_user
from auth_service.router import router as auth_router
from backend_logic2.erp_client import (
    ERPNextAPIError,
    get_stock_level,
    router as purchase_router,
)


app = FastAPI(title="SKN31 Purchasing Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
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


@app.get("/api/health")
def health_check():
    """Return API liveness without requiring database or ERP connectivity."""
    return {"status": "ok", "message": "FastAPI server is running properly."}


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
