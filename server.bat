@echo off
setlocal
cd /d "%~dp0"
title BiddingFlow FastAPI Server

rem SSE처럼 오래 열려 있는 연결이 있어도 Ctrl+C 후 최대 3초만 정상 종료를 기다립니다.
.\.venv\Scripts\python.exe -m uvicorn main:app ^
  --reload ^
  --host 0.0.0.0 ^
  --port 8000 ^
  --timeout-graceful-shutdown 3

endlocal
