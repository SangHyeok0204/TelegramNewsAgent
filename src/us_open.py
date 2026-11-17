# -*- coding: utf-8 -*-
"""미국장 개장 후 흐름 카드 — 평일 저녁 슬롯 (2026-09-20 owner 결정).

목적은 '다빈도 뉴스'가 아니라 **미국 정규장 개장 이후 지수·섹터·주도주 흐름 모니터링**이다.
  - 슬롯: 미국 개장(ET 09:30 → KST 환산, 서머타임 자동) + SLOT_AFTER_MIN. 서머타임 23:55 · 겨울 00:55.
  - 요일: 미국 거래일(월~금 ET)만. 주말·한국 기준 토요일 새벽(금요일 세션)은 자연히 포함된다. 미국 휴장일은
    처리하지 않는다 — 글이 없으면 카드가 안 나간다.
  - 창: 개장 -WINDOW_BEFORE_MIN(프리마켓) → 슬롯 시각. 24h 창이 아니다.
  - 방: ROOMS 만 (2026-09-20 3주 실측 — 개장 후 미국 주식 글을 규칙적으로 올리는 방). 없으면 전체.
  - 집계: sonnet 1콜, /team 없음(입력 10~30건이라 Opus+team 은 낭비). 최대 MAX_ITEMS 건, 재료 없으면 적게.
  - 중복: news_dedup(최근 3일 발송 이력) 그대로. 카드는 slack_cards 의 card carousel 재사용.
  - 대시보드 JSON(dashboard_analysis.json, 24h 계약)은 건드리지 않는다.

    python -m src.us_open --dry-run      # 가장 최근 세션 창으로 집계까지(발송 없음, claude 1콜)
    python -m src.us_open --send         # 실제 발송(예시 확인용)
    python -m src.us_open --selftest     # 시각 계산·창 계산 검사(네트워크·claude 없음)
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, time, timedelta

from . import config, daily_report, news_dedup, slack_cards
from .daily_report import KST, _read_csv, allowed_domains
from . import channels

SLOT_AFTER_MIN = 85        # 개장 +85분 = 개장 직후 봉우리(22:30~23:30)를 다 담는 시각
WINDOW_BEFORE_MIN = 60     # 개장 -60분부터(프리마켓 뉴스)
SLOT_GRACE_H = 5           # 슬롯 시각 후 이 시간 안이면 늦게라도 1회 실행(재기동 대비)
MAX_ITEMS = 6
MODEL = "claude-sonnet-5"  # 2026-10-01: 별칭 "sonnet" 이 5.5 로 바뀐 뒤 검색을 건너뛰어 링크 0건 → 버전 고정
CLAUDE_TIMEOUT = 600
# 2026-09-20 실측 상위(개장 후 22:30~23:59 활동일 / 미국 주식 글): 편집은 여기 한 곳.
ROOMS = ["피에이뉴스PANews", "투행자료창고", "하나중국신흥국", "차분남", "쿠쿠쿱koob", "아무자료아무노래"]

_ISSUE = daily_report._ISSUE
SCHEMA = json.dumps({"type": "object", "properties": {"items": {"type": "array", "items": _ISSUE}},
                     "required": ["items"]}, ensure_ascii=False)
PROMPT = (
    "다음은 한국 증권가 텔레그램 방에서 미국 정규장 개장 전후(프리마켓~개장 후 약 1시간 반)에 올라온 토픽 목록이다. "
    "각 항목은 id(정수), room(방), topic(한 줄 헤드라인)이다.\n"
    f"목적은 '미국장 개장 이후 흐름 모니터링'이다. 지수·섹터·주도주의 움직임, 그 원인이 된 매크로 이벤트·실적·"
    f"정책 발언을 중심으로 최대 {MAX_ITEMS}개만 뽑아라. 재료가 없으면 적게, 없으면 0개. "
    "언급 빈도보다 '장 흐름을 설명하는가'가 기준이다. 한국 주식·코인·광고·리포트 발간 알림은 제외.\n"
    "- 아래 '이미 보낸 뉴스' 와 같은 사건은 넣지 말고 새 사실만.\n"
    "각 이슈 필드:\n"
    "- title: 한 줄 제목(핵심 압축, 수치 포함).\n"
    "- summary: 핵심 '수치·키워드' 3~6개를 ' · '(가운뎃점)로 나열. 문장 금지. 예) '나스닥 -1.2% · 美10Y 4.5% · 엔비디아 -3%'.\n"
    "- ids: 해당 입력 항목 id 전부(정수 배열).\n"
    "- link: 대표 기사 URL 1개를 WebSearch/WebFetch 로 찾는다. 언론사·통신사·기업 공식 보도자료의 기사 URL — 사이트 우선순위 없음. 블로그·카페·커뮤니티·SNS·유튜브·단축링크와 로그인·유료 기사(한경 프리미엄 등) 금지. 존재를 확인한 것만, 없으면 \"\".\n"
    "한국어로. 입력:\n"
)


# ── 시각 ──
def _nth_sunday(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)      # 첫 일요일
    return d + timedelta(days=7 * (n - 1))


def us_dst(d: date) -> bool:
    """미국 서머타임 여부(3월 둘째 일요일 ~ 11월 첫째 일요일). venv 에 tzdata 가 없어 규칙으로 계산한다."""
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def open_kst(d: date) -> datetime:
    """ET 거래일 d 의 정규장 개장(09:30 ET) 을 KST 로. 서머타임 22:30, 겨울 23:30."""
    return datetime.combine(d, time(22 if us_dst(d) else 23, 30), tzinfo=KST)


def slot_for(now: datetime):
    """지금 처리해야 할 세션 → (stamp, window_start, window_end) 또는 None.
    최근 ET 거래일(월~금) 중 슬롯 시각(개장+85분)이 지났고 SLOT_GRACE_H 안인 것 하나."""
    for back in (0, 1):
        d = now.date() - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        slot = open_kst(d) + timedelta(minutes=SLOT_AFTER_MIN)
        if slot <= now < slot + timedelta(hours=SLOT_GRACE_H):
            return f"us {d.isoformat()}", open_kst(d) - timedelta(minutes=WINDOW_BEFORE_MIN), slot
    return None


# ── 입력 ──
def collect_window(start: datetime, end: datetime) -> list[dict]:
    """[start, end) 의 토픽 — ROOMS 만(processed CSV). ROOMS 가 비어 있으면 전체 방."""
    items = []
    for ch in channels.load_channels():
        if ROOMS and ch["alias"] not in ROOMS:
            continue
        for r in _read_csv(config.PROCESSED_DIR / f"{ch['alias']}_processed.csv"):
            topic = (r.get("topic") or "").strip()
            if not topic:
                continue
            try:
                dt = datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except Exception:
                continue
            if start <= dt < end:
                items.append({"id": len(items), "room": ch["alias"], "topic": topic})
    return items


# ── 집계 ──
def analyze(items: list[dict], sent=None) -> tuple[list[dict], float]:
    domains = allowed_domains()
    prompt = (PROMPT + daily_report._sent_block(sent) + "입력 토픽: " + json.dumps(items, ensure_ascii=False))
    proc = subprocess.run(
        [config.CLAUDE_EXE, "-p", "--model", MODEL, "--output-format", "json", "--json-schema", SCHEMA,
         "--dangerously-skip-permissions"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude 실패(rc={proc.returncode}): {(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    out = data.get("structured_output") or json.loads(data["result"])
    n = len(items)
    cands, added = [], {}
    for iss in (out.get("items") or [])[:MAX_ITEMS]:
        ids = sorted({i for i in (iss.get("ids") or []) if isinstance(i, int) and 0 <= i < n})
        cands.append({"section": "us", "kind": "top", "title": str(iss.get("title") or "").strip(),
                      "summary": str(iss.get("summary") or "").strip(), "ids": ids, "mentions": len(ids),
                      "link": daily_report.accept_link(iss.get("link"), domains, added)})
    daily_report.add_domains(added, domains)
    cost = float(data.get("total_cost_usd") or 0)
    return cands, cost + daily_report.ensure_links(cands, domains, "us_open")


# ── 카드 ──
def blocks(items_new: list[dict], end: datetime, total: int, cost: float) -> list:
    head = f"🇺🇸 미국장 개장 후 흐름 · {end:%m-%d %H:%M}"
    out = [{"type": "header", "text": {"type": "plain_text", "text": head[:150]}}]
    if items_new:
        out += [slack_cards._item_section(it) for it in items_new[:MAX_ITEMS]]
    else:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": "새 뉴스 없음"}})
    out.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f"개장 -{WINDOW_BEFORE_MIN}분 ~ +{SLOT_AFTER_MIN}분 · 방 {len(ROOMS) or '전체'} · 새 *{len(items_new)}*건 / 후보 {total} · 비용 ${cost:.2f}"}]})
    return out


def run(now: datetime | None = None, send: bool = True, force_window=None) -> dict:
    """슬롯 본체 — collect._task_us 가 부른다. 반환 {sent, new, total, cost}. 새 게 없으면 발송 없음."""
    now = now or datetime.now(KST)
    win = force_window or (lambda s: s and s[1:])(slot_for(now))
    if not win:
        print("[us_open] 처리할 세션 없음")
        return {"sent": False, "new": 0, "total": 0, "cost": 0.0}
    start, end = win
    items = collect_window(start, end)
    print(f"[us_open] 창 {start:%m-%d %H:%M}~{end:%H:%M} 토픽 {len(items)}건 (방 {len(ROOMS)})")
    if not items:
        return {"sent": False, "new": 0, "total": 0, "cost": 0.0}
    history = news_dedup.load_sent()
    cands, cost = analyze(items, sent=history)
    idx = news_dedup.find_new(cands, history) if cands else []
    new_items = [cands[i] for i in idx]
    print(f"[us_open] 후보 {len(cands)}건 · 새 뉴스 {len(new_items)}건 (집계 ${cost:.2f} + 중복판정 ${news_dedup.LAST_COST:.2f})")
    for it in cands:
        print(f"   {'NEW' if it in new_items else 'dup'} {it['title']} | {it['summary']} | {it['link'] or '-'}")
    if not new_items or not send:
        return {"sent": False, "new": len(new_items), "total": len(cands), "cost": cost}
    ok = slack_cards.send_blocks(blocks(new_items, end, len(cands), cost + news_dedup.LAST_COST),
                                 f"🇺🇸 미국장 개장 후 흐름 {len(new_items)}건").get("ok")
    if ok:
        news_dedup.append_sent(new_items)
    return {"sent": bool(ok), "new": len(new_items), "total": len(cands), "cost": cost}


def _selftest():
    assert us_dst(date(2026, 9, 21)) and not us_dst(date(2026, 11, 2)) and not us_dst(date(2026, 3, 7)) and us_dst(date(2026, 3, 9))
    assert open_kst(date(2026, 9, 21)).strftime("%H:%M") == "22:30" and open_kst(date(2026, 11, 2)).strftime("%H:%M") == "23:30"
    k = lambda *a: datetime(*a, tzinfo=KST)
    assert slot_for(k(2026, 9, 21, 23, 54)) is None                       # 월 23:54 — 아직
    st, a, b = slot_for(k(2026, 9, 21, 23, 56)); assert st == "us 2026-09-21" and (a.hour, a.minute, b.hour, b.minute) == (21, 30, 23, 55)
    assert slot_for(k(2026, 9, 22, 4, 0))[0] == "us 2026-09-21"           # 새벽 4시 — 아직 grace 안
    assert slot_for(k(2026, 9, 22, 5, 0)) is None                         # grace 종료
    assert slot_for(k(2026, 9, 19, 23, 56)) is None and slot_for(k(2026, 9, 20, 23, 56)) is None  # 토·일 저녁 없음
    st, a, b = slot_for(k(2026, 9, 26, 0, 30)); assert st == "us 2026-09-25"  # 토요일 새벽 = 금요일 세션(서머타임 23:55 슬롯 뒤)
    st, a, b = slot_for(k(2026, 11, 3, 0, 56)); assert st == "us 2026-11-02" and b.strftime("%H:%M") == "00:55"  # 겨울
    assert slot_for(k(2026, 11, 2, 23, 56)) is None                       # 겨울 23:56 — 개장 23:30 이라 아직
    # 링크 0건 보충(daily_report.ensure_links): 0건→보충 · 도메인 밖/범위 밖 버림 · 실패→그대로 · 링크 있거나 후보 0건→호출 없음
    calls = []
    def fake(result):
        def search(issues, domains):
            calls.append(1)
            if isinstance(result, Exception):
                raise result
            return result, 0.5
        return search
    e = daily_report.ensure_links
    daily_report._url_alive = lambda url: False   # 셀프테스트는 네트워크를 타지 않는다 — 목록 밖 링크는 '안 열림'
    daily_report._rss_links = lambda issues, domains: {}   # RSS 단계도 네트워크 없이 — 0건 → sonnet 보충으로 넘어간다
    # RSS 가 채운 건 sonnet 에 안 넘긴다 · 다 채우면 보충 콜 없음
    iss = [{"link": ""}, {"link": ""}]; calls.clear()
    assert e(iss, {"a.com"}, "t", fake({0: "https://a.com/s"}), rss=lambda i, d: {0: "https://a.com/r"}) == 0.5
    assert iss == [{"link": "https://a.com/r"}, {"link": "https://a.com/s"}]
    iss = [{"link": ""}]; calls.clear()
    assert e(iss, {"a.com"}, "t", fake({}), rss=lambda i, d: {0: "https://a.com/r"}) == 0.0 and not calls
    assert daily_report._title_score("삼성전자, 2030년 말까지 8인치 파운드리 완전 철수 통보", "삼성전자, 2030년 8인치 파운드리 완전 철수") >= daily_report.RSS_MIN_SCORE
    assert daily_report._title_score("심텍 3Q 영업익 1,018억 서프라이즈", "심텍, SOCAMM 타고 '깜짝 실적'…AI 기판 대장주 굳힌다") < daily_report.RSS_MIN_SCORE
    iss = [{"link": ""}, {"link": ""}]
    assert e(iss, {"a.com"}, "t", fake({0: "https://a.com/x", 1: "https://b.com/y", 9: "https://a.com/z"})) == 0.5
    assert iss == [{"link": "https://a.com/x"}, {"link": ""}]
    iss = [{"link": ""}]; assert e(iss, {"a.com"}, "t", fake(RuntimeError("x"))) == 0.0 and iss == [{"link": ""}]
    calls.clear()
    assert e([{"link": "https://a.com/x"}], {"a.com"}, "t", fake({})) == 0.0 and e([], {"a.com"}, "t", fake({})) == 0.0 and not calls
    iss = [{"link": "https://a.com/x"}, {"link": ""}]           # 2026-10-04: 빈 것만 보충
    assert e(iss, {"a.com"}, "t", fake({0: "https://a.com/y"})) == 0.5 and iss == [{"link": "https://a.com/x"}, {"link": "https://a.com/y"}]
    # 목록 밖 링크: 비기사 사이트 버림 · 열리면 받고 엑셀에 행 추가(임시 사본)
    acc = daily_report.accept_link
    assert acc("https://youtu.be/x", set(), {}) == "" and acc("https://m.blog.naver.com/x", set(), {}) == "" and acc("naver.com", set(), {}) == ""
    daily_report._url_alive = lambda url: True
    added = {}
    assert acc("https://www.newsite.co.kr/a/1", set(), added) == "https://www.newsite.co.kr/a/1" and added == {"www.newsite.co.kr": "https://www.newsite.co.kr/a/1"}
    assert acc("https://fox.com/a", set(), {}) == "https://fox.com/a"     # 'x.com' 접미사 오탐 없음
    daily_report._url_alive = lambda url: False   # 아래는 열림 확인 없이 리스트 매칭만으로 통과해야 한다
    for u in ("https://sedaily.com/a", "https://m.sedaily.com/a", "https://www.sedaily.com/a"):   # 같은 매체 = 한 행
        assert acc(u, {"www.sedaily.com"}, {}) == u, u
    assert acc("https://news.naver.com/a", {"n.news.naver.com"}, {}) == ""   # 다른 서브도메인은 별개
    daily_report._url_alive = lambda url: True
    # 한경 프리미엄9(로그인) 은 리스트에 있는 사이트여도 버림, 일반 기사는 통과
    assert acc("https://www.hankyung.com/article/202609220337i", {"www.hankyung.com"}, {}) == ""
    assert acc("https://contents.premium.naver.com/umbrellaresearch/x/contents/1", {"contents.premium.naver.com"}, {}) == ""
    assert acc("https://www.hankyung.com/article/2026100423117", {"www.hankyung.com"}, {}) == "https://www.hankyung.com/article/2026100423117"
    import shutil, tempfile
    from pathlib import Path
    tmpdir = Path(tempfile.mkdtemp())
    shutil.copy(config.INPUT_DIR / "뉴스링크정리.xlsx", tmpdir / "뉴스링크정리.xlsx")
    real = config.INPUT_DIR
    try:
        config.INPUT_DIR = tmpdir
        before = allowed_domains(); doms = set(before)
        daily_report.add_domains(added, doms)
        after = allowed_domains()
        assert after == before | {"www.newsite.co.kr"} and "www.newsite.co.kr" in doms, after - before
    finally:
        config.INPUT_DIR = real
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert len(daily_report._issues({"macro": {"top": [{}], "notable": [{}]}, "stock": {"top": [{}]}})) == 3
    print("selftest ok")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry-run"
    if mode == "--selftest":
        _selftest()
        sys.exit(0)
    now = datetime.now(KST)
    win = slot_for(now)
    if not win:   # 가장 최근 평일 세션으로 강제 — 예시·점검용
        d = now.date()
        while d.weekday() >= 5 or open_kst(d) + timedelta(minutes=SLOT_AFTER_MIN) > now:
            d -= timedelta(days=1)
        win = (f"us {d}", open_kst(d) - timedelta(minutes=WINDOW_BEFORE_MIN), open_kst(d) + timedelta(minutes=SLOT_AFTER_MIN))
    print(f"[us_open] 세션 {win[0]}")
    print(json.dumps(run(now, send=(mode == "--send"), force_window=win[1:]), ensure_ascii=False))
