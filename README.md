# TelegramNewsAgent

본인 Telegram 계정이 속한 증권가 방의 메시지를 매시 :00·:30 에 수집해 SQLite 에 쌓고, 매 정시에 claude CLI 로 토픽·대시보드 집계 JSON 을 만든다.
MABS 대시보드(`C:\Users\adg01\Desktop\MABS`, TradingDesk 메뉴)가 그 JSON 을 카드로 보여 준다.
Slack 에는 **최근 3일간 보낸 뉴스와 겹치지 않는 새 뉴스만** 매 정시에 보낸다 (그 밖에 로그인 필요 · 작업 3회 실패 카드, `src/news_dedup.py`·`src/slack_cards.py` — 2026-09-10).
일간 HTML 리포트(`텔레그램모니터_YYMMDD.html`)는 2026-09-10 owner 결정으로 철회했다. 지난 파일은 `data/output/` 에 남아 있다.

> Bot API 로는 개인계정 방 히스토리를 못 읽는다. **Telethon(MTProto user client)** 사용. 본인 계정·본인 데이터 보관 용도.

## 사전 준비 (사람)

1. `my.telegram.org` → API development tools → `API_ID` / `API_HASH` 발급
2. 공용 `Agents/.env` 에 `TELEGRAM_BOT__TG_API_ID`, `TELEGRAM_BOT__TG_API_HASH`, `TELEGRAM_BOT__TG_PHONE` 채우기 (`Agents/.env.example` 참고)

## 실행 (호스트, 2026-09-09 —)

```
cd C:\Users\adg01\Desktop\parksanghyeok\Agents\TelegramNewsAgent
login.bat                        # 최초 1회 로그인 코드 입력 → session\tg.session
schtasks /run /tn TelegramNewsAgent   # 상주 시작 (로그온 시 작업 스케줄러가 자동으로 띄운다)
```

이 폴더가 에이전트 본체다. `run.bat` 이 `.venv` 로 `src.collect --watch` 를 띄우고, MABS(`http://localhost:3100/trading-desk`)는 `data/` 를 읽기 전용으로 마운트해 산출물만 본다.
소스를 고치면 `schtasks /end /tn TelegramNewsAgent` → `/run` 으로 재시작. 컨테이너 시절 `Dockerfile`·`entrypoint.sh` 는 2026-10-01 삭제(휴지통).

## 문서

- `AGENT.md` — 역할 계약 (READS/WRITES/ASK/DENY/BUDGET/STOP/ANTI-PATTERNS), 파일 역할, 스케줄, 대시보드 계약
- `CLAUDE.md` — 기술 결정, 디렉터리, 보안, 작업 규약
- `docs/db-schema.md` — DB 스키마
- 2026-06~07 빌드 루프 이력(STATE.md · LOOP.md · loop-*.md · 옛 *.bat · 파이프라인 보고서 html)은 2026-09-09 정리(휴지통). 필요하면 git 이력이나 휴지통에서

## 저장소 구성 (공개본)

- 키·세션·수집 데이터(`.env`, `session/`, `data/`)는 올리지 않았다. 필요한 키 이름은 `.env.example` 참고.
- `slack_notify.py` 는 원래 상위 `Agents/` 폴더에서 여러 에이전트가 같이 쓰는 공용 Slack 발송 모듈이다. 참고용으로 함께 올렸다.
