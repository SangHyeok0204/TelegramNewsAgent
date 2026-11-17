# -*- coding: utf-8 -*-
"""이미 보낸 뉴스와 대조해 '새 뉴스'만 골라낸다 (2026-09-10).

매 정시 집계가 내놓는 후보(섹션 × 상위5 + 특이3 = 최대 24건, 2026-09-17 전엔 3+2=15)를 최근 3일간 Slack 으로
보낸 뉴스와 비교한다. 같은 사건의 재탕이면 빼고 새 사실만 카드에 올린다.

판정은 2단계다.
  1) 결정론적 사전 필터 — 제목 정규화 일치 또는 같은 링크면 바로 중복(claude 0콜).
  2) 남은 후보만 claude(sonnet) 1콜. 실패하면 전부 '새 뉴스'로 두고 크게 로그를 남긴다
     (뉴스를 조용히 버리는 쪽이 더 나쁘다).

(2)를 나중에 결정론적 규칙으로 바꿀 수 있게 판정을 이 파일 하나에 가둔다.
바깥이 아는 것은 `find_new(candidates, sent_history) -> [새 후보의 index]` 뿐이다.

    .venv\\Scripts\\python -X utf8 -m src.news_dedup --selftest   # claude 없이 사전 필터 점검
    .venv\\Scripts\\python -X utf8 -m src.news_dedup --history    # 최근 3일 발송 이력 요약
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import config

MODEL = "sonnet"
CLAUDE_TIMEOUT = 300
HISTORY_DAYS = 3
SENT_PATH = Path(config.DATA_ROOT) / "cache" / "sent_news.jsonl"

# 직전 find_new 가 쓴 claude 비용(USD). 카드 컨텍스트가 읽는다 —
# find_new 의 반환값은 인덱스 목록으로 고정한다(인터페이스 계약).
LAST_COST = 0.0

SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "new": {"type": "array", "items": {"type": "integer"}},
            "dups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"idx": {"type": "integer"}, "matches": {"type": "string"}},
                    "required": ["idx", "matches"],
                },
            },
        },
        "required": ["new", "dups"],
    },
    ensure_ascii=False,
)

PROMPT = (
    "아래 '후보' 뉴스 각각이 '이미_보낸_뉴스'와 같은 사건인지 판정한다.\n"
    "- dup: 같은 사건·같은 회사·같은 수치의 후속이거나 재탕(표현·제목만 달라진 것 포함).\n"
    "- new: 새 사실 — 다른 회사, 다른 숫자, 다른 결정·발표.\n"
    "판단이 애매하면 new 로 둔다(새 뉴스를 놓치는 쪽이 더 나쁘다).\n"
    "후보의 idx 는 입력에 적힌 값을 그대로 쓴다. 모든 후보를 new 또는 dups 중 정확히 한 쪽에만 넣는다.\n"
    "dups 의 matches 에는 근거가 된 '이미_보낸_뉴스'의 title 을 그대로 적는다.\n"
    "설명 없이 JSON 만 출력한다. 입력:\n"
)


def _norm(text):
    """제목 비교용 정규화 — 공백·기호·대소문자를 지운 글자만 남긴다."""
    return re.sub(r"[^0-9a-z가-힣]+", "", str(text or "").lower())


def _link(url):
    """링크 비교용 정규화 — 프로토콜·끝 슬래시만 정리한다(질의문자열은 그대로 둔다)."""
    return re.sub(r"^https?://", "", str(url or "").strip().lower()).rstrip("/")


def candidates_from(payload):
    """집계 payload → 후보 목록. 카드·이력이 쓰는 후보 형식은 여기 하나로 정한다."""
    out = []
    for section in ("macro", "industry", "stock"):
        sec = ((payload or {}).get("sections") or {}).get(section) or {}
        # 2026-09-17: 5+3 여유분 — 2026-10-03 부터 카드도 5+3 이라 slack_cards.shown_items 가 거의 다 싣는다
        for kind, lst, limit in (("top", sec.get("top") or [], 5), ("notable", sec.get("notable") or [], 3)):
            for iss in list(lst)[:limit]:
                out.append({
                    "section": section,
                    "kind": kind,  # 2026-09-10: 카드가 핵심/놓칠 뉴스를 구분한다
                    "title": str(iss.get("title") or "").strip(),
                    "summary": str(iss.get("summary") or "").strip(),
                    "ids": list(iss.get("ids") or []),
                    "link": str(iss.get("link") or "").strip(),
                    "mentions": iss.get("mentions") or 0,
                })
    return out


def find_new(candidates, sent_history):
    """후보 중 '새 뉴스'인 것의 index 목록(오름차순).

    candidates: [{section, title, summary, ids, link, mentions}]
    sent_history: [{title, summary, link, sent_at}]
    """
    global LAST_COST
    LAST_COST = 0.0
    if not candidates:
        return []
    if not sent_history:
        return list(range(len(candidates)))  # 이력이 없으면 전부 새 뉴스 — claude 를 부르지 않는다

    seen_titles = {_norm(h.get("title")) for h in sent_history if _norm(h.get("title"))}
    seen_links = {_link(h.get("link")) for h in sent_history if _link(h.get("link"))}
    rest = []
    for i, c in enumerate(candidates):
        link = _link(c.get("link"))
        if _norm(c.get("title")) in seen_titles or (link and link in seen_links):
            continue  # 사전 필터: 제목·링크가 같으면 물어볼 것도 없다
        rest.append(i)
    if not rest:
        return []

    try:
        picked, LAST_COST = _ask_claude(candidates, sent_history, rest)
    except Exception as e:
        # 실패를 삼키면 새 뉴스가 아니라 '모든 뉴스'가 조용히 사라진다. 크게 남기고 전부 보낸다.
        print(f"[dedup] !!! 중복 판정 실패 — 후보 {len(rest)}건을 모두 새 뉴스로 보냅니다: {e}", flush=True)
        return rest
    return picked


def _ask_claude(candidates, history, rest):
    """남은 후보만 claude(sonnet, 도구 없음) 1콜로 판정 → (새 뉴스 index, cost)."""
    payload = {
        "후보": [{"idx": i, "title": candidates[i].get("title"),
                  "summary": candidates[i].get("summary"), "link": candidates[i].get("link")}
                 for i in rest],
        "이미_보낸_뉴스": [{"title": h.get("title"), "summary": h.get("summary"),
                            "sent_at": h.get("sent_at")} for h in history],
    }
    # --setting-sources "" · --strict-mcp-config: 이 기기의 CLAUDE.md·설정·MCP 서버를 프롬프트에
    # 싣지 않는다. 2026-09-10 실측 — 같은 입력·같은 판정에서 $0.4637 → $0.0438 (10배).
    # 도구를 안 쓰는 호출이라 설정이 필요 없다.
    proc = subprocess.run(
        [config.CLAUDE_EXE, "-p", "--model", MODEL, "--output-format", "json",
         "--json-schema", SCHEMA, "--tools", "", "--setting-sources", "", "--strict-mcp-config"],
        input=PROMPT + json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 실패(rc={proc.returncode}): {(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    out = data.get("structured_output") or json.loads(data["result"])
    allowed = set(rest)
    dups = {d.get("idx") for d in (out.get("dups") or []) if isinstance(d, dict)}
    new = {i for i in (out.get("new") or []) if isinstance(i, int) and i in allowed}
    # 모델이 어느 쪽에도 안 넣은 후보는 새 뉴스로 친다(누락 = 삭제가 되지 않게).
    new |= allowed - new - dups
    return sorted(new), data.get("total_cost_usd", 0.0)


# ---- 발송 이력 (data/cache/sent_news.jsonl) ----

def load_sent(days=HISTORY_DAYS, path=None):
    """최근 days 일 안에 보낸 뉴스 목록. 파일이 없거나 줄이 깨졌으면 그 줄만 건너뛴다."""
    p = Path(path) if path else SENT_PATH
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if datetime.fromisoformat(str(rec.get("sent_at"))) >= cutoff:
                out.append(rec)
        except Exception:
            continue
    return out


def append_sent(items, days=HISTORY_DAYS, path=None):
    """보낸 뉴스를 이력에 남긴다. days 보다 오래된 줄은 이때 같이 버린다(파일 무한 증식 방지).

    쓰기는 임시파일 후 os.replace — 도중에 죽어도 이력이 반쪽으로 남지 않는다."""
    p = Path(path) if path else SENT_PATH
    now = datetime.now().isoformat(timespec="seconds")
    rows = load_sent(days, p)
    rows += [{"sent_at": now, "section": it.get("section"), "title": it.get("title"),
              "summary": it.get("summary"), "link": it.get("link"), "ids": list(it.get("ids") or [])}
             for it in items]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, p)
    return len(rows)


def _selftest():
    """claude 없이 도는 점검 — 사전 필터와 이력 저장이 깨지면 여기서 걸린다."""
    cands = [
        {"section": "macro", "title": "美 9월 CPI 3.1%", "summary": "CPI 3.1%", "ids": [1], "link": "", "mentions": 2},
        {"section": "stock", "title": "삼성전자 3분기 영업익 9조", "summary": "9조", "ids": [2], "link": "https://a.com/x", "mentions": 3},
        {"section": "industry", "title": "조선 수주 잔고 사상 최대", "summary": "잔고", "ids": [3], "link": "", "mentions": 1},
    ]
    assert find_new(cands, []) == [0, 1, 2], "이력이 없으면 전부 새 뉴스"
    assert find_new([], [{"title": "x", "sent_at": datetime.now().isoformat()}]) == []
    # 제목 완전 일치(기호·공백만 다름)와 링크 일치는 claude 없이 걸러진다.
    # claude 를 스텁으로 막고 '남은 후보만' 넘어가는지까지 확인한다.
    global _ask_claude
    hist = [{"title": "美 9월 CPI  3.1%!", "summary": "", "link": "", "sent_at": datetime.now().isoformat()},
            {"title": "전혀 다른 제목", "summary": "", "link": "http://a.com/x/", "sent_at": datetime.now().isoformat()}]
    real, asked = _ask_claude, []
    _ask_claude = lambda c, h, rest: (asked.append(list(rest)) or (list(rest), 0.0))
    try:
        assert find_new(cands, hist) == [2], "제목·링크가 같은 후보는 사전 필터에서 빠진다"
        assert asked == [[2]], f"claude 에는 사전 필터를 통과한 후보만 넘어간다: {asked}"
    finally:
        _ask_claude = real
    # 이력 저장·로드 (임시 경로, 실제 이력 파일은 건드리지 않는다)
    tmp = Path(config.DATA_ROOT) / "cache" / "sent_news.selftest.jsonl"
    tmp.unlink(missing_ok=True)
    append_sent(cands[:2], path=tmp)
    got = load_sent(path=tmp)
    assert [r["title"] for r in got] == [c["title"] for c in cands[:2]], got
    assert append_sent(cands[2:], path=tmp) == 3, "append 는 기존 줄을 지우지 않는다"
    old = {"sent_at": (datetime.now() - timedelta(days=9)).isoformat(), "title": "옛날 뉴스"}
    with open(tmp, "a", encoding="utf-8") as f:
        f.write(json.dumps(old, ensure_ascii=False) + "\n")
    assert len(load_sent(path=tmp)) == 3, "3일보다 오래된 줄은 이력에서 빠진다"
    tmp.unlink(missing_ok=True)
    print("[dedup] selftest OK")


if __name__ == "__main__":
    if "--history" in sys.argv[1:]:
        rows = load_sent()
        print(f"[dedup] 최근 {HISTORY_DAYS}일 발송 {len(rows)}건 -> {SENT_PATH}")
        for r in rows[-20:]:
            print(f"  {r.get('sent_at')} [{r.get('section')}] {r.get('title')}")
    else:
        _selftest()
