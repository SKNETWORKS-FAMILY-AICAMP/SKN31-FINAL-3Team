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
from backend_logic2.api.mr_substitute_routes import router as mr_substitute_router
from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    SITE_URL,
    get_stock_level,
    router as purchase_router,
)
from backend_logic2.nodes.mr.reject_material_request import (
    router as material_request_router,
)


app = FastAPI(title="SKN31 Purchasing Agent API")

# SITE_URL(ERPNext 주소)을 CORS 허용 목록에 자동으로 추가함(2026-09-01) -
# MR의 "AI 대체품 확인" Client Script가 ERPNext 페이지 안에서 이 API로
# fetch()를 날리는데, ERPNext 주소가 이 목록에 없으면 브라우저가 CORS로
# 그 요청 자체를 막아버림(배포 위치가 어디든 상관없이 무조건 걸리는
# 문제라 SITE_URL 기준으로 자동 반영되게 함 - .env에 SITE_URL 값이
# 바뀌어도 여기 따로 손댈 필요 없음).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        SITE_URL,
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
