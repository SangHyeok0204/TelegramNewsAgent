"""증분 수집 — 마지막 저장 이후 새 메시지만 DB에 저장하고 방별 CSV에 append.

--chat 으로 단일 방을, 인자 없으면 channels.CHANNELS 전체를 순차 수집한다.
방별로 raw/{alias}.csv 에 텍스트가 누적된다(이미지는 수집하지 않음, 텍스트 전용).

--watch 는 통합 스케줄러로 상주한다(단일 프로세스 = 세션 충돌 없음). 2026-09-10 개편:
  - 수집     매시 :00 과 :30 에 맞춰(경계 정렬). 방은 순차·방간 2초라 창 안에서 저절로 분산된다
  - 하루 2회 06:55 · 12:55 (SEND_TIMES, 2026-09-14) → processed(새 글만 청킹) → 최근 24h 집계(Opus+/team) →
             최근 3일 발송 이력과 중복 판정(src/news_dedup.py) → 새 뉴스만 Slack 카드
  - Slack 카드  새 뉴스·미로그인 경보·작업 3회 실패 (src/slack_cards.py)
2026-09-10 본문 보강: 수집할 때 새 글의 링크 기사 본문과 첨부 PDF 텍스트를 같이 받아
DB(url_text·attach_text)에 넣는다 (src/enrich.py). 청킹이 제목이 아니라 내용을 본다.
정시 작업은 **데몬 스레드**로 돈다 — 집계 1콜이 길게는 25분이라 같은 스레드에서 돌리면
그동안 60초 tick 이 멈춰 :30 수집이 통째로 밀린다. 앞 슬롯이 아직 돌고 있으면 이번 슬롯은
건너뛴다(겹쳐 돌지 않는다). task_state.json 은 잠금 아래 read-modify-write 로만 쓴다.
미로그인 동안에는 정시 작업을 아예 돌리지 않는다(claude 비용 0).
정시 작업은 슬롯 시각(`hourly_slot`)으로 cache/task_state.json 에 기록하고 밀린 슬롯을
따라잡지 않는다 — 꺼져 있던 시간만큼 집계가 연달아 터지면 비용만 나가고 결과는 거의 같다.
2026-09-10: 일간 HTML 리포트(07:30)는 owner 결정으로 철회. 매 정시 카드가 대신한다.

⚠️ 이 파일은 --watch 프로세스에 모듈로 캐시된다. 스케줄을 고쳤으면 **재기동해야**
반영된다(2026-08-14: 재기동을 안 해 집계 풀링이 한 번도 안 돈 채 이틀 갔다).

사용:
    python -m src.collect                         # channels 전체 순차 1회
    python -m src.collect --watch --interval 30   # 상주 스케줄러 (run.bat)
    python -m src.collect --chat -1002471352838   # 단일 방
"""
import argparse
import contextlib
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from telethon import utils
from telethon.errors import FloodWaitError

from . import (channels, config, daily_report, db, enrich, export_csv, news_dedup,
               processed, slack_cards)
from .client import make_client

SLEEP_BETWEEN = 2  # 방 사이 간격(초) — flood 예방

# LAN 대시보드 하트비트(2026-07-21) — 포트 없는 상주 프로세스 감시용. 실패는 무시.
# docker: 기본 비활성(빈 값). 필요하면 TELEGRAM_BOT__HEARTBEAT_URL 로 켠다.
HEARTBEAT_URL = os.getenv("TELEGRAM_BOT__HEARTBEAT_URL", "")

_logfh = None  # --log 시 config.LOG_PATH 로 tee 하는 파일 핸들


def _heartbeat():
    """LAN 대시보드에 살아있음 통지(POST, 실패는 무시)."""
    if not HEARTBEAT_URL:
        return
    try:
        req = urllib.request.Request(HEARTBEAT_URL, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:
        pass


def emit(msg):
    """콘솔과(활성화 시) 로그 파일에 동시에 출력한다.

    정시 스레드가 _sub 로 stdout 을 _Tee 로 바꿔 둔 동안에는 print 가 이미 로그 파일까지
    쓴다 — 그때 또 쓰면 같은 줄이 두 번 남는다 (2026-09-10 스레드화)."""
    out = sys.stdout
    print(msg, flush=True)
    if _logfh is not None and not isinstance(out, _Tee):
        _logfh.write(msg + "\n")
        _logfh.flush()


# ---- 본문 보강(enrichment, 2026-09-10) ----
# 청킹 입력이 제목뿐이라 토픽이 얄팍했다 → 수집할 때 링크 본문과 PDF 첨부 텍스트를 같이 넣는다.
PDF_MAX_BYTES = enrich.PDF_MAX_BYTES   # 이보다 큰 PDF 는 받지 않는다 (2026-09-16: 링크 PDF 와 공유)
ENRICH_BUDGET = 300                # 한 수집 사이클에서 URL 본문 받기에 쓸 총시간(초). 2026-09-11: 120→60 · 2026-09-16: 60→300 (매 사이클 20~64건이 걸려 정시 enrich_missing 이 553건 중 382건만 메웠다. 사이클 간격 30분이라 여유), 못 채운 건 정시 작업의 enrich_missing 이 메운다
_enrich_deadline = 0.0             # 0 이면 예산 없음(단일 방 수동 수집)
_enrich_skipped = 0                # 예산 초과로 못 받은 URL 수 (다음에 --enrich-missing 로)


def _start_enrich_budget():
    global _enrich_deadline, _enrich_skipped
    _enrich_deadline = time.monotonic() + ENRICH_BUDGET
    _enrich_skipped = 0


def _first_url(text):
    m = export_csv.URL_RE.search(text or "")
    return m.group(0).rstrip(').,"\'>') if m else ""


def _enrich(client, chat_id, m):
    """새 메시지 1건 → (media_file, media_type, attach_text, url_text).

    PDF 첨부는 받아서 텍스트를 뽑고, 본문 첫 URL 은 기사 본문을 받는다. 실패는 전부 None.
    """
    global _enrich_skipped
    media_file = media_type = attach_text = url_text = None
    doc = getattr(m, "document", None)
    if getattr(m, "web_preview", None):
        media_type = "webpage"          # 링크 미리보기 — 본문은 아래 url_text 로 받는다
    elif doc is not None:
        mime = getattr(doc, "mime_type", "") or ""
        media_type = f"document/{mime.rsplit('/', 1)[-1]}" if mime else "document"
        if mime == "application/pdf" and (getattr(doc, "size", 0) or 0) <= PDF_MAX_BYTES:
            dest = config.MEDIA_DIR / str(chat_id) / f"{m.id}.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                client.download_media(m, file=str(dest))
            if dest.exists():
                media_file = dest.relative_to(config.DATA_ROOT).as_posix()
                attach_text = enrich.extract_pdf_text(dest) or None
    elif getattr(m, "photo", None):
        media_type = "photo"
    url = _first_url(m.message)
    if url:
        if _enrich_deadline and time.monotonic() > _enrich_deadline:
            _enrich_skipped += 1        # 이번 사이클 예산 소진 — url_text 는 NULL 로 남는다
        else:
            _t, _b = enrich.fetch_url_text(url)
            url_text = _b or (_t or None)  # 2026-09-10: 본문이 없으면 제목만이라도(DART 공시 등 프레임 페이지)
    return media_file, media_type, attach_text, url_text


def collect_once(client, entity, chat_id, alias=""):
    """방의 마지막 저장 메시지 이후 새 글만 (본문 보강해서) DB에 저장하고 건수를 반환한다."""
    conn = db.connect()
    db.init_db(conn)
    last = conn.execute(
        "SELECT COALESCE(MAX(msg_id), 0) FROM messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0]
    # min_id 초과(= last_id 이후 새 글)만 가져온다
    rows, n_url, n_url_ok, n_pdf, pdf_chars = [], 0, 0, 0, 0
    for m in client.iter_messages(entity, min_id=last):
        try:
            extra = _enrich(client, chat_id, m)
        except Exception as e:  # 보강 실패로 수집이 멈추면 안 된다
            emit(f"[enrich] {alias or chat_id} msg {m.id}: {e}")
            extra = (None, None, None, None)
        if _first_url(m.message):
            n_url += 1
            n_url_ok += bool(extra[3])
        if extra[2]:
            n_pdf += 1
            pdf_chars += len(extra[2])
        rows.append((chat_id, m.id, m.date.isoformat(), m.sender_id, m.message or "") + extra)
    db.upsert_messages(conn, rows)
    conn.close()
    if n_url or n_pdf:
        note = f"[enrich] url {n_url_ok}/{n_url}"
        if n_pdf:
            note += f" · pdf {n_pdf} (텍스트 {pdf_chars:,}자)"
        emit(f"[{alias or chat_id}] {note}")
    return len(rows)


def collect_channel(client, ch):
    """단일 방: DB 증분 저장 + raw/{alias}.csv 에 증분 append."""
    entity = client.get_entity(ch["id"])
    chat_id = utils.get_peer_id(entity)
    n = collect_once(client, entity, chat_id, ch["alias"])
    conn = db.connect()
    export_csv.append_increment(conn, chat_id, f"{ch['alias']}.csv")
    conn.close()
    if n > 0:
        emit(f"[{ch['alias']}] {n}건 수집")
    return n


def collect_all(client):
    """watchlist 전체를 순차로 한 바퀴 수집한다."""
    total = 0
    _start_enrich_budget()
    # 매 사이클 watchlist.csv 재로딩 — 상주 중 방을 추가/삭제해도 재기동 없이 반영된다
    # (import 시 1회 캐시하는 channels.CHANNELS 를 쓰면 재기동 전까지 옛 목록으로 돈다).
    chans = channels.load_channels()
    for i, ch in enumerate(chans):
        _heartbeat()  # 수집 사이클(길 수 있음) 중에도 생존 신호 유지
        try:
            total += collect_channel(client, ch)
        except FloodWaitError as e:
            emit(f"[flood-wait] {e.seconds}s @ {ch['alias']} — 대기")
            time.sleep(e.seconds)
        except Exception as e:
            emit(f"[ERROR] {ch['alias']}: {e}")
        if i < len(chans) - 1:
            time.sleep(SLEEP_BETWEEN)
    now = datetime.now()
    if _enrich_skipped:
        emit(f"[enrich] 예산({ENRICH_BUDGET}s) 초과 — URL {_enrich_skipped}건 건너뜀 "
             f"(나중에 `--enrich-missing` 로 채운다)")
    emit(f"[수집 완료] {now:%H:%M}분 텔레그램 봇_ 방 개수: {len(chans)} 수집 건수: {total} 건")
    # TODO(processed): 한 바퀴 후 raw 데이터를 claude CLI subprocess로 분석 → processed/ 저장 (추후)
    return total


class NotAuthorized(RuntimeError):
    """세션 파일은 있으나 서버에서 로그아웃된 상태. --watch 는 죽지 않고 대기해야 한다
    (2026-09-08: 미인증 시 SystemExit → 컨테이너 재시작 → 수 초마다 재접속 크래시 루프)."""


def _connect():
    client = make_client()
    client.connect()
    try:
        if not client.is_user_authorized():
            raise NotAuthorized("[collect] 미로그인 상태입니다. 먼저 `python -m src.client`로 로그인하세요.")
    except BaseException:
        # 연결만 열고 예외로 빠지면 클라이언트가 새서(자동 재접속 계속) 같은 auth key 로
        # 수백 개가 쌓인다 (2026-09-09: 시간당 12만 건 "Server closed the connection").
        client.disconnect()
        raise
    return client


# ---- 정시 작업 스케줄 (--watch 전용) ----

STATE_PATH = Path(config.LOG_PATH).parent / "task_state.json"
RETRY_COOLDOWN = 600  # 작업 실패 시 재시도 대기(초)
MAX_FAILS = 3
NIGHT_START, NIGHT_END = 22, 5  # 2026-09-10 owner: 22:00~05:00 는 수집만, 정시 청킹·집계는 05:00 슬롯부터 재개
SEND_TIMES = ((6, 55), (12, 55))  # 2026-09-14 owner: 매 정시 → 하루 2회. 청킹+집계+새 뉴스 카드 발송 시각


def is_night(now):
    return now.hour >= NIGHT_START or now.hour < NIGHT_END
  # 같은 stamp 재시도 상한 (Agents/CLAUDE.md 루프 규칙, 2026-09-10)

# 정시 작업은 데몬 스레드로 돈다 (2026-09-10) — 집계가 길어도 수집 tick 이 멈추지 않게.
_STATE_LOCK = threading.Lock()  # task_state.json 은 이 잠금 아래에서만 읽고 쓴다
_hourly_thread = None           # 지금 도는 정시 스레드(살아 있으면 다음 슬롯은 건너뛴다)
_hourly_skipped = None          # '건너뜀' 을 이미 찍은 슬롯


class _Tee:
    """processed/daily_report 의 print 출력을 콘솔과 --log 파일에 동시에 쓴다."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
            st.flush()

    def flush(self):
        for st in self._streams:
            st.flush()


def _sub(fn):
    """서브태스크 실행 — 그 print 출력을 --log 파일에도 tee."""
    if _logfh is None:
        fn()
        return
    orig = sys.stdout
    with contextlib.redirect_stdout(_Tee(orig, _logfh)):
        fn()


def _task_hourly():
    """매 정시(그 :00 수집 직후) — 새 글 청킹 → 최근 24h 집계 → 중복 제거 → 새 뉴스만 카드.

    집계는 Opus + /team (daily_report.AGG_MODEL/AGG_TEAM). 콜당 $5~9 = 하루 $120~200 이지만
    owner 가 비용을 받아들이고 품질을 택했다 (2026-09-10 결정).
    이미 보낸 뉴스는 최근 3일 이력과 대조해 뺀다 — 새 게 없으면 카드를 보내지 않는다."""
    try:  # 2026-09-11: 수집 사이클 예산에 걸려 비어 있는 링크 본문을 청킹 직전에 채운다(세션 불필요)
        enrich_missing(limit=1500, days=1, budget=300)
    except Exception as e:  # noqa: BLE001 — 본문 보강 실패가 청킹을 막지 않는다
        emit(f"[enrich-missing] 실패(무시): {type(e).__name__}: {e}")
    processed.process_all()  # 집계 직전 새 글만 청킹 → 창 공백 방지
    history = news_dedup.load_sent()
    # 2026-09-17: 발송 이력을 집계에도 넘겨 처음부터 새 사건을 고르게 유도(확률적). 차단은 아래 find_new 가 한다.
    payload = daily_report.export_analysis(sent=history)  # 방금 만든 집계를 그대로 쓴다(디스크 재독 불필요)
    if payload is None:
        return  # 최근 24h 토픽 0건 — 집계도 카드도 없다
    cands = news_dedup.candidates_from(payload)  # 5+3 여유분(최대 24건)
    idx = news_dedup.find_new(cands, history)
    agg_cost = float(payload.get("costUsd") or 0)
    emit(f"[뉴스] 후보 {len(cands)}건 · 최근 3일 발송 {len(history)}건 · 새 뉴스 {len(idx)}건 "
         f"(집계 ${agg_cost:.2f} + 중복판정 ${news_dedup.LAST_COST:.2f})")
    if not idx:
        return  # 새 게 없으면 카드 없음 (로그만)
    new_items = [cands[i] for i in idx]
    blocks = slack_cards.news_blocks(payload, new_items, len(cands), news_dedup.LAST_COST)  # 2026-09-10 카드 v2
    shown = slack_cards.shown_items(new_items)  # 카드는 분류당 5+3(2026-10-03) — 여유분에서 잘린 항목은 다음 회차 후보로 남는다
    if slack_cards.send_blocks(blocks, f"📰 새 뉴스 {len(shown)}건").get("ok"):
        news_dedup.append_sent(shown)  # 보여 준 것만 이력에 남긴다 (2026-09-17)
        slack_cards.send_extra(blocks, f"📰 새 뉴스 {len(shown)}건")  # 2026-10-10 웹훅 복사본


def next_boundary(now, minutes=30):
    """다음 수집 경계 시각. minutes=30 이면 :00/:30 (09:07→09:30, 09:30→10:00).

    '지금+30분'으로 잡으면 재기동할 때마다 창이 밀려 정시 집계가 수집 직전에 걸린다."""
    return (now.replace(minute=0, second=0, microsecond=0)
            + timedelta(minutes=(now.minute // minutes + 1) * minutes))


def _load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def read_state():
    """잠금 아래에서 state 스냅샷을 읽는다 (2026-09-10 정시 작업 스레드화)."""
    with _STATE_LOCK:
        return _load_state()


def update_state(patch):
    """잠금 아래 read-modify-write — 값이 None 인 키는 삭제한다.

    정시 스레드와 메인 루프가 각자 들고 있던 옛 dict 를 통째로 저장하면 서로의 키를
    지운다(로그인 경보 날짜 ↔ 정시 슬롯 스탬프). 그래서 저장은 항상 이 함수로만 한다."""
    with _STATE_LOCK:
        st = _load_state()
        for k, v in patch.items():
            if v is None:
                st.pop(k, None)
            else:
                st[k] = v
        _save_state(st)
        return st


def _try_task(key, stamp, label, fn, now):
    """stamp 로 아직 안 돈 작업이면 실행하고 state 에 stamp 를 남긴다. 실행했으면 True.

    2026-09-10: 정시 작업은 이 함수를 데몬 스레드에서 돈다 — state 는 patch 단위로만 쓴다."""
    st = read_state()
    if st.get(key) == stamp:
        return False
    fail = st.get(key + "_fail")
    if fail:
        try:
            if (now - datetime.fromisoformat(fail)).total_seconds() < RETRY_COOLDOWN:
                return False
        except ValueError:
            pass
    emit(f"[스케줄] {label} 시작")
    ok = False
    try:
        _sub(fn)
        update_state({key: stamp, key + "_fail": None, key + "_fails": None})
        ok = True
        emit(f"[스케줄] {label} 완료")
    except Exception as e:
        # 2026-09-10: 무한 재시도 금지. 같은 stamp 3회 실패면 그 stamp 는 포기하고 알린다.
        fails = st.get(key + "_fails") or {}
        n = (fails.get("n", 0) if fails.get("stamp") == stamp else 0) + 1
        if n >= MAX_FAILS:
            update_state({key: stamp, key + "_gave_up": str(e)[:200],
                          key + "_fail": None, key + "_fails": None})
            emit(f"[스케줄] {label} {MAX_FAILS}회 실패: {e} — 이 슬롯은 포기")
            slack_cards.send(*slack_cards.task_failed_card(label, str(e)))
        else:
            update_state({key + "_fails": {"stamp": stamp, "n": n}, key + "_fail": now.isoformat()})
            emit(f"[스케줄] {label} 실패({n}/{MAX_FAILS}): {e} — {RETRY_COOLDOWN // 60}분 후 재시도")
    return ok


_NIGHT_LOGGED = {}
_us_thread = None


def _task_us():
    """평일 저녁 미국장 개장 후 슬롯(2026-09-20 owner) — 새 글 청킹 → us_open(개장 -60분~+85분, 지정 방, sonnet) → 카드."""
    processed.process_all()  # 저녁 새 글 청킹(06:55 슬롯 부담도 그만큼 준다)
    from . import us_open
    r = us_open.run(datetime.now(us_open.KST))
    emit(f"[미국장] 후보 {r['total']}건 · 새 뉴스 {r['new']}건 · 발송 {r['sent']} (집계 ${r['cost']:.2f})")


def _run_us_task():
    """매 tick — 미국 개장+85분 슬롯(ET 거래일 월~금)이 지났고 아직 안 돌았으면 데몬 스레드로.
    야간(is_night) 과 무관하게 돈다. 밀린 세션은 grace(5h) 안일 때만 1회."""
    global _us_thread
    from . import us_open
    now = datetime.now(us_open.KST)
    win = us_open.slot_for(now)
    if not win or read_state().get("us_slot") == win[0]:
        return
    if (_us_thread is not None and _us_thread.is_alive()) or (_hourly_thread is not None and _hourly_thread.is_alive()):
        return  # 앞 작업이 돌고 있으면 다음 tick 에 다시 본다(processed CSV 양쪽 append 방지)
    _us_thread = threading.Thread(
        target=_try_task, name="us_open",
        args=("us_slot", win[0], f"{win[2]:%H:%M} 미국장 개장 후 흐름", _task_us, now.replace(tzinfo=None)),
        daemon=True)
    _us_thread.start()


def _run_us_close_task():
    """매 tick — 06:40 미국장 마감 카드 · 08:20 채권·환 카드 (2026-09-26 owner). LLM 없이 DB 만 읽어 1초 안에 끝나므로 스레드 없음.
    카드 ① : 어제(ET) 가 NYSE 거래일이면. 한투+투행 도착 시 발송, 07:00 이면 있는 것만으로. 상태 키 us_close_slot=세션날짜.
    카드 ② : 08:20~. 한국 영업일이면 키움+KB 오늘자, 휴장이면 투행 값만(축소). 08:30 이면 있는 것만으로. 상태 키 us_bond_slot=오늘."""
    from . import us_close
    now = datetime.now(us_close.KST)
    hm = (now.hour, now.minute)
    st = read_state()
    # 2026-09-26 owner: 발송 창은 아침뿐 — 창이 지나면(재기동·장애) 그날 카드는 건너뛴다. 밤에 아침 카드가 나가면 안 된다.
    if hm >= (6, 40):
        sess = us_close.us_session_for(now)
        if sess and st.get("us_close_slot") != sess.isoformat():
            if hm >= (9, 0):
                update_state({"us_close_slot": sess.isoformat()})
                emit(f"[마감카드] {sess} 발송 창(06:40~09:00) 지남 — 건너뜀")
            else:
                r = us_close.send_close(now, deadline=hm >= (7, 0))
                if r.get("sent") or "포기" in r.get("why", ""):
                    update_state({"us_close_slot": sess.isoformat()})
                    emit(f"[마감카드] {sess} 발송={r.get('sent')} ts={r.get('ts')} 숨김={r.get('errors')} partial={r.get('partial')} {r.get('why', '')}")
    if hm >= (8, 20):
        key = now.date().isoformat()
        if st.get("us_bond_slot") != key and hm >= (10, 0):
            update_state({"us_bond_slot": key})
            emit(f"[채권카드] {key} 발송 창(08:20~10:00) 지남 — 건너뜀")
        elif st.get("us_bond_slot") != key:
            try:
                r = us_close.send_bond(now, deadline=hm >= (8, 30))
            except Exception as e:  # noqa: BLE001 — 카드 실패가 수집 tick 을 막지 않는다
                emit(f"[채권카드] 실패: {type(e).__name__}: {e}"); r = {"sent": False, "why": "예외 — 포기"}
            if r.get("sent") or "포기" in r.get("why", "") or "휴장" in r.get("why", ""):
                update_state({"us_bond_slot": key})
                emit(f"[채권카드] {key} 발송={r.get('sent')} mode={r.get('mode')} ts={r.get('ts')} 숨김={r.get('errors')} {r.get('why', '')}")


def _run_hourly_task():
    """매 tick 호출 — 지금 정시 슬롯이 아직 안 돌았으면 **데몬 스레드로** 띄운다.

    스레드로 도는 이유: 집계 1콜이 Opus+/team 이라 길게는 25분(CLAUDE_TIMEOUT)이다.
    같은 스레드에서 돌리면 그동안 60초 tick 이 통째로 멈춰 :30 수집이 통으로 밀린다.

    **밀린 슬롯을 따라잡지 않는다** — 집계는 늘 '최근 24h 스냅샷'이라 지나간 슬롯을
    다시 돌려봐야 거의 같은 결과가 반복될 뿐인데 비용은 그대로 나간다(꺼져 있다 켜면
    밀린 슬롯이 연달아 터진다). 지금 슬롯 하나만 본다.
    앞 슬롯이 아직 돌고 있으면 이번 슬롯은 건너뛴다 — 겹쳐 돌면 집계 비용이 2배로 나가고
    processed CSV 를 양쪽에서 append 한다.
    이 함수는 수집 사이클 **직후**에 불린다 → :00 수집이 끝난 뒤에 정시 작업이 돈다."""
    now = datetime.now()
    if is_night(now):
        slot = now.strftime('%Y-%m-%d %H:00')
        if _NIGHT_LOGGED.get('slot') != slot:
            _NIGHT_LOGGED['slot'] = slot
            emit(f"[정시] 야간({NIGHT_START:02d}~{NIGHT_END:02d}시) — 수집만, 청킹·집계 건너뜀")
        return
    global _hourly_thread, _hourly_skipped
    now = datetime.now()  # 수집은 수 분 걸린다 — 슬롯은 '끝난 시각' 기준
    # 2026-09-14: 오늘 지난 SEND_TIMES 중 가장 최근 것 하나만 본다(같은 날 재기동이면 그 슬롯을 늦게라도 1회 보낸다).
    passed = [t for t in SEND_TIMES if (now.hour, now.minute) >= t]
    if not passed:
        return
    stamp = now.strftime("%Y-%m-%d ") + "%02d:%02d" % passed[-1]
    if read_state().get("hourly_slot") == stamp:
        return  # 이 슬롯은 이미 끝났다 — 매 tick 스레드를 만들지 않는다
    if _hourly_thread is not None and _hourly_thread.is_alive():
        if _hourly_skipped != stamp:  # 같은 슬롯을 매 tick 찍지 않는다
            _hourly_skipped = stamp
            emit("[정시] 이전 집계 아직 실행 중 — 이번 슬롯 건너뜀")
        return
    _hourly_thread = threading.Thread(
        target=_try_task, name="hourly",
        args=("hourly_slot", stamp, f"{stamp[-5:]} 청킹+집계+새 뉴스", _task_hourly, now),
        daemon=True,  # 창을 닫으면 집계 중이어도 같이 죽는다(스탬프가 안 찍혀 다음 기동 때 그 슬롯 재시도)
    )
    _hourly_skipped = stamp  # 2026-09-14: 같은 슬롯이 도는 동안 '건너뜀' 을 찍지 않는다(다른 슬롯일 때만)
    _hourly_thread.start()


def _notify_login_needed():
    """미로그인 Slack 경보 — 하루 1회만 (2026-09-10)."""
    today = datetime.now().strftime("%Y-%m-%d")
    if read_state().get("slack_login_alert_date") == today:
        return
    if slack_cards.send(*slack_cards.login_alert_card()).get("ok"):
        update_state({"slack_login_alert_date": today})  # 정시 스레드의 키를 덮어쓰지 않는다


def run_once_all():
    """한 바퀴 수집(연결→수집→해제). --watch 루프가 매 사이클 호출한다."""
    client = _connect()
    try:
        collect_all(client)
    finally:
        client.disconnect()


# --watch 단일 인스턴스 잠금 — 고정 localhost 포트를 점유해 2번째 상주 스케줄러를 차단한다.
# 소켓은 프로세스 종료 시 OS가 자동 회수 → stale lock 없음. 옛 run_collect.bat 의 "2번째 실행 금지"
# 경고를 코드로 강제(중복 실행 시 세션/DB 충돌·이중 처리·task_state 경합 방지).
_LOCK_PORT = 47654
_lock_sock = None


def _acquire_watch_lock():
    """다른 --watch 가 이미 실행 중이면 False. 성공 시 소켓을 전역에 잡아둔다(닫히면 해제)."""
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 0.0.0.0 바인드: 락 겸 LAN 대시보드(lan-dashboard) TCP 생존 신호 (SO_REUSEADDR 미사용 → 2번째 bind 는 실패)
        s.bind(("0.0.0.0", _LOCK_PORT))
    except OSError:
        s.close()
        return False
    _lock_sock = s
    return True


def enrich_missing(limit=None, days=7, budget=None):
    """텔레그램 없이(세션 불필요) 최근 days 일 글 중 url_text 가 빈 것을 채운다.

    수집 사이클 예산(ENRICH_BUDGET)에 걸려 건너뛴 URL 을 나중에 메우는 용도.
    """
    conn = db.connect()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    q = ("SELECT chat_id, msg_id, text FROM messages "
         "WHERE url_text IS NULL AND date_utc >= ? AND text LIKE '%http%' "
         "ORDER BY date_utc DESC")
    args = [cutoff]
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(q, args).fetchall()
    t0 = time.monotonic()
    filled = chars = 0
    for chat_id, msg_id, text in rows:
        if budget and time.monotonic() - t0 > budget:  # 2026-09-11: 정시 작업 안에서는 시간 상한
            emit(f"[enrich-missing] 예산({budget}s) 초과 — 나머지는 다음 정시에")
            break
        _t, body = enrich.fetch_url_text(_first_url(text))
        body = body or _t  # 2026-09-10: 본문 없으면 제목
        if not body:
            continue
        conn.execute("UPDATE messages SET url_text = ? WHERE chat_id = ? AND msg_id = ?",
                     (body, chat_id, msg_id))
        filled += 1
        chars += len(body)
    conn.commit()
    conn.close()
    emit(f"[enrich-missing] 대상 {len(rows)}건 · 채움 {filled}건 "
         f"(평균 {chars // filled if filled else 0:,}자, {time.monotonic() - t0:.1f}s)")
    return filled


def main():
    global _logfh
    ap = argparse.ArgumentParser(prog="src.collect")
    ap.add_argument("--chat", help="단일 방 id(-100...). 생략 시 channels 전체 순차")
    ap.add_argument("--watch", action="store_true", help="주기적으로 반복 수집(콘솔 상주)")
    ap.add_argument("--interval", type=int, default=30, help="--watch 수집 주기(분), 기본 30")
    ap.add_argument("--log", action="store_true", help="config.LOG_PATH 에도 출력을 기록(tee)")
    ap.add_argument("--enrich-missing", action="store_true",
                    help="최근 7일 중 url_text 가 빈 글의 링크 본문을 채운다(텔레그램 연결 없음)")
    ap.add_argument("--limit", type=int, help="--enrich-missing 대상 건수 상한")
    args = ap.parse_args()

    if args.log:
        _logfh = open(config.LOG_PATH, "a", encoding="utf-8")

    if args.enrich_missing:
        enrich_missing(args.limit)
        return

    if args.chat:
        try:
            client = _connect()
        except NotAuthorized as e:
            raise SystemExit(str(e))
        try:
            chat = int(args.chat) if args.chat.lstrip("-").isdigit() else args.chat
            cid = utils.get_peer_id(client.get_entity(chat))
            ch = next((c for c in channels.CHANNELS if c["id"] == cid),
                      {"id": cid, "alias": str(cid)})
            _start_enrich_budget()
            collect_channel(client, ch)
        finally:
            client.disconnect()
        return

    if not args.watch:
        try:
            run_once_all()
        except NotAuthorized as e:
            raise SystemExit(str(e))
        return

    # --watch: 콘솔 상주 통합 스케줄러 (경계 정렬 수집 + 매 정시 집계·중복제거·카드)
    if not _acquire_watch_lock():
        emit(f"[collect] 다른 --watch 인스턴스가 이미 실행 중입니다(port {_LOCK_PORT}). 이 인스턴스는 종료합니다.")
        return
    emit(f"텔레그램 모니터링을 수행합니다.. (수집 {args.interval}분 경계 :00/:30 / "
         f"청킹+집계+새 뉴스 카드 {' · '.join('%02d:%02d' % t for t in SEND_TIMES)} · 미국장 개장+85분 평일 저녁 카드 · 06:40 마감 카드 · 08:20 채권 카드)")
    next_collect = datetime.now()  # 기동 직후 1회는 경계를 기다리지 않고 바로 수집
    authorized = True  # 로그인 상태. False 인 동안 정시 작업(=claude 비용)을 돌리지 않는다
    next_skip_log = datetime.now()
    while True:
        _heartbeat()  # 매 tick(60s) 생존 신호 — 기동 즉시부터 online
        if datetime.now() >= next_collect:
            try:
                run_once_all()
                if not authorized:
                    slack_cards.send(*slack_cards.recovered_card())
                authorized = True
                next_collect = next_boundary(datetime.now(), args.interval)
                emit(f"[대기] 다음 수집 {next_collect:%H:%M} 예정")
                emit("")
            except NotAuthorized as e:
                # 죽지 않고 대기. 사람이 재로그인하면 다음 사이클에 자동 재개.
                authorized = False
                emit(str(e))
                emit("[대기] 재로그인 후 자동 재개 — 5분 뒤 재확인")
                _notify_login_needed()
                next_collect = datetime.now() + timedelta(minutes=5)
            except Exception as e:
                emit(f"[ERROR] 사이클 실패: {e}")
                next_collect = next_boundary(datetime.now(), args.interval)
                emit(f"[대기] 다음 수집 {next_collect:%H:%M} 예정")
                emit("")
        if authorized:
            _run_hourly_task()
            _run_us_task()  # 2026-09-20: 평일 저녁 미국장 개장 후 슬롯(야간 무관)
            _run_us_close_task()  # 2026-09-26: 06:40 마감 카드 · 08:20 채권 카드 (LLM 없음)
        elif datetime.now() >= next_skip_log:
            # 2026-09-09: 미로그인 상태로 2시간 집계가 4회 돌아 $20.47 나갔다.
            emit("[대기] 미로그인 — 정시 작업 건너뜀")
            next_skip_log = datetime.now() + timedelta(hours=1)
        time.sleep(60)


if __name__ == "__main__":
    main()
