# -*- coding: utf-8 -*-
"""TelegramNewsAgent Slack 카드 (2026-09-10).

`data/output/dashboard_analysis.json`(대시보드 집계 payload)을 그대로 받아 Slack 카드로 옮긴다.
JSON 자체는 손대지 않는다 — MABS TradingDesk 가 계속 그 파일을 읽는다.

문장 규칙: 짧고 쉬운 말(ELI5). 값은 굵게. 헤더 + 불릿 + 컨텍스트 3단 (`Agents/CLAUDE.md`).
발송은 공용 `Agents/slack_notify.py` 하나만 거친다(표준 라이브러리 전용).

    .venv\\Scripts\\python -X utf8 -m src.slack_cards --dry-run [--new]    # blocks JSON 만 출력
    .venv\\Scripts\\python -X utf8 -m src.slack_cards --send [--new]       # 실제 발송
    .venv\\Scripts\\python -X utf8 -m src.slack_cards --send-login-alert   # 로그인 경보 발송
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import config

SECTIONS = (("macro", "🌐 매크로"), ("industry", "🏭 산업"), ("stock", "📈 종목"))
TOP_N = 5            # 섹션당 상위 토픽 줄 수 (2026-10-03: 3+2 → 5+3, 집계 여유분 전부 표시)
NOTABLE_N = 3        # 섹션당 특이 토픽 줄 수
SUMMARY_MAX = 50     # 요약 최대 한글 글자수
LINES_MAX = 2800     # 본문 섹션 여유분 (Slack section 상한 3000)
REASON_MAX = 120     # 실패 사유 최대 길이


def _slack_notify():
    """공용 모듈은 Agents/ 폴더에 있다 — 이 파일(src/) 기준 두 단계 위."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import slack_notify
    return slack_notify


def _dt(value):
    """ISO 문자열 → datetime. 없거나 깨졌으면 현재 시각."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now()


def _summary(text, limit):
    """'·' 로 나뉜 사실 중 앞 2개만 남기고 limit 글자까지 자른다."""
    if limit <= 0:
        return ""
    facts = [p.strip() for p in str(text or "").split("·") if p.strip()]
    s = " · ".join(facts[:2])
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _top_line(item, limit):
    """`• <링크|제목> — 요약 (언급 N)`. 링크가 없으면 제목만."""
    title = str(item.get("title") or "").strip() or "제목 없음"
    link = str(item.get("link") or "").strip()
    line = f"• <{link}|{title}>" if link else f"• {title}"
    s = _summary(item.get("summary"), limit)
    if s:
        line += f" — {s}"
    mentions = item.get("mentions") or 0
    if mentions >= 2:
        line += f" (언급 {mentions})"
    return line


def _body(sections, notables, limit):
    """매크로 → 산업 → 종목 순으로 소제목 + 상위/특이 줄. 섹션 사이 빈 줄."""
    out = []
    for key, heading in SECTIONS:
        sec = sections.get(key) or {}
        tops = list(sec.get("top") or [])[:TOP_N]
        nots = list(sec.get("notable") or [])[:NOTABLE_N] if notables else []
        if not tops and not nots:
            continue
        if out:
            out.append("")
        out.append(f"*{heading}*")
        out += [_top_line(t, limit) for t in tops]
        out += [f"◦ {str(n.get('title') or '').strip()}" for n in nots]
    return out


def dashboard_card(payload):
    """집계 payload 전체 → (title, lines, context). 발송은 하지 않는다.

    중복 제거 없이 '지금 집계 스냅샷'을 그대로 보여 준다 — 상주 스케줄러는 쓰지 않고
    사람이 CLI 로 확인·발송할 때만 쓴다(정기 발송은 new_news_card 쪽이다)."""
    payload = payload or {}
    sections = payload.get("sections") or {}
    gen = _dt(payload.get("generatedAt"))
    end = _dt(payload.get("windowEnd") or payload.get("generatedAt"))
    hours = payload.get("windowHours") or 24

    title = f"📰 텔레그램 뉴스 브리핑 · {end:%m-%d %H:%M} (최근 {hours}h)"

    # 길면 특이 토픽부터 버리고, 그래도 길면 요약을 버린다(제목·링크는 끝까지 남긴다).
    for notables, limit in ((True, SUMMARY_MAX), (False, SUMMARY_MAX), (False, 0)):
        lines = _body(sections, notables, limit)
        if len("\n".join(lines)) <= LINES_MAX:
            break
    if not lines:
        lines = [f"새 토픽 없음 (최근 {hours}h)"]

    context = (f"토픽 *{payload.get('topics') or 0}* · 방 *{payload.get('rooms') or 0}* · "
               f"비용 ${float(payload.get('costUsd') or 0):.2f} · 생성 {gen:%H:%M}")
    return title, lines, context


def new_news_card(payload, new_items, total, dedup_cost=0.0):
    """중복을 뺀 '새 뉴스'만 담은 매 정시 카드 (2026-09-10).

    new_items 는 `news_dedup.candidates_from` 형식({section, title, summary, link, mentions}) 목록.
    본문 레이아웃은 dashboard_card 와 같은 `_body` 를 쓴다 — 섹션별 한 묶음."""
    payload = payload or {}
    end = _dt(payload.get("windowEnd") or payload.get("generatedAt"))
    sections = {}
    for it in new_items:
        sections.setdefault(it.get("section"), {"top": [], "notable": []})["top"].append(it)

    title = f"📰 새 뉴스 · {end:%m-%d %H}:00 (최근 24h 기준)"
    for limit in (SUMMARY_MAX, 0):  # 길면 요약부터 버린다(제목·링크는 남긴다)
        lines = _body(sections, False, limit)
        if len("\n".join(lines)) <= LINES_MAX:
            break
    cost = float(payload.get("costUsd") or 0) + float(dedup_cost or 0)
    context = (f"새 *{len(new_items)}*건 / 후보 {total} · 중복 {max(total - len(new_items), 0)}건 제외 · "
               f"비용 ${cost:.2f}")
    return title, lines, context


def _now_context():
    return f"TelegramNewsAgent · {datetime.now():%H:%M}"


def login_alert_card():
    """세션 만료 — 사람이 login.bat 을 돌려야 한다(하루 1회만 보낸다)."""
    return ("⚠️ 텔레그램 로그인 필요",
            ["세션이 만료돼 수집이 멈췄어요",
             "조치: 데스크탑에서 *login.bat* 실행 → 코드 입력",
             "로그인 전까지 리포트·집계는 건너뜁니다 (비용 0)"],
            _now_context())


def recovered_card():
    return ("✅ 텔레그램 수집 재개", ["로그인이 확인돼 수집·리포트를 다시 합니다"], _now_context())


def task_failed_card(label, reason):
    """재시도 3회 상한에 걸린 작업 — 그 슬롯은 포기한다."""
    return (f"❌ 작업 3회 실패: {label}",
            [str(reason or "")[:REASON_MAX],
             "오늘은 더 시도하지 않아요. 로그 확인: data/cache/collect_log.txt"],
            _now_context())


def send(title, lines, context):
    """카드를 보낸다. 어떤 경우에도 예외를 올리지 않고 응답 dict 를 돌려준다."""
    try:
        res = _slack_notify().send_card(title, lines=lines, context=context)
    except Exception as e:                       # import·경로 실패도 수집을 멈추지 않는다
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    status = f"ok ts={res.get('ts')}" if res.get("ok") else f"error={res.get('error')}"
    print(f"[slack] {title} → {status}", flush=True)  # 토큰은 절대 찍지 않는다
    return res


def load_payload(path=None):
    """대시보드 집계 JSON 로드. 없거나 깨졌으면 None."""
    p = Path(path) if path else config.OUTPUT_DIR / "dashboard_analysis.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[slack] 집계 JSON 읽기 실패: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# 카드 v2 (2026-09-10 owner 요청): 분야별 핵심 3 + 놓칠 뉴스 2, 헤드라인 하이퍼링크,
# 기사 썸네일(og:image), 키워드는 한 줄씩 줄바꿈. Block Kit 블록을 직접 만든다.
# ---------------------------------------------------------------------------
import re
import urllib.request

OG_CACHE = config.DATA_ROOT / "cache" / "og_cache.json"
OG_TIMEOUT = 5          # 링크당 썸네일 조회 최대 초
OG_MAX_BYTES = 200_000  # 페이지 앞부분만 읽는다 (og:image 는 head 에 있다)
KW_MAX = 5              # 헤드라인당 키워드 줄 수
BLOCKS_MAX = 48         # Slack 메시지 블록 상한 50
CARD_BODY_MAX = 200     # card.body 상한 (Slack 문서, 2026-09)
CAROUSEL_MAX = 10       # carousel 당 card 상한


def _og_cache():
    try:
        return json.loads(OG_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def og_image(url):
    """기사 URL → og:image URL (없으면 ""). 결과는 og_cache.json 에 캐시(실패도 캐시해 재조회 안 함)."""
    url = (url or "").strip()
    if not url.startswith("http"):
        return ""
    cache = _og_cache()
    if url in cache:
        return cache[url]
    img = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TelegramNewsAgent/1.0"})
        with urllib.request.urlopen(req, timeout=OG_TIMEOUT) as r:
            head = r.read(OG_MAX_BYTES).decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', head, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', head, re.I))
        if m and m.group(1).startswith("http"):
            img = m.group(1)
    except Exception:
        img = ""
    cache[url] = img
    try:
        OG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        OG_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return img


def _link(item):
    title = str(item.get("title") or "").strip().replace("<", "〈").replace(">", "〉")
    link = str(item.get("link") or "").strip()
    return f"<{link}|{title}>" if link.startswith("http") else title


def _keywords(item):
    """summary 'a · b · c' → 키워드 목록(최대 KW_MAX)."""
    raw = str(item.get("summary") or "")
    parts = [p.strip(" ·•-") for p in re.split(r"\s·\s|\n", raw)]  # 구분자는 " · " 뿐 — 쉼표(3,500억)·붙은 ·(주식·금)는 자르지 않는다
    return [p for p in parts if p][:KW_MAX]


def _item_card(item, thumbs=True):
    """뉴스 1건 → card (2026-09-14, carousel 안에 넣는다).

    헤드라인 + '핵심 키워드' 불릿 템플릿은 section 시절 그대로 — title 에 헤드라인, body 에 불릿.
    body 는 200자 상한이라 넘치는 불릿은 뒤에서부터 뺀다. 링크는 '기사 보기' 버튼(title 하이퍼링크는 문서에 없다)."""
    title = str(item.get("title") or "").strip().replace("<", "〈").replace(">", "〉")
    kws = _keywords(item)
    body = "*핵심 키워드*"
    for k in kws:
        cand = f"{body}\n• {k}"
        if len(cand) > CARD_BODY_MAX:
            break
        body = cand
    link = str(item.get("link") or "").strip()
    if link.startswith("http"):  # 2026-09-14 owner: 제목에도 링크. title 은 mrkdwn 이라 <url|text> 가 들어간다
        title = f"<{link}|{title[:150 - len(link) - 3]}>"
    card = {"type": "card",
            "title": {"type": "mrkdwn", "text": title[:150]},
            "body": {"type": "mrkdwn", "text": body}}
    if link.startswith("http"):
        card["actions"] = [{"type": "button", "text": {"type": "plain_text", "text": "기사 보기"},
                            "url": link, "action_id": "open_article"}]
    img = og_image(link) if thumbs else ""
    if img:
        card["hero_image"] = {"type": "image", "image_url": img, "alt_text": "기사 썸네일"}
    return card


def _item_section(item, thumbs=True):
    """뉴스 1건 → section 블록 (2026-09-20 owner: carousel 은 한 화면에 1건만 보여 세로 목록으로 되돌림).
    본문 = 링크 제목(굵게) + '핵심 키워드' 불릿, 오른쪽 accessory 에 og:image 썸네일."""
    text = f"*{_link(item)}*\n*핵심 키워드*" + "".join(f"\n• {k}" for k in _keywords(item))
    block = {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}
    img = og_image(item.get("link")) if thumbs else ""
    if img:
        block["accessory"] = {"type": "image", "image_url": img, "alt_text": "기사 썸네일"}
    return block


def shown_items(new_items):
    """카드에 실제로 실리는 항목 — 분류 순서대로 top TOP_N + notable NOTABLE_N (2026-09-17).
    발송 이력(news_dedup.append_sent)에는 이 목록만 남긴다 — 여유분(5+3)에서 잘린 항목을 '보냈다'고 적으면 다음 회차에 영영 안 나온다."""
    out = []
    for key, _ in SECTIONS:
        tops = [i for i in new_items if i.get("section") == key and i.get("kind", "top") == "top"][:TOP_N]
        nots = [i for i in new_items if i.get("section") == key and i.get("kind") == "notable"][:NOTABLE_N]
        out += (tops + nots)[:CAROUSEL_MAX]
    return out


def news_blocks(payload, new_items, total, dedup_cost=0.0, thumbs=True):
    """매 정시 '새 뉴스' 카드 v2 blocks. new_items 는 candidates_from 형식(kind 포함)."""
    payload = payload or {}
    end = _dt(payload.get("windowEnd") or payload.get("generatedAt"))
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"📰 새 뉴스 · {end:%m-%d %H}:00 (최근 24h 기준)"[:150]}}]
    for key, heading in SECTIONS:
        # 2026-09-14 카드 v3: 분류마다 carousel 하나, 안에 뉴스 card. top 뒤에 notable 을 같은 줄에 잇는다
        # (owner: '놓치기 쉬운 뉴스' 를 따로 빼지 않는다).
        items = [i for i in shown_items(new_items) if i.get("section") == key]
        if not items:
            continue
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{heading}*  ·  {len(items)}건"}})
        blocks += [_item_section(it, thumbs) for it in items]  # 2026-09-20: carousel → 세로 목록(한 화면에 여러 건)
    if len(blocks) == 1:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "새 뉴스 없음 (최근 24h 기준)"}})
    cost = float(payload.get("costUsd") or 0) + float(dedup_cost or 0)
    shown = len(shown_items(new_items))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"새 *{shown}*건 / 후보 {total} · 중복 {max(total - len(new_items), 0)}건 제외"
                           + (f" · 여유분 {len(new_items) - shown}건 미표시" if len(new_items) > shown else "")
                           + f" · 비용 ${cost:.2f}"}]})
    return blocks


# ── 카드 형식 하네스 (2026-09-26 owner: us_close 카드를 레퍼런스로 모든 카드에 적용) ──
LINE_MAX = 140          # 한 줄(줄바꿈 사이) 최대 글자 — 넘으면 가로로 지저분하게 흐른다
PARA_MAX = 200          # 줄바꿈 없는 문단 상한 — 요약은 불릿 여러 줄로 쪼개야 한다
FIELDS_MAX = 10         # section.fields 상한 (Slack)
BLOCKS_LIMIT = 50


def lint_blocks(blocks) -> list[str]:
    """Slack 제한 + 가독성 규칙 위반 목록. 비어 있으면 통과.
    규칙: 블록 ≤50 · header ≤150자 · section ≤3,000자 · fields ≤10 · 한 줄 ≤LINE_MAX(URL 제외)
          · 줄바꿈 없는 문단 ≤PARA_MAX · table/carousel 금지(2026-09-19/20 owner)."""
    out = []
    if len(blocks) > BLOCKS_LIMIT:
        out.append(f"블록 {len(blocks)}개 > {BLOCKS_LIMIT}")
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t in ("table", "carousel"):
            out.append(f"#{i} {t} 블록 금지")
        texts = []
        if t == "header":
            if len(b["text"]["text"]) > 150:
                out.append(f"#{i} header 150자 초과")
        elif t == "section":
            if b.get("fields"):
                if len(b["fields"]) > FIELDS_MAX:
                    out.append(f"#{i} fields {len(b['fields'])}개 > {FIELDS_MAX}")
                texts = [f.get("text", "") for f in b["fields"]]
            if b.get("text"):
                texts.append(b["text"]["text"])
        elif t == "context":
            texts = [e.get("text", "") for e in b.get("elements", []) if e.get("type") == "mrkdwn"]
        for s in texts:
            if len(s) > 3000:
                out.append(f"#{i} 텍스트 3,000자 초과")
            for line in s.split("\n"):
                if len(line) > LINE_MAX and "http" not in line:
                    out.append(f"#{i} 한 줄 {len(line)}자 > {LINE_MAX}: {line[:40]}…")
            if "\n" not in s and len(s) > PARA_MAX and "http" not in s:
                out.append(f"#{i} 줄바꿈 없는 문단 {len(s)}자 > {PARA_MAX}")
    return out


def send_blocks(blocks, text):
    """blocks 를 그대로 보낸다. 예외 없이 응답 dict. 보내기 전 lint — 위반은 로그로 남기되 발송은 막지 않는다
    (뉴스 카드는 LLM 산출이라 가끔 넘친다). us_close 처럼 결정론적 카드는 호출 전에 strict 로 막는다."""
    for p in lint_blocks(blocks):
        print(f"[card-lint] {text[:20]} — {p}", flush=True)
    try:
        res = _slack_notify().post(blocks=blocks, text=text)
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    status = f"ok ts={res.get('ts')}" if res.get("ok") else f"error={res.get('error')}"
    print(f"[slack] {text} → {status}", flush=True)
    return res


def send_extra(blocks, text):
    """2026-10-10 owner: 06:55/12:55 새 뉴스 카드만 다른 워크스페이스 웹훅(NEWS_EXTRA_WEBHOOK_URL)에도 복사 발송.
    키가 없으면 조용히 건너뛴다. 실패해도 본 발송·이력에는 영향 없다."""
    try:
        sn = _slack_notify()
        url = sn.load_env().get("NEWS_EXTRA_WEBHOOK_URL", "")
        if not url:
            return {"ok": False, "error": "no_url"}
        res = sn.post_webhook(url, blocks=blocks, text=text)
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(f"[slack-extra] {text} → {'ok' if res.get('ok') else 'error=' + str(res.get('error'))}", flush=True)
    return res


def _check(blocks):
    """Slack 제한 자체 점검 — --dry-run 이 곧 테스트다."""
    assert blocks and blocks[0]["type"] == "header", "헤더 블록 없음"
    assert len(blocks[0]["text"]["text"]) <= 150, "헤더 150자 초과"
    assert blocks[-1]["type"] == "context", "컨텍스트 블록 없음"
    for b in blocks:
        if b["type"] == "section" and "text" in b:
            assert len(b["text"]["text"]) <= 3000, "섹션 3000자 초과"
    # 빈 입력에서도 카드가 만들어져야 한다(집계가 비었을 때 예외로 스케줄러를 죽이지 않게).
    assert new_news_card({}, [], 0)[0].startswith("📰 새 뉴스"), "새 뉴스 카드 헤더 불일치"
    # 2026-09-14 카드 v3 — carousel/card 제한
    demo = [{"section": "macro", "kind": "top", "title": "t", "summary": " · ".join(["키워드" * 12] * 6),
             "link": "https://example.com/a", "ids": [1], "mentions": 1}]
    b = news_blocks({}, demo, 1, thumbs=False)
    assert not [x for x in b if x["type"] == "carousel"], "2026-09-20: carousel 금지 — 세로 section 목록"
    sec = [x for x in b if x["type"] == "section"][1]   # [0] 은 분류 머리글
    assert sec["text"]["text"].startswith("*<https://example.com/a|t>*\n*핵심 키워드*\n• ") and sec["text"]["text"].count("\n• ") == KW_MAX
    assert len(sec["text"]["text"]) <= 3000 and "accessory" not in sec
    assert "놓치기 쉬운 뉴스" not in json.dumps(news_blocks({}, demo + [dict(demo[0], kind="notable")], 2, thumbs=False), ensure_ascii=False)
    assert len(news_blocks({}, [dict(demo[0], section=s, title=f"{s}{i}") for s in ("macro", "industry", "stock") for i in range(5)], 15, thumbs=False)) <= BLOCKS_MAX
    # 2026-10-03 카드 5+3: 넘치는 후보만 잘리고, 보여 준 것만 shown_items 로 남는다
    bench = [dict(demo[0], title=f"t{i}", kind="top") for i in range(6)] + [dict(demo[0], title=f"n{i}", kind="notable") for i in range(4)]
    shown = shown_items(bench)
    assert [i["title"] for i in shown] == ["t0", "t1", "t2", "t3", "t4", "n0", "n1", "n2"], shown
    assert "여유분 2건 미표시" in json.dumps(news_blocks({}, bench, 10, thumbs=False), ensure_ascii=False)
    full = [dict(b, section=s) for s in ("macro", "industry", "stock") for b in bench]
    assert not lint_blocks(news_blocks({}, full, 30, thumbs=False)), "5+3 × 3분류 카드가 Slack 한도 안"


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--send-login-alert" in args:
        raise SystemExit(0 if send(*login_alert_card()).get("ok") else 1)

    payload = load_payload()
    if payload is None:
        raise SystemExit(2)
    if "--new" in args:
        # 매 정시 '새 뉴스' 카드 미리보기 — 중복 판정 없이(이력 비었다고 보고) 후보 전부를 그린다.
        from .news_dedup import candidates_from
        cands = candidates_from(payload)
        title, lines, context = new_news_card(payload, cands, len(cands))
    else:
        title, lines, context = dashboard_card(payload)
    if "--send" in args:
        raise SystemExit(0 if send(title, lines, context).get("ok") else 1)
    blocks = _slack_notify().card(title, lines, None, context)
    _check(blocks)
    print(json.dumps(blocks, ensure_ascii=False, indent=2))
