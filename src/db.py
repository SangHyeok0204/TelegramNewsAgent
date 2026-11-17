"""SQLite 스키마/입출력 (M3).

messages 테이블에 방(chat)별 메시지를 idempotent하게 저장한다.
같은 (chat_id, msg_id)는 PRIMARY KEY 충돌로 무시되어 재실행해도 중복이 안 생긴다.
"""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id   INTEGER NOT NULL,   -- 방 id (telethon get_peer_id 정규화: 채널은 -100...)
    msg_id    INTEGER NOT NULL,   -- 방 내 메시지 id
    date_utc  TEXT    NOT NULL,   -- ISO8601 UTC
    sender_id INTEGER,            -- 보낸이 id (채널 글은 보통 NULL)
    text      TEXT,               -- 메시지 본문
    media_file TEXT,              -- 다운로드한 미디어 파일명/상대경로 (없으면 NULL)
    media_type TEXT,              -- photo / document/pdf / webpage (2026-09-10)
    attach_text TEXT,             -- 첨부 PDF 에서 뽑은 텍스트 (2026-09-10)
    url_text   TEXT,              -- 본문 첫 URL 을 따라가 받은 기사 본문 (2026-09-10)
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(chat_id, date_utc);

CREATE TABLE IF NOT EXISTS export_state (
    chat_id       INTEGER PRIMARY KEY,   -- 방 id
    last_msg_id   INTEGER NOT NULL,      -- 마지막으로 CSV에 내보낸 msg_id (증분 경계)
    last_date_utc TEXT                   -- 그 메시지의 업로드 시각(기록용)
);
"""


# 나중에 추가된 컬럼들 — 기존 DB 에 없으면 붙인다(비파괴적, 멱등)
LATE_COLUMNS = {"media_file": "TEXT", "media_type": "TEXT",
                "attach_text": "TEXT", "url_text": "TEXT"}

# upsert 컬럼 순서. 짧은 튜플(백필 등)은 뒤가 NULL 로 채워진다.
INSERT_COLUMNS = ("chat_id", "msg_id", "date_utc", "sender_id", "text",
                  "media_file", "media_type", "attach_text", "url_text")


def _migrate(conn):
    """messages 에 빠진 후발 컬럼을 붙인다. connect() 마다 도는 값싼 점검
    (PRAGMA 1회) — 상주 프로세스가 재기동 없이 새 컬럼을 갖게 하려고 여기 둔다."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if not cols:
        return  # 테이블 자체가 없다 → init_db 가 SCHEMA 로 만든다
    added = [c for c in LATE_COLUMNS if c not in cols]
    for c in added:
        conn.execute(f"ALTER TABLE messages ADD COLUMN {c} {LATE_COLUMNS[c]}")
    if added:
        conn.commit()
    return added


def connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    if own:
        conn.close()


def upsert_messages(conn, rows):
    """rows: (chat_id, msg_id, date_utc, sender_id, text[, media_file, media_type,
    attach_text, url_text]) 들. 중복(PK)은 무시 — 이미 있는 행의 enrichment 를 덮지 않는다."""
    n = len(INSERT_COLUMNS)
    rows = [tuple(r) + (None,) * (n - len(r)) for r in rows]
    conn.executemany(
        f"INSERT OR IGNORE INTO messages ({', '.join(INSERT_COLUMNS)}) "
        f"VALUES ({', '.join('?' * n)})",
        rows,
    )
    conn.commit()
    return conn.total_changes


def get_export_watermark(conn, chat_id):
    """마지막으로 CSV에 내보낸 msg_id. 없으면 0(=처음부터)."""
    row = conn.execute(
        "SELECT last_msg_id FROM export_state WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return row[0] if row else 0


def set_export_watermark(conn, chat_id, last_msg_id, last_date_utc):
    conn.execute(
        "INSERT INTO export_state (chat_id, last_msg_id, last_date_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET "
        "last_msg_id = excluded.last_msg_id, last_date_utc = excluded.last_date_utc",
        (chat_id, last_msg_id, last_date_utc),
    )
    conn.commit()


def set_media_file(conn, chat_id, msg_id, media_file):
    """메시지에 다운로드한 미디어 파일명을 기록한다."""
    conn.execute(
        "UPDATE messages SET media_file = ? WHERE chat_id = ? AND msg_id = ?",
        (media_file, chat_id, msg_id),
    )
    conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"[db] 스키마 초기화 완료 -> {config.DB_PATH}")
