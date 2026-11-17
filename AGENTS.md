# TelegramNewsAgent — 텔레그램 증권가 방 수집 · 토픽 · 대시보드 집계

본인 **개인 Telegram 계정**이 속한 증권가 방(watchlist)의 메시지를 매시 :00·:30 에 SQLite 에 증분 저장하고,
하루 2회(06:55·12:55) Codex CLI 로 토픽 CSV · 대시보드 집계 JSON 을 만든 뒤 **이미 보낸 뉴스를 뺀 새 뉴스만** Slack 카드로 보낸다
(`src/news_dedup.py` + `src/slack_cards.py`, 2026-09-10). 카드는 2026-09-20 부터 세로 목록(section + 썸네일 accessory) — carousel 은 한 화면에 1건만 보여 폐기. 같은 날 owner 결정으로 일간 HTML 리포트는 철회했다 — 산출물은 집계 JSON 과 Slack 카드뿐이다.
MABS 대시보드(TradingDesk)는 그 JSON 을 읽기만 한다. 미로그인 동안에는 정시 작업을 건너뛰어 Codex 비용이 0 이다.
역할 계약(READS/WRITES/ASK/DENY/BUDGET/STOP)은 `AGENT.md`. 이 파일은 기술 결정과 작업 규약.

## 핵심 기술 결정 (고정 — 바꾸지 말 것)

- **수집: Telethon (MTProto user client). Bot API 아님.** 봇은 임의 방의 히스토리를 못 읽는다.
- **인증**: `my.telegram.org` 의 `API_ID`/`API_HASH` + 전화번호. 최초 1회 코드(및 2FA) 입력은 **사람 게이트**.
- **저장**: SQLite `data/db/telegram_bot.db`. 세션은 `session/tg.session` (커밋 금지).
- **LLM**: Codex CLI 를 subprocess 로 (`Codex -p --json-schema`, 토픽·중복판정 sonnet · 집계 opus + `/oh-my-Codex:team`). 이 기기의 로그인 계정을 쓴다. API 키는 선택.
- **실행**: 호스트 상주 `run.bat` (`python -m src.collect --watch --interval 30 --log`), 로그온 시 작업 스케줄러 `TelegramNewsAgent` (2026-09-09; Docker 컨테이너는 같은 날 폐기, compose 서비스 disabled). 세션은 `session/tg.session`.
- **스케줄** (2026-09-14 개편): 수집은 매시 :00·:30 경계 · 청킹 + 최근 24h 집계 + 중복 제거 → 새 뉴스 카드는 **하루 2회 06:55·12:55** (`collect.SEND_TIMES`, 2026-09-10~13 은 매 정시였다). 여기에 2026-09-20 부터 **평일 저녁 미국장 개장 후 카드**(`src/us_open.py`, 개장+85분 = 서머타임 23:55/겨울 00:55, ET 거래일만, 지정 방·sonnet) 가 따로 돈다. 같은 날 재기동이면 가장 최근에 지난 슬롯 하나만 늦게라도 1회 보낸다. 정시 작업은 데몬 스레드에서 돌아 수집 tick(60초)을 막지 않으며, 앞 슬롯이 진행 중이면 이번 슬롯은 건너뛴다. 집계 Codex 타임아웃 25분(한 콜이 두 슬롯에 걸치지 않게). 미인증이면 정시 작업 스킵 + 로그인 경보 1일 1회, 같은 슬롯 3회 실패면 포기 + 실패 경보, 밀린 정시 슬롯은 따라잡지 않음.
- **청킹 단위** (2026-09-14 owner 결정): 기사 링크가 2개 이상인 다이제스트 글(예: 한투 테크팀 Tech Daily)은 `processed.split_digest` 가 **링크 단위로 쪼개** 링크마다 본문을 따로 받아 각각 한 행(msg_id `4686#1`..`4686#7`)으로 토픽·분류를 만든다. DB `messages` 의 게시물 PK 는 그대로다. 링크 0~1개 글은 이전처럼 한 행. 점검: `python -m src.processed --check`.
- **본문 보강** (2026-09-16 owner 결정): 수집 사이클 URL 예산 60→300초(`collect.ENRICH_BUDGET`). `enrich.fetch_url_text` 는 링크 대상이 PDF 면 메모리에서 텍스트를 뽑아 `url_text` 에 넣고(하나·신한 리서치 링크), naver.me 브리지는 안의 URL 을 한 번 더 따라가며, 조선비즈(Fusion JSON)·이데일리(itemprop=articleBody)·wallstreetcn(공개 JSON API) 셀렉터를 둔다. Playwright·브라우저 MCP 는 쓰지 않는다 (상주 프로세스가 사용자 Chrome 을 건드리고 URL 당 Codex 비용이 붙는다). PDF 는 pdf-inspector markdown 을 먼저 쓰고 글자 수가 pypdf 의 90% 미만이거나 앞 800자가 표 조각이면 pypdf 로 폴백(`enrich._inspector_ok`, 22개 실측 9:13). 청킹 입력 `processed.EXTRA_CHARS` 800→2000 · `ROW_CHARS` 1600→3000. 점검: `python -m src.enrich`.
- **새 뉴스 카드 후보** (2026-09-17 owner 결정): 카드가 분류당 1건만 나오는 회차가 있었다 — 집계가 분류당 3+2 만 내놓고 중복 제거가 지난 3일 발송분을 빼면 채울 재료가 없었다(06:55·12:55 창이 18h 겹쳐 같은 이슈가 다시 top 에 뽑힌다). 수정: (1) 집계 프롬프트에 최근 3일 발송 이력(`daily_report._sent_block`, 약 7천 자)을 넣어 처음부터 새 사건을 고르게 유도 — 확률적 유도이고 차단은 여전히 `news_dedup.find_new`. (2) 집계는 분류당 top 5 + notable 3 여유분(최대 24 후보), 카드는 `slack_cards.shown_items` 가 3+2 로 자르고 **보여 준 것만** 발송 이력에 남긴다(잘린 여유분은 다음 회차 후보). 대시보드 JSON 의 top/notable 배열도 5+3 으로 길어진다(구조 동일). 검증: `[뉴스] 후보 24건 · ... 새 뉴스 N건` 로그와 카드 컨텍스트의 `여유분 k건 미표시`. **2026-10-03 owner 결정: 카드도 5+3** (`slack_cards.TOP_N/NOTABLE_N`) — 매 회차 새 뉴스 24건 중 9건이 잘렸고, 구글 선캐처 TPU 위성 발사가 산업 top 5위로 잘린 사례. 최대 24건 · 블록 29개.
- **카드 링크 출처** (2026-10-04 owner 결정): 링크는 사이트 우선순위 없이 언론사·통신사·기업 보도자료 기사를 받는다(프롬프트에 사이트 목록 안 넣음). `daily_report.accept_link` 가 비기사 사이트·로그인 유료 기사(한경 프리미엄9 = `hankyung.com/article/<숫자>i` · 네이버 프리미엄콘텐츠 `contents.premium.naver.com` — 리스트에서도 2026-10-09 삭제)를 버리고, 리스트에 없던 사이트는 URL 이 실제로 열려야 받는다. 리스트 비교는 매체 단위(`daily_report._site` — `www.`·`m.`·`mobile.` 를 뗌, 다른 서브도메인은 별개)라 매체당 한 행이면 된다 (2026-10-09 중복 24행 정리, 241→217행). 받은 새 사이트는 `add_domains` 가 엑셀 끝에 행으로 추가(비고 '자동 추가', 사이트명은 호스트 — 사람이 정리). `ensure_links` 는 빈 링크가 하나라도 있으면 보충 1콜(전엔 0건일 때만). 계기: 10-04 12:55 카드 24건 중 링크 5건.
- **비용**: 시간별 집계 기본값은 Opus + `/team` (`TELEGRAM_BOT__AGG_MODEL`=opus, `TELEGRAM_BOT__AGG_TEAM`=1). 콜당 $5~9, 24회면 하루 $120~200 이고 owner 가 이 비용을 승인했다. 싸게 돌려야 하면 두 환경변수를 `sonnet`·`0` 으로 내린다. 토픽 청킹(`processed`)과 중복 판정(`news_dedup`)에는 `--setting-sources "" --strict-mcp-config` 를 준다 — 이 기기 AGENTS.md·설정·MCP 가 프롬프트에 안 실려 호출당 고정비 약 $0.13 이 빠진다(실측 $0.2051→$0.0710, $0.4637→$0.0438). 집계는 `/team` 이 OMC 플러그인을 쓰므로 이 플래그를 주지 않는다.

## 디렉터리 (실제)

```
TelegramNewsAgent/
├─ AGENT.md · AGENTS.md · README.md
├─ requirements.txt
├─ src/            config · client · channels · db · backfill · collect · export_csv · processed · daily_report · news_dedup · slack_cards · media(보존)
├─ docs/db-schema.md
├─ data/
│   ├─ input/  watchlist.csv · raw/*.csv · processed/*.csv
│   ├─ db/     telegram_bot.db
│   ├─ output/ dashboard_analysis.json (· 텔레그램모니터_YYMMDD.html 은 2026-09-10 철회 전 기록)
│   └─ cache/  collect_log.txt · task_state.json · sent_news.jsonl(발송 이력 3일)
└─ run.bat · login.bat · session/tg.session   (호스트 상주 · 로그인, 2026-09-09)
```

## 보안 (절대 규칙)

- 비밀값은 공용 `Agents/.env` (2026-09-10). `*.session*`, `*.db`, `*.csv` 는 커밋 금지 (`.gitignore`). 세션 파일 = 로그인된 계정 그 자체.
- 대량 백필은 flood-wait / 계정 제한 리스크. 방간 sleep 2s, 개인 사용 한도 내.

## 실행 명령

```
cd C:\Users\adg01\Desktop\parksanghyeok\Agents\TelegramNewsAgent
login.bat                                   # 최초 1회 로그인 (코드 입력)
schtasks /run /tn TelegramNewsAgent              # 상주 시작 (로그온 시 자동)
.venv\Scripts\python -m src.backfill --list-dialogs      # 대화방 목록
.venv\Scripts\python -m src.backfill --all --days 7      # 새 방 과거 적재
```

## 작업 규약

- 소스는 이 폴더에서 고친다. 고친 뒤에는 `schtasks /end` → `/run` 으로 재시작. **단, `/end` 는 cmd 창만 닫고 python 은 살아남는다** (2026-09-16 실측: 전날 뜬 collect 가 그대로 남아 새 인스턴스가 포트 잠금에 걸려 즉시 종료). 재시작 전에 `Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'src\.collect --watch' } | % { Stop-Process -Id $_.ProcessId -Force }` 로 먼저 내린다 (`.venv\Scripts\python.exe` 는 런처 스텁이라 시스템 python 자식이 하나 더 보이는데 같은 인스턴스다).
- 경로를 코드에 하드코딩하지 않는다. `config.py` 의 환경변수(`TELEGRAM_BOT__DATA_ROOT`, `TELEGRAM_BOT__SESSION`, `TELEGRAM_BOT__HEARTBEAT_URL`)로만 바꾼다.
- 산출물 JSON 구조를 바꾸면 MABS `apps/api/app/api/telegram_news.py` 와 같은 커밋에서 맞춘다.
- 사람 게이트(로그인/2FA, 대량 백필, DB 초기화)에서는 멈추고 보고한다.
- 사람이 준 교정은 `AGENT.md` ANTI-PATTERNS 로 승격한다.
