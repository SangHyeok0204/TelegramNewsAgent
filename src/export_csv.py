"""messages.db -> 방별 CSV 내보내기.

각 행이 메시지 1건. 시각은 한국시간(KST), 일자순. 엑셀 호환 UTF-8(BOM).
text 원문은 DB(messages.text)에만 두고 CSV 에는 넣지 않는다(2026-09-08, 중복 저장 제거).
메시지에 포함된 URL만 url 컬럼으로 추출한다.
파일명만 주면 raw 디렉터리(config.RAW_DIR)에 저장된다.

사용:
    python -m src.export_csv --chat -1002471352838 --out 미국주식인사이더.csv
"""
import argparse
import csv
import os
import re
from datetime import datetime, timedelta, timezone

from . import config, db

KST = timezone(timedelta(hours=9))
URL_RE = re.compile(r"https?://\S+")
COLUMNS = ["date", "time", "msg_id", "url"]


def _parse_chat(s):
    return int(s) if s.lstrip("-").isdigit() else s


def resolve_out_path(out_path):
    """파일명만 주어지면 raw 디렉터리(config.RAW_DIR)에 배치한다.
    디렉터리가 포함된 경로면 그대로 사용한다."""
    if os.path.dirname(out_path):
        return out_path
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.RAW_DIR / out_path)


def _extract_urls(text):
    """text에서 http(s) URL을 추출해 공백으로 연결(없으면 '')."""
    if not text:
        return ""
    return " ".join(URL_RE.findall(text))


def _to_row(date_utc, msg_id, text):
    text = text or ""
    dt = datetime.fromisoformat(date_utc).astimezone(KST)
    return [dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), msg_id, _extract_urls(text)]


def export(chat_id, out_path):
    """방의 전체 메시지를 새 CSV로 내보낸다(덮어쓰기)."""
    out_path = resolve_out_path(out_path)
    conn = db.connect()
    rows = conn.execute(
        "SELECT date_utc, msg_id, text FROM messages WHERE chat_id = ? ORDER BY date_utc",
        (chat_id,),
    ).fetchall()
    conn.close()
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for date_utc, msg_id, text in rows:
            w.writerow(_to_row(date_utc, msg_id, text))
    print(f"[export] {len(rows)}건 -> {out_path}")


def append_increment(conn, chat_id, out_path):
    """export_state 워터마크 이후 새 메시지만 out_path에 append하고 워터마크를 갱신한다.

    반환: (추가 건수, 마지막 수집 KST 시각 문자열 또는 None)
    """
    out_path = resolve_out_path(out_path)
    last = db.get_export_watermark(conn, chat_id)
    rows = conn.execute(
        "SELECT date_utc, msg_id, text FROM messages "
        "WHERE chat_id = ? AND msg_id > ? ORDER BY msg_id",
        (chat_id, last),
    ).fetchall()
    if not rows:
        return 0, None

    new_file = not os.path.exists(out_path)
    # 새 파일에만 BOM(utf-8-sig)+헤더. append 시엔 BOM 없이(파일 중간 BOM 방지).
    enc = "utf-8-sig" if new_file else "utf-8"
    with open(out_path, "a", newline="", encoding=enc) as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)
        for date_utc, msg_id, text in rows:
            w.writerow(_to_row(date_utc, msg_id, text))

    last_date_utc, last_msg_id = rows[-1][0], rows[-1][1]
    db.set_export_watermark(conn, chat_id, last_msg_id, last_date_utc)
    last_kst = datetime.fromisoformat(last_date_utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    return len(rows), last_kst


def main():
    ap = argparse.ArgumentParser(prog="src.export_csv")
    ap.add_argument("--chat", required=True, help="내보낼 방 id(-100...) 또는 username")
    ap.add_argument("--out", default="export.csv", help="출력 CSV (파일명만 주면 raw 폴더)")
    args = ap.parse_args()
    export(_parse_chat(args.chat), args.out)


if __name__ == "__main__":
    main()
