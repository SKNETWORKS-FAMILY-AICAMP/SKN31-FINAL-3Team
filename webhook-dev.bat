@echo off
setlocal
cd /d "%~dp0"
title BiddingFlow Local Webhook Tunnel

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\webhook_dev.ps1"

endlocal
