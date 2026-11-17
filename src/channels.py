"""수집 대상 방 목록 — input/watchlist.csv 에서 읽는다.

watchlist.csv 컬럼: index, name(방 표시 이름), id(-100...), alias(raw CSV 파일명)
방 추가/제거/이름변경은 watchlist.csv 를 편집한다.
대화방 id는 `python -m src.backfill --list-dialogs` 로 확인.
"""
import csv

from . import config


def load_channels():
    """watchlist.csv 를 읽어 [{id, alias}, ...] 로 반환(없으면 빈 목록)."""
    path = config.INPUT_DIR / "watchlist.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = reader.fieldnames, list(reader)
    if any(not (r.get("id") or "").strip().lstrip("-").isdigit() for r in rows if (r.get("id") or "").strip()):
        rows = _heal_excel_ids(path, cols, rows)
    out = []
    for row in rows:
        rid = (row.get("id") or "").strip()
        alias = (row.get("alias") or "").strip()
        if rid and alias and rid.lstrip("-").isdigit():
            out.append({"id": int(rid), "alias": alias})
        elif rid and alias:
            print(f"[channels] id 복구 실패로 건너뜀: {alias} id={rid}")
    return out


def _heal_excel_ids(path, cols, rows):
    """Excel 로 저장하면 id 가 -1.00113E+12 처럼 6자리 유효숫자로 깨진다(2026-09-08 두 번 발생, 크래시 루프).
    DB(messages.chat_id / export_state.chat_id)에서 같은 6자리로 반올림되는 id 를 찾아 되돌리고 파일을 다시 쓴다.
    후보가 여럿이면 raw CSV 의 msg_id 겹침이 가장 큰 쪽. 못 찾으면 그 행은 그대로 두고 로더가 건너뛴다."""
    import sqlite3
    from . import config
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        ids = {r[0] for r in conn.execute("SELECT DISTINCT chat_id FROM messages")}
        ids |= {r[0] for r in conn.execute("SELECT chat_id FROM export_state")}
    except sqlite3.Error:
        return rows
    sci = lambda v: f"{float(v):.5E}"
    used = {int(r["id"]) for r in rows if (r.get("id") or "").strip().lstrip("-").isdigit()}
    fixed = 0
    for r in rows:
        rid = (r.get("id") or "").strip()
        if not rid or rid.lstrip("-").isdigit():
            continue
        try:
            cands = [c for c in ids if sci(c) == sci(rid) and c not in used]
        except ValueError:
            continue
        if len(cands) > 1:
            raw = config.RAW_DIR / f"{(r.get('alias') or '').strip()}.csv"
            if raw.exists():
                with open(raw, encoding="utf-8-sig", newline="") as f:
                    mine = {int(x["msg_id"]) for x in csv.DictReader(f)}
                cands = [max(cands, key=lambda c: len(mine & {x[0] for x in conn.execute(
                    "SELECT msg_id FROM messages WHERE chat_id = ?", (c,))}))]
        if len(cands) == 1:
            r["id"] = str(cands[0]); used.add(cands[0]); fixed += 1
    conn.close()
    if fixed:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        print(f"[channels] Excel 손상 id {fixed}개 복구 → {path}")
    return rows


# import 시 1회 로딩되는 스냅샷 — 단발 CLI(--chat, backfill)용.
# ★상주 프로세스(collect --watch)는 이 캐시를 쓰면 재기동 전까지 옛 목록으로 돈다.
#   매 사이클 최신 목록이 필요한 곳(collect_all/process_all/daily_report)은 load_channels() 를 직접 호출한다.
CHANNELS = load_channels()
