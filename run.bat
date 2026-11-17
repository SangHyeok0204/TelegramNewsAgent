@echo off
REM TelegramNewsAgent 데스크탑 실행 (2026-09-09). 컨테이너 대신 호스트 상주 — TokenUsageAgent·YonseiAgent 와 같은 방식.
REM 로그온 시 자동 실행(작업 스케줄러 TelegramNewsAgent). 수동 실행도 가능. 창은 최소화로 떠 있어야 한다.
REM 최초 1회 로그인(사람 게이트): login.bat
REM 주의: chcp 65001 아래에는 ASCII 만 쓴다 — 한글 줄이 있으면 배치 파서가 깨져 set 이 무시된다 (2026-09-10)
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
REM hourly aggregation uses /oh-my-claudecode:team inside claude -p (needs this env)
set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
".venv\Scripts\python.exe" -m src.collect --watch --interval 30 --log
endlocal
