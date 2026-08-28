# NextERP 인증·세션 설정

이 프로젝트의 로그인은 ERPNext 계정으로 신원을 확인하고, 이후 API 세션은
PostgreSQL의 `auth` 스키마에서 관리한다.

## 저장 구조

- Access Token: 15분짜리 JWT
- Refresh Token: 브라우저에만 전달되는 불투명 문자열
- Refresh Token DB 값: SHA-256 해시
- ERPNext `sid`: 서버 DB에 암호화하여 저장
- JWT와 프론트 응답에는 ERPNext `sid`를 포함하지 않음

## 1. 패키지 설치

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. 환경변수 준비

`.env.example`을 참고하여 기존 `.env`에 다음 값을 추가한다.

```dotenv
DATABASE_URL=postgresql://nexterp:비밀번호@13.209.103.102:15432/nexterp
JWT_SECRET=충분히_긴_랜덤값
SESSION_ENCRYPTION_KEY=JWT_SECRET과_다른_랜덤값
```

비밀번호에 `@`, `:`, `/`, `#` 같은 문자가 포함되면 URL 인코딩해야 한다.
실제 `.env`는 Git에 커밋하지 않는다.

랜덤 키 생성 예시:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

명령을 두 번 실행해 `JWT_SECRET`과 `SESSION_ENCRYPTION_KEY`에 서로 다른 값을
사용한다. 운영 중 키를 바꾸면 기존 JWT 또는 암호화된 ERP 세션을 사용할 수
없으므로 배포 환경에서는 키를 고정한다.

## 3. PostgreSQL 마이그레이션

프로젝트 루트에서 실행한다.

```powershell
.\.venv\Scripts\python.exe -m auth_service.migrate
```

최초 실행 시 다음 테이블이 생성된다.

```text
auth.users
auth.sessions
auth.refresh_tokens
public.schema_migrations
```

동일 명령을 다시 실행해도 이미 적용된 마이그레이션은 건너뛴다.

현재 개발 단계에서는 기존 `nexterp` 계정으로 마이그레이션할 수 있다. 실제
운영 단계에서는 DDL용 계정과 FastAPI 실행 계정을 분리하는 것이 권장된다.

## 4. 생성 결과 확인

PostgreSQL에 접속하여 확인한다.

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema IN ('auth', 'public')
  AND table_name IN ('users', 'sessions', 'refresh_tokens', 'schema_migrations')
ORDER BY table_schema, table_name;
```

Refresh Token 원문이나 ERPNext `sid` 평문이 저장되지 않는지도 확인한다.

```sql
SELECT id, session_id, length(token_hash) AS hash_length, used_at, revoked_at
FROM auth.refresh_tokens
ORDER BY issued_at DESC
LIMIT 5;
```

`hash_length`는 64여야 한다.

## 5. API 실행 및 확인

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

구현된 인증 API:

```text
POST /api/login
POST /api/refresh
POST /api/logout
GET  /api/me
```

`/api/health`만 인증 없이 호출할 수 있다. `/api/erp_test`와 `/purchase/*`는
유효한 Access Token과 활성 DB 세션이 필요하다.

## 6. 기존 토큰 주의

기존 구현에서 발급한 JWT 형식의 Refresh Token은 새 DB에서 찾을 수 없으므로
마이그레이션 적용 후 사용자는 한 번 다시 로그인해야 한다.

## 아직 적용하지 않은 항목

- Refresh Token을 HttpOnly/Secure Cookie로 이전
- ERPNext 세션 만료 여부를 Refresh 시점에 재검증
- 세션별 기기 목록과 전체 로그아웃
- 비대칭 JWT 서명과 JWKS

현재 개발 환경에서는 JSON 응답과 `localStorage` 방식을 유지한다.
