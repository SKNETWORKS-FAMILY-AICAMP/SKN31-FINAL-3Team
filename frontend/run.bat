@echo off
setlocal

rem Always run from this batch file's directory, even when launched elsewhere.
cd /d "%~dp0"

where npm >nul 2>&1
if errorlevel 1 (
    echo [NextERP Frontend] npm was not found.
    echo Install Node.js and run this file again.
    pause
    exit /b 1
)

echo [NextERP Frontend] Installing frontend dependencies...
call npm install
if errorlevel 1 (
    echo [NextERP Frontend] npm install failed. The development server was not started.
    pause
    exit /b 1
)

echo [NextERP Frontend] Starting the Vite development server...
call npm run dev -- --open
set "RUN_EXIT_CODE=%errorlevel%"

if not "%RUN_EXIT_CODE%"=="0" (
    echo [NextERP Frontend] The development server exited with an error.
    pause
)

endlocal & exit /b %RUN_EXIT_CODE%
