# DB 스키마

SQLite `data/db/telegram_bot.db`. 정본은 `src/db.py` 의 `SCHEMA` + `LATE_COLUMNS` 다.
아래 `dialogs` 는 M3 초안이고 실제로는 만들지 않았다 (방 목록은 `data/input/watchlist.csv`,
증분 경계는 `MAX(msg_id)` 와 `export_state` 가 대신한다).

## messages (실제)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| chat_id | INTEGER | 방 id (telethon `get_peer_id`, 채널은 -100…) |
| msg_id | INTEGER | 방 내 메시지 id |
| date_utc | TEXT | ISO8601 (UTC) |
| sender_id | INTEGER NULL | 발신자 (채널 글은 보통 NULL) |
| text | TEXT NULL | 본문 |
| media_file | TEXT NULL | 받아 둔 파일. 사진은 파일명, PDF 는 `input/media/<chat_id>/<msg_id>.pdf` 상대경로 |
| media_type | TEXT NULL | `photo` / `document/<subtype>` / `webpage` (2026-09-10) |
| attach_text | TEXT NULL | 첨부 PDF 에서 뽑은 텍스트, 앞 8쪽·4,000자 (2026-09-10) |
| url_text | TEXT NULL | 본문 첫 URL 을 따라가 받은 기사 본문, 1,500자 (2026-09-10) |

- **PRIMARY KEY (chat_id, msg_id)** → 중복 방지. upsert 는 `INSERT OR IGNORE` 라
  이미 있는 행의 보강 컬럼을 덮어쓰지 않는다.
- 인덱스: `idx_messages_date (chat_id, date_utc)`.
- 후발 컬럼은 `db._migrate()` 가 `connect()` 마다 `PRAGMA table_info` 로 확인해 붙인다(멱등).
  상주 프로세스는 재기동 없이 다음 `connect()` 에서 새 컬럼을 갖는다.

## export_state (CSV 증분 경계)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| chat_id | INTEGER PK | 방 id |
| last_msg_id | INTEGER | 마지막으로 CSV 에 내보낸 msg_id |
| last_date_utc | TEXT NULL | 그 메시지 시각 (기록용) |

## 증분 규칙

- 수집: 방별 `MAX(msg_id)` 이후만 (`iter_messages(min_id=…)`).
- CSV: `export_state.last_msg_id` 이후만 append.
- 보강(`url_text`·`attach_text`)은 수집 시 1회. 사이클 예산(120초)에 걸려 비면
  `python -m src.collect --enrich-missing` 이 최근 7일 분을 나중에 채운다.
