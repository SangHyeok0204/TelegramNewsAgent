@echo off
REM 텔레그램 최초 1회 로그인 (코드·2FA 입력 — 사람 게이트). 세션은 session\tg.session 에 저장된다.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m src.client
pause
endlocal
