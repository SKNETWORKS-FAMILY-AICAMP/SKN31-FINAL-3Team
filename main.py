import os
import secrets
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# .env 로드
ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_FILE)

def get_or_create_jwt_secret() -> str:
    """
    Supabase와 동일한 사양(256-bit / 64 hex characters)의 JWT Secret Key를 
    .env에서 가져오거나, 없으면 자동 생성하여 .env에 영구 보관합니다.
    """
    secret = os.environ.get("JWT_SECRET")
    if not secret or len(secret) < 32:
        # Supabase 표준: 256-bit 엔트로피를 가진 64자리 16진수 보안 키 생성
        secret = secrets.token_hex(32)
        os.environ["JWT_SECRET"] = secret
        try:
            if not ENV_FILE.exists():
                ENV_FILE.touch()
            with open(ENV_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n# Auto-generated Supabase Standard JWT Secret\nJWT_SECRET={secret}\n")
        except Exception:
            pass
    return secret

# 백엔드 로직 연동용 임포트 (예시)
from backend_logic2.erp_client import get_stock_level, ERPNextAPIError,router as purchase_router

app = FastAPI(title="SKN31 Purchasing Agent API")

app.include_router(purchase_router)

# 1. CORS 설정 (프론트엔드 개발 환경 허용)
origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. 테스트 API 라우트
@app.get("/api/health")
def health_check():
    """서버 상태 확인용 API"""
    return {"status": "ok", "message": "FastAPI server is running properly."}

from pydantic import BaseModel
import requests
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Header, HTTPException
from urllib.parse import quote

SECRET_KEY = get_or_create_jwt_secret()
ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

def get_erp_user_profile(erp_base_url: str, erp_sid: str, user_id: str) -> dict:
    """현재 ERPNext 세션의 User 문서에서 화면 표시용 계정 정보를 조회합니다."""
    fallback = {
        "id": user_id,
        "email": user_id if "@" in user_id else "",
        "username": user_id,
        "full_name": user_id,
        "user_type": "System User",
    }

    try:
        response = requests.get(
            f"{erp_base_url}/api/resource/User/{quote(user_id, safe='')}",
            params={"fields": '["name","full_name","first_name","last_name","email","username","user_type"]'},
            cookies={"sid": erp_sid},
            timeout=10,
        )
        if response.status_code != 200:
            return fallback

        user = response.json().get("data") or {}
        composed_name = " ".join(
            part.strip() for part in (user.get("first_name"), user.get("last_name"))
            if isinstance(part, str) and part.strip()
        )
        return {
            "id": user.get("name") or user_id,
            "email": user.get("email") or fallback["email"],
            "username": user.get("username") or user.get("name") or user_id,
            "full_name": user.get("full_name") or composed_name or user.get("name") or user_id,
            "user_type": user.get("user_type") or fallback["user_type"],
        }
    except (requests.RequestException, ValueError, TypeError):
        # 프로필 조회 실패가 이미 성공한 ERPNext 로그인을 막아서는 안 됩니다.
        return fallback


def create_supabase_token(
    user_email: str,
    erp_sid: str,
    expires_delta: timedelta,
    token_type: str = "access_token",
    user_profile: dict | None = None,
):
    """
    Supabase GoTrue JWT 사양을 완벽히 준수하는 토큰 생성 함수
    """
    now = datetime.now(timezone.utc)
    expire = now + expires_delta
    profile = user_profile or {
        "id": user_email,
        "email": user_email if "@" in user_email else "",
        "username": user_email,
        "full_name": user_email,
        "user_type": "System User",
    }
    payload = {
        "iss": "biddingflow-auth",
        "sub": user_email,
        "aud": "authenticated",
        "role": "authenticated",
        "email": profile.get("email") or user_email,
        "name": profile.get("full_name") or user_email,
        "app_metadata": {
            "provider": "erpnext",
            "providers": ["erpnext"]
        },
        "user_metadata": {
            "email": profile.get("email") or user_email,
            "erp_sid": erp_sid,
            "erp_user_id": profile.get("id") or user_email,
            "username": profile.get("username") or user_email,
            "full_name": profile.get("full_name") or user_email,
            "user_type": profile.get("user_type") or "System User",
        },
        "erp_sid": erp_sid,
        "token_type": token_type,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp())
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

class LoginRequest(BaseModel):
    email: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str

@app.post("/api/login")
def login_to_erp(req: LoginRequest):
    """
    프론트엔드에서 받은 ID/PW를 실제 ERPNext 서버로 넘겨 인증하고, 
    성공 시 Access Token과 Refresh Token을 반환하는 엔드포인트
    """
    ERP_BASE_URL = os.environ.get("ERPNEXT_BASE_URL", "http://13.209.103.102:8080")
    
    try:
        response = requests.post(
            f"{ERP_BASE_URL}/api/method/login",
            data={"usr": req.email, "pwd": req.password},
            timeout=10
        )
        
        if response.status_code == 200:
            cookies = response.cookies.get_dict()
            sid = cookies.get("sid")
            
            if sid:
                user_profile = get_erp_user_profile(ERP_BASE_URL, sid, req.email)
                # Supabase GoTrue Auth 사양 토큰 발급
                access_token = create_supabase_token(req.email, sid, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES), token_type="access_token", user_profile=user_profile)
                refresh_token = create_supabase_token(req.email, sid, timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS), token_type="refresh_token", user_profile=user_profile)
                
                return {
                    "success": True, 
                    "message": "로그인 성공", 
                    "access_token": access_token,
                    "token_type": "bearer",
                    "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                    "refresh_token": refresh_token,
                    "user": {
                        "id": user_profile["id"],
                        "aud": "authenticated",
                        "role": "authenticated",
                        "email": user_profile["email"],
                        "username": user_profile["username"],
                        "full_name": user_profile["full_name"],
                        "user_type": user_profile["user_type"],
                        "app_metadata": {"provider": "erpnext", "providers": ["erpnext"]},
                        "user_metadata": {
                            "email": user_profile["email"],
                            "erp_user_id": user_profile["id"],
                            "username": user_profile["username"],
                            "full_name": user_profile["full_name"],
                            "user_type": user_profile["user_type"],
                        }
                    }
                }
            else:
                return {"success": False, "message": "ERP 로그인 성공했으나 sid 쿠키를 받지 못했습니다."}
        else:
            return {"success": False, "message": "아이디나 비밀번호가 올바르지 않습니다."}
            
    except requests.RequestException as e:
        return {"success": False, "message": f"ERP 서버 연결 오류: {str(e)}"}

@app.post("/api/refresh")
def refresh_token(req: RefreshRequest):
    """
    만료된 Access Token 대신 Refresh Token을 받아 검증하고
    새로운 Supabase 표준 Access Token을 발급하는 엔드포인트
    """
    try:
        # 리프레시 토큰 검증
        payload = jwt.decode(req.refresh_token, SECRET_KEY, algorithms=[ALGORITHM], audience="authenticated")
        metadata = payload.get("user_metadata") or {}
        erp_user_id = payload.get("sub") or metadata.get("erp_user_id")
        email = payload.get("email") or metadata.get("email") or erp_user_id
        sid = payload.get("erp_sid") or metadata.get("erp_sid")
        
        if erp_user_id is None or sid is None:
            raise HTTPException(status_code=401, detail="유효하지 않은 토큰 페이로드입니다.")
            
        # 새 Access Token 발급
        user_profile = {
            "id": metadata.get("erp_user_id") or erp_user_id,
            "email": metadata.get("email") or email,
            "username": metadata.get("username") or erp_user_id,
            "full_name": metadata.get("full_name") or payload.get("name") or email,
            "user_type": metadata.get("user_type") or "System User",
        }
        new_access_token = create_supabase_token(erp_user_id, sid, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES), token_type="access_token", user_profile=user_profile)
        
        return {
            "success": True,
            "access_token": new_access_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh Token이 만료되었습니다. 다시 로그인해주세요.")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"유효하지 않은 Refresh Token입니다: {str(e)}")

@app.get("/api/erp_test")
def test_erp_connection(item_code: str = "SF-001", warehouse: str = "api_test용 - SKN31"):
    """
    기존에 작성된 erp_client.py가 정상 작동하는지 FastAPI 위에서 테스트합니다.
    (주의: .env 파일에 API_KEY 등이 세팅되어 있어야 합니다)
    """
    try:
        stock = get_stock_level(item_code, warehouse)
        return {
            "success": True,
            "item_code": item_code,
            "warehouse": warehouse,
            "stock_data": stock
        }
    except ERPNextAPIError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Unknown error: {str(e)}"}


# 4. (배포 시) 루트 경로에서 React index.html 서빙 예시
# @app.get("/", response_class=HTMLResponse)
# async def serve_react_app(request: Request):
#     return templates.TemplateResponse("index.html", {"request": request})

if __name__ == "__main__":
    import uvicorn
    # 기본적으로 8000 포트에서 실행
    uvicorn.run("main:app", host="0.0.0.1", port=8000, reload=True)


app.include_router(purchase_router)