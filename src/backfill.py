"""히스토리 백필 (M2 대화방 나열 + M4 단일 방 백필).

사용:
    python -m src.backfill --list-dialogs                       # 대화방 목록
    python -m src.backfill --chat -1002471352838 --days 7       # 최근 7일 백필
"""
import argparse
import time
from datetime import datetime, timedelta, timezone

from telethon import utils
from telethon.errors import FloodWaitError

from . import channels, config, db, media
from .client import make_client


def _parse_chat(s):
    """'-1002471352838' 같은 숫자면 int, 아니면 username 문자열로."""
    return int(s) if s.lstrip("-").isdigit() else s


def list_dialogs():
    """로그인된 계정이 속한 모든 대화방(그룹/채널/DM)을 콘솔에 출력한다."""
    client = make_client()
    client.connect()
    if not client.is_user_authorized():
        raise SystemExit(
            "[backfill] 미로그인 상태입니다. 먼저 `python -m src.client`로 로그인하세요."
        )
    print(f"{'id':>15}  {'type':<8}  title")
    print("-" * 60)
    n = 0
    for d in client.iter_dialogs():
        kind = "channel" if d.is_channel else "group" if d.is_group else "user"
        print(f"{d.id:>15}  {kind:<8}  {d.name}")
        n += 1
    print("-" * 60)
    print(f"총 {n}개 대화방")
    client.disconnect()


def backfill(chat, days, media_dl=False):
    """특정 방의 최근 `days`일 메시지를 messages.db에 저장한다(중복 무시).
    media_dl=True면 사진도 MEDIA_DIR에 다운로드한다."""
    client = make_client()
    client.connect()
    if not client.is_user_authorized():
        raise SystemExit(
            "[backfill] 미로그인 상태입니다. 먼저 `python -m src.client`로 로그인하세요."
        )
    entity = client.get_entity(chat)
    chat_id = utils.get_peer_id(entity)  # dialog.id와 같은 정규화 형태(-100...)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    conn = db.connect()
    db.init_db(conn)
    rows = []
    msgs = []
    # iter_messages는 최신 -> 과거 순. cutoff보다 오래되면 중단.
    for msg in client.iter_messages(entity):
        if msg.date < cutoff:
            break
        rows.append((chat_id, msg.id, msg.date.isoformat(), msg.sender_id, msg.message or ""))
        msgs.append(msg)
    inserted = db.upsert_messages(conn, rows)
    dl = ptot = 0
    if media_dl:
        dl, ptot = media.download_photos(client, chat_id, conn, msgs)
    conn.close()
    client.disconnect()

    title = getattr(entity, "title", chat_id)
    print(f"[backfill] '{title}' 최근 {days}일: 수집 {len(rows)}건 / 신규 저장 {inserted}건")
    if media_dl:
        print(f"           사진 {ptot}건 중 {dl}건 새로 다운로드 -> {config.MEDIA_DIR}")
    print(f"           chat_id={chat_id} -> {config.DB_PATH}")


def backfill_all(days):
    """channels(watchlist) 전체를 한 세션으로 순차 백필(방간 sleep, flood 처리)."""
    client = make_client()
    client.connect()
    if not client.is_user_authorized():
        raise SystemExit(
            "[backfill] 미로그인 상태입니다. 먼저 `python -m src.client`로 로그인하세요."
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    conn = db.connect()
    db.init_db(conn)
    chans = channels.CHANNELS
    for i, ch in enumerate(chans):
        try:
            entity = client.get_entity(ch["id"])
            chat_id = utils.get_peer_id(entity)
            rows = []
            for msg in client.iter_messages(entity):
                if msg.date < cutoff:
                    break
                rows.append((chat_id, msg.id, msg.date.isoformat(), msg.sender_id, msg.message or ""))
            db.upsert_messages(conn, rows)
            print(f"[backfill] {ch['alias']}: {len(rows)}건")
        except FloodWaitError as e:
            print(f"[backfill] flood-wait {e.seconds}s @ {ch['alias']} — 대기")
            time.sleep(e.seconds)
        except Exception as e:
            print(f"[backfill] ERROR @ {ch['alias']}: {e}")
        if i < len(chans) - 1:
            time.sleep(2)
    conn.close()
    client.disconnect()
    print(f"[backfill] 전체 완료 — {len(chans)}개 방 (최근 {days}일)")


def main():
    ap = argparse.ArgumentParser(prog="src.backfill")
    ap.add_argument("--list-dialogs", action="store_true", help="대화방 목록만 출력")
    ap.add_argument("--chat", help="백필할 방 id(-100...) 또는 username")
    ap.add_argument("--days", type=int, default=7, help="최근 며칠치 (기본 7)")
    ap.add_argument("--all", action="store_true", help="watchlist 전체 방 백필")
    ap.add_argument("--media", action="store_true", help="사진 미디어도 다운로드")
    args = ap.parse_args()

    if args.list_dialogs:
        list_dialogs()
    elif args.all:
        backfill_all(args.days)
    elif args.chat:
        backfill(_parse_chat(args.chat), args.days, args.media)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
