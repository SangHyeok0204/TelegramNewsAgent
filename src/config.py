"""환경설정 로딩 — 중앙 vault(.env)에서 비밀값을, NEW_ROOT 이름앵커로 경로를 계산한다.

- 비밀값(TG_API_ID/HASH/PHONE)은 중앙 vault data/_secrets/.env 의
  TELEGRAM_BOT__* 키에서 읽는다(하위호환으로 구 TG_* 도 fallback).
- 데이터/세션/로그 경로는 코드에 하드코딩하지 않고 NEW_ROOT 기준으로 계산한다.
"""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# 분류 2단계 중첩(모니터링\실시간 모니터링\텔레그램모니터)이라 깊이 가변 →
# 폴더 이름 앵커로 raw 루트를 찾는다.
PROJECT_DIR = Path(__file__).resolve().parents[1]
NEW_ROOT = next((p for p in PROJECT_DIR.parents if p.name == "raw"), None)

if NEW_ROOT:
    # 회사 PC: 중앙 vault 에서 비밀값 로드
    load_dotenv(NEW_ROOT / "data" / "_비밀값(중요)" / ".env")
else:
    # 단독(standalone) PC: raw 앵커 없음 → Agents/.env (공용, 2026-09-10). 이미 있는 환경변수가 우선.
    load_dotenv(PROJECT_DIR.parent / ".env")


def _env(*names, default=""):
    """여러 키를 우선순위대로 조회(앞이 우선). 신규 TELEGRAM_BOT__* → 구 TG_* fallback."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


API_ID = _env("TELEGRAM_BOT__TG_API_ID", "TG_API_ID")
API_HASH = _env("TELEGRAM_BOT__TG_API_HASH", "TG_API_HASH")
PHONE = _env("TELEGRAM_BOT__TG_PHONE", "TG_PHONE")

# 경로(비밀 아님) — NEW_ROOT 기준 중앙 data 레이아웃
if NEW_ROOT:
    DATA_ROOT = NEW_ROOT / "data" / "텔레그램모니터"
    # telethon 세션(민감) → 중앙 _secrets
    SESSION = str(NEW_ROOT / "data" / "_비밀값(중요)" / "telegram_bot" / "tg.session")
else:
    # standalone: 기본은 에이전트 폴더의 data/ 와 session/tg.session (2026-09-10, 경로를 .env 에 두지 않는다).
    # 환경변수 TELEGRAM_BOT__DATA_ROOT · TELEGRAM_BOT__SESSION 이 있으면 그것이 우선(컨테이너용).
    DATA_ROOT = Path(os.getenv("TELEGRAM_BOT__DATA_ROOT") or PROJECT_DIR / "data")
    SESSION = os.getenv("TELEGRAM_BOT__SESSION") or str(PROJECT_DIR / "session" / "tg.session")
Path(SESSION).parent.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_ROOT / "db" / "telegram_bot.db")
(DATA_ROOT / "db").mkdir(parents=True, exist_ok=True)
# 수집 입력 데이터: raw(방별 CSV, 실시간 누적) / processed(분석 결과, 추후)
INPUT_DIR = DATA_ROOT / "input"
RAW_DIR = INPUT_DIR / "raw"
PROCESSED_DIR = INPUT_DIR / "processed"
# 과거 수집 이미지 보존용 (현재 이미지 수집은 중단)
MEDIA_DIR = INPUT_DIR / "media"
# 산출물(일간 리포트 등) — 규약 5.1 output
OUTPUT_DIR = DATA_ROOT / "output"
# 런타임 로그
LOG_PATH = str(DATA_ROOT / "cache" / "collect_log.txt")
# claude CLI 실행 파일. Windows 호스트에서는 npm 이 claude.cmd 로 깔아서 subprocess(["claude", …]) 가
# WinError 2 (파일을 찾을 수 없습니다) 로 죽는다 (2026-09-10 실측: 청크 전부 실패). PATHEXT 를 보는 which 로 해결.
CLAUDE_EXE = shutil.which("claude") or "claude"


if __name__ == "__main__":
    # 동작 확인: 로딩된 설정 요약 (비밀값은 마스킹)
    def mask(v):
        return (v[:2] + "***") if v else "(비어있음)"

    print("[config] 중앙 vault 로딩 확인")
    print(f"  NEW_ROOT    = {NEW_ROOT}")
    print(f"  TG_API_ID   = {mask(API_ID)}")
    print(f"  TG_API_HASH = {mask(API_HASH)}")
    print(f"  TG_PHONE    = {mask(PHONE)}")
    print(f"  SESSION     = {SESSION}")
    print(f"  DB_PATH     = {DB_PATH}")
    print(f"  RAW_DIR     = {RAW_DIR}")
    print(f"  PROCESSED   = {PROCESSED_DIR}")
    print(f"  LOG_PATH    = {LOG_PATH}")
