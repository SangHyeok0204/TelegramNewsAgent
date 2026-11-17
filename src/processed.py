"""raw CSV → claude(sonnet)로 토픽 생성 → processed CSV (M11 자동화).

전체 방의 미처리 글을 모아 청크(CHUNK글) 단위로 claude를 한 번에 호출한다.
방마다 호출하던 고정비(시스템 프롬프트 캐시)를 청크당 1회로 줄여 비용을 절감.
- 데일리/위클리(여러 종목) → 대표 1~2개 + 핵심 한 문장
- 단일 공시/단신/리포트 → 한 문장(종목명·수치 유지)
- 링크 글은 수집 때 받아 둔 [링크 본문]·[첨부 PDF] 를 그대로 쓴다(2026-09-10, src/enrich.py)
- 다이제스트 글(기사 링크 2개 이상)은 **링크 단위로 쪼개** 각각 한 행이 된다(2026-09-14, owner 결정).
  processed CSV 의 msg_id 는 "4686#1".."4686#7" 처럼 하위 번호가 붙고, 링크마다 본문을 따로 받아 토픽을 만든다.
  DB(messages) 의 게시물 단위 PK 는 그대로 — 쪼개는 건 이 층에서만 한다.
- 그게 없을 때만 WebFetch, 실패 시 "[URL 링크: <url>]"
- text·url 모두 없으면 → 빈 문자열

사용:
    python -m src.processed                 # 전체 방(watchlist)
    python -m src.processed --chat 한투해투   # 단일 방
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import csv
import json
import os
import subprocess

from . import channels, config, db, enrich
from .export_csv import URL_RE

MODEL = "sonnet"
COLUMNS = ["date", "time", "msg_id", "topic", "category"]
CLAUDE_TIMEOUT = 900  # 초
CHUNK = 30  # 한 claude 호출당 최대 글 수
PENDING_DAYS = 3  # 이보다 오래된 미처리 글은 토픽 생성 안 함
# 청킹 입력 상한(2026-09-10 링크 본문·PDF 첨부 추가) — 한 행이 프롬프트를 잡아먹지 않게
EXTRA_CHARS = 2000  # [링크 본문]·[첨부 PDF] 각각의 상한 (2026-09-16: 800→2000, 파서 개선분이 claude 에 보이게. 청크당 sonnet 비용 상승은 owner 승인)
ROW_CHARS = 3000   # 한 행 전체 상한 (2026-09-16: 1600→3000)
SPLIT_MIN_URLS = 2  # 기사 링크가 이만큼 이상이면 다이제스트 → 링크 단위로 쪼갠다 (2026-09-14)
FETCH_WORKERS = 8   # 쪼갠 링크 본문 병렬 수집 스레드 수

# 게시물 분류(category) — 세 축 + 기타. HTML 섹션 구성의 기준.
CATEGORIES = ["매크로", "산업", "종목", "기타"]

PROMPT_RULES = (
    "당신은 한국 증권가 텔레그램 메시지에서 (1) 한 줄 토픽(헤드라인)과 "
    "(2) 게시물 분류(category)를 뽑는다.\n"
    "아래 입력의 각 글(idx, text, url)마다 topic 과 category 를 만들어 idx 로 매핑한다.\n"
    "\n[topic 규칙]\n"
    "- 여러 종목/뉴스 묶음(데일리/위클리) → 대표 1~2개 + 핵심을 한 문장으로\n"
    "- 단일 공시/단신/리포트 → 한 문장(종목명·핵심 수치 유지)\n"
    "- text 안에 \"[링크 본문]\" 또는 \"[첨부 PDF]\" 가 있으면 그 본문으로 토픽을 만든다"
    "(수집할 때 미리 받아 둔 기사 본문·PDF 텍스트다 — WebFetch 하지 않는다).\n"
    "- url 은 있는데 \"[링크 본문]\" 이 없을 때만 → WebFetch로 그 기사를 읽어 한 문장 토픽. "
    "읽기 실패해도 URL 경로에 기사 제목이 보이면(예: reuters.com/.../anthropic-ceo-urges-ai-companies-slow-model-development) "
    "그 제목을 한국어 한 문장 토픽으로 옮긴다. 제목도 없으면 정확히 \"[URL 링크: <그 url>]\" 형식으로.\n"
    "- text도 url도 없으면 → 빈 문자열 \"\"\n"
    "- 항목은 한 게시물을 링크 단위로 쪼갠 조각일 수 있다(idx 가 다른데 머리말이 같은 경우). "
    "그때는 그 항목의 url·[링크 본문] 기사 하나만으로 토픽을 만든다(게시물 전체를 요약하지 않는다).\n"
    "\n[category 규칙] 다음 넷 중 하나로만 분류한다:\n"
    "- \"매크로\": 금리·환율·유가·원자재, 경제지표(고용·물가·GDP 등), 중앙은행·통화/재정정책, "
    "지수·시황 전반, 채권 등 거시(macro) 이슈가 중심인 글.\n"
    "- \"산업\": 특정 산업·섹터·테마(반도체·2차전지·조선·방산·바이오·AI·자동차 등) 전반의 "
    "업황·정책·밸류체인 동향이 중심인 글(개별 기업보다 섹터 차원).\n"
    "- \"종목\": 특정 상장사(개별 기업) 중심 글 — 실적·공시·목표주가·수급·이벤트 등.\n"
    "- \"기타\": 위 셋에 해당하지 않는 글(잡담·인사·광고·링크만·빈 글 등).\n"
    "판단이 애매하면 글의 '가장 핵심적인' 성격 하나로 정한다.\n"
    "\n한국어 한 문장(구 형태 가능). 입력의 모든 idx 에 대해 빠짐없이 topic 과 category 를 낼 것.\n"
    "입력(JSON):\n"
)

SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "idx": {"type": "integer"},
                        "topic": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                    },
                    "required": ["idx", "topic", "category"],
                },
            }
        },
        "required": ["topics"],
    },
    ensure_ascii=False,
)


def _read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def pending(alias):
    """raw 중 processed에 아직 없는 행(미처리 글)을 raw 순서대로 반환."""
    raw = _read_csv(config.RAW_DIR / f"{alias}.csv")
    # 쪼갠 행("4686#3")은 원 msg_id 로 환산해 비교한다 — 안 그러면 다이제스트 글이 영원히 미처리로 남는다
    done = {r["msg_id"].split("#", 1)[0] for r in _read_csv(config.PROCESSED_DIR / f"{alias}_processed.csv")}
    # 오래 꺼져 있다 켜면 밀린 글 전부에 claude 를 돌린다(2026-09-08: 2주치 21k건 ≈ $200).
    # 리포트/대시보드는 최근 24h 만 보므로 PENDING_DAYS 보다 오래된 글은 영구 건너뜀.
    cutoff = (datetime.now() - timedelta(days=PENDING_DAYS)).strftime("%Y-%m-%d")
    return [r for r in raw if r["msg_id"] not in done and r["date"] >= cutoff]


def split_digest(text):
    """다이제스트 글 → [(조각 텍스트, url)]. 기사 링크가 SPLIT_MIN_URLS 미만이면 None.

    조각 i = 직전 링크 끝 ~ 이 링크 끝(링크 포함). 마지막 조각은 뒤에 남은 꼬리까지 가진다.
    t.me·유튜브 같은 SKIP_HOSTS 링크와 중복 링크는 자를 지점으로 안 쓴다(글 서명용 채널 링크가 조각이 되지 않게).
    # ponytail: 위치 기반 단순 분할 — 링크가 글 끝에 몰려 있는 형식이면 조각 텍스트가 링크뿐이라 [링크 본문]에 의존한다
    """
    text = text or ""
    cuts, seen = [], set()
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(').,"\'>')
        if url in seen or enrich._skip_host(url):
            continue
        seen.add(url)
        cuts.append((m.start() + len(url), url))
    if len(cuts) < SPLIT_MIN_URLS:
        return None
    out, prev = [], 0
    for i, (end, url) in enumerate(cuts):
        stop = len(text) if i == len(cuts) - 1 else end
        out.append((text[prev:stop].strip().lstrip(">)]」』 \n\t").rstrip(), url))  # 직전 링크의 닫는 괄호 제거
        prev = end
    return out


def _explode(r, text, url_text):
    """raw 행 1개 → 쪼갠 하위 행들(msg_id 에 #n). 다이제스트가 아니면 None.
    첫 링크 본문은 수집 때 DB 에 받아 둔 url_text 를 재사용하고 나머지만 새로 받는다."""
    parts = split_digest(text)
    if not parts:
        return None
    first = URL_RE.search(text).group(0).rstrip(').,"\'>')  # collect._first_url 과 같은 규칙

    def body(url):
        if url == first and url_text:
            return url_text
        return enrich.fetch_url_text(url)[1]

    with ThreadPoolExecutor(FETCH_WORKERS) as ex:
        bodies = list(ex.map(body, [u for _, u in parts]))
    rows = []
    for i, ((seg, url), b) in enumerate(zip(parts, bodies), 1):
        sub = dict(r, msg_id=f"{r['msg_id']}#{i}", url=url)
        sub["text"] = (seg + (f"\n[링크 본문] {b[:EXTRA_CHARS]}" if b else ""))[:ROW_CHARS]
        rows.append(sub)
    return rows


def _pending_with_text(ch):
    """pending(alias) 에 본문을 붙인다. raw CSV 에는 text 가 없으므로(2026-09-08) DB 에서 (chat_id, msg_id) 로 읽는다.

    2026-09-10: 수집 때 저장해 둔 링크 본문(url_text)·PDF 첨부 텍스트(attach_text)를 같이 붙인다.
    첨부만 있는 글(text 가 빈 글, 하루 152건)도 이제 내용이 들어가 토픽이 나온다."""
    rows = pending(ch["alias"])
    if not rows:
        return rows
    conn = db.connect()
    out = []
    for r in rows:
        hit = conn.execute(
            "SELECT text, url_text, attach_text FROM messages WHERE chat_id = ? AND msg_id = ?",
            (ch["id"], int(r["msg_id"]))).fetchone()
        text, url_text, attach_text = hit if hit else ("", "", "")
        subs = _explode(r, text, url_text)  # 2026-09-14: 다이제스트 글은 링크 단위 행으로
        if subs:
            out.extend(subs)
            continue
        parts = [text or ""]
        if url_text:
            parts.append(f"[링크 본문] {url_text[:EXTRA_CHARS]}")
        if attach_text:
            parts.append(f"[첨부 PDF] {attach_text[:EXTRA_CHARS]}")
        r["text"] = "\n".join(p for p in parts if p)[:ROW_CHARS]
        out.append(r)
    conn.close()
    return out


def _claude(rows):
    """rows(<=CHUNK) → ({local_idx: topic}, cost). 글들을 한 번의 호출로 처리."""
    items = [{"idx": i, "text": r.get("text", ""), "url": r.get("url", "")} for i, r in enumerate(rows)]
    prompt = PROMPT_RULES + json.dumps(items, ensure_ascii=False)
    # 프롬프트는 stdin으로 전달(긴 청크가 Windows 명령줄 길이 한계를 넘지 않도록)
    # --setting-sources "" · --strict-mcp-config: 이 기기의 CLAUDE.md·설정·MCP 서버를 프롬프트에
    # 싣지 않는다. 청킹에 필요한 건 --allowedTools WebFetch 하나뿐이라 설정이 없어도 된다.
    # 2026-09-10 실측(같은 2행 청크, 같은 분류·동등한 토픽, WebFetch 정상): $0.2051 → $0.0710.
    proc = subprocess.run(
        [config.CLAUDE_EXE, "-p", "--model", MODEL, "--output-format", "json",
         "--json-schema", SCHEMA, "--allowedTools", "WebFetch",  # 2026-09-10: config.CLAUDE_EXE
         "--setting-sources", "", "--strict-mcp-config"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 실패(rc={proc.returncode}): {(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    cost = data.get("total_cost_usd", 0.0)
    out = data.get("structured_output") or json.loads(data["result"])
    return {int(t["idx"]): (t["topic"], t.get("category") or "기타") for t in out["topics"]}, cost


def _ensure_columns(path):
    """구 4컬럼 processed CSV(category 없음)를 5컬럼으로 업그레이드(기존 행 category 공란). 멱등."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or "category" in rows[0]:
        return
    rows[0] = rows[0] + ["category"]
    for i in range(1, len(rows)):
        rows[i] = rows[i] + [""]
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    os.replace(tmp, path)


def _append(alias, triples):
    """triples: [(raw_row, topic, category)] → processed CSV에 append (date/time/msg_id는 raw 그대로)."""
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = config.PROCESSED_DIR / f"{alias}_processed.csv"
    new = not out.exists()
    if not new:
        _ensure_columns(out)  # 구 4컬럼 파일이면 append 전에 5컬럼으로 승격
    enc = "utf-8-sig" if new else "utf-8"  # 새 파일만 BOM
    with open(out, "a", newline="", encoding=enc) as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        for r, topic, category in triples:
            w.writerow([r["date"], r["time"], r["msg_id"], topic, category])  # 쪼갠 행은 "4686#3" 꼴


def process_channel(alias):
    """단일 방: 미처리 글 토픽 생성 → append. (건수, cost)."""
    ch = next((c for c in channels.load_channels() if c["alias"] == alias), None)
    rows = _pending_with_text(ch) if ch else []
    if not rows:
        print(f"[processed] {alias}: 새 글 0건")
        return 0, 0.0
    topics, cost = _claude(rows)
    _append(alias, [(r, *topics.get(i, ("", "기타"))) for i, r in enumerate(rows)])
    print(f"[processed] {alias}: {len(rows)}건 토픽 생성 (${cost:.3f})")
    return len(rows), cost


def process_all():
    """전체 방의 미처리 글을 모아 청크 단위로 한 번에 처리(비용 절감)."""
    # 매 실행 watchlist.csv 재로딩 — 상주 스케줄러가 재기동 없이 신규 방을 처리 대상에 포함한다.
    jobs = [(ch["alias"], r) for ch in channels.load_channels() for r in _pending_with_text(ch)]
    if not jobs:
        print("[processed] 새 글 0건 — claude 호출 없음")
        return
    out_topics = {}
    total_cost = 0.0
    n_chunks = 0
    n_failed = 0
    for start in range(0, len(jobs), CHUNK):
        n_chunks += 1
        rows = [r for _, r in jobs[start:start + CHUNK]]
        try:
            topics, cost = _claude(rows)
            for li, tp in topics.items():
                out_topics[start + li] = tp
            total_cost += cost
            print(f"[processed] 청크 {start // CHUNK + 1}: {len(rows)}건 (${cost:.3f})")
        except Exception as e:
            n_failed += 1
            print(f"[processed] 청크 {start // CHUNK + 1} ERROR: {e}")
    if n_chunks and n_failed == n_chunks:
        # 2026-09-10: 전부 실패는 성공이 아니다 — 작업 실패로 올려 _try_task 가 재시도·3회 경보를 낸다
        raise RuntimeError(f"processed 청크 {n_chunks}개 전부 실패")
    # 성공한 글만 방별로 모아 append (실패분은 다음 사이클에 재시도 — 멱등)
    by_alias = {}
    for gi, (alias, r) in enumerate(jobs):
        if gi in out_topics:
            topic, category = out_topics[gi]
            by_alias.setdefault(alias, []).append((r, topic, category))
    for alias, triples in by_alias.items():
        _append(alias, triples)
    print(f"[processed] 한 바퀴 완료 — 새 토픽 {len(out_topics)}/{len(jobs)}건 (총 ${total_cost:.3f})")


def _check():
    """split_digest 자체 점검 — 한투 테크팀 Tech Daily 형식 + 서명용 t.me 링크."""
    sample = ("[한투증권 Tech Team] Tech Daily\n● Daily News Clipping\n\n"
              "▶️ 삼성전자·CXMT LPDDR5X 공급 < https://vo.la/a1 >\n\n"
              "▶️ 삼성 파운드리 2nm 지연 (Trendforce) < https://vo.la/b2 >\n\n"
              "▶️ 키옥시아 ADR 상장 < https://vo.la/c3 >\n\nhttps://t.me/KISemicon")
    parts = split_digest(sample)
    assert [u for _, u in parts] == ["https://vo.la/a1", "https://vo.la/b2", "https://vo.la/c3"], parts
    assert parts[0][0].startswith("[한투증권") and "LPDDR5X" in parts[0][0]
    assert parts[1][0].startswith("▶️ 삼성 파운드리") and "vo.la/b2" in parts[1][0]
    assert parts[2][0].startswith("▶️ 키옥시아") and parts[2][0].endswith("https://t.me/KISemicon")
    assert split_digest("단신 하나 https://vo.la/x") is None          # 링크 1개 → 안 쪼갠다
    assert split_digest("https://vo.la/x 같은 링크 https://vo.la/x") is None  # 중복 링크
    assert split_digest("") is None
    print("split_digest ok")


def main():
    ap = argparse.ArgumentParser(prog="src.processed")
    ap.add_argument("--chat", help="단일 방 alias 또는 id. 생략 시 watchlist 전체")
    ap.add_argument("--check", action="store_true", help="split_digest 자체 점검")
    args = ap.parse_args()
    if args.check:
        _check()
        return
    if args.chat:
        m = next((c for c in channels.CHANNELS if c["alias"] == args.chat or str(c["id"]) == args.chat), None)
        process_channel(m["alias"] if m else args.chat)
    else:
        process_all()


if __name__ == "__main__":
    main()
