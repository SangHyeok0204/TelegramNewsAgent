"""최근 24h 토픽 집계 — Opus(+/team) 1콜로 매크로·산업·종목별 이슈를 뽑아 JSON 으로 남긴다.

`collect.py` 의 매 정시 작업이 부른다. 산출물은 `output/dashboard_analysis.json` 하나이고
MABS TradingDesk 와 Slack '새 뉴스' 카드가 그 파일(또는 반환 payload)을 읽는다.

2026-09-10: 일간 HTML 리포트(`텔레그램모니터_YYMMDD.html`)는 owner 결정으로 철회됐다.
generate·render_html·import_from_html 과 CSS·하이라이트 블록을 이 파일에서 지웠다 —
매 정시 카드가 그 역할을 대신한다. 파일 이름은 이력 때문에 그대로 둔다.

사용:
    python -m src.daily_report                     # 실행시각 기준 최근 24h 집계
    python -m src.daily_report --date 2026-06-30   # 그 날 07:00 기준 최근 24h 집계
"""
import argparse
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import channels, config
from .processed import _read_csv  # CSV 읽기 재사용

KST = timezone(timedelta(hours=9))
MODEL = "opus"
CLAUDE_TIMEOUT = 1500  # 25분. /team 은 워커 여럿이 링크까지 찾아 오래 걸리지만,
# 한 번의 호출이 두 정시 슬롯에 걸치면 안 된다 — 매달린 콜은 여기서 끊는다 (2026-09-10).

# 집계 모델 — 기본 Opus + /team. 콜당 $5~9 이고 매 정시면 하루 $120~200 이지만
# owner 가 그 비용을 받아들이고 품질을 택했다 (2026-09-10 결정).
# 싸게 돌려야 할 때만 환경변수로 내린다: TELEGRAM_BOT__AGG_MODEL=sonnet · TELEGRAM_BOT__AGG_TEAM=0.
AGG_MODEL = os.getenv("TELEGRAM_BOT__AGG_MODEL") or MODEL
AGG_TEAM = (os.getenv("TELEGRAM_BOT__AGG_TEAM") or "1").strip().lower() in ("1", "true", "yes")

# 각 이슈: title + summary(키워드/수치를 ' · '로 나열, 문장 금지) + ids(근거 토픽 id) + link(대표 기사)
# mentions 는 모델이 적지 않는다 — ids 를 코드가 세서 넣는다(_finalize). 2026-09-08 사용자 요청.
_ISSUE = {"type": "object", "properties": {
    "title": {"type": "string"}, "summary": {"type": "string"},
    "ids": {"type": "array", "items": {"type": "integer"}},
    "link": {"type": "string"}},
    "required": ["title", "summary", "ids", "link"]}
# 섹션 = 빈도상위 top 5 + 단독·특이 notable 3 (2026-09-17: 3+2 → 5+3. 2026-10-03 카드도 5+3 — 선캐처 TPU 위성이 산업 top 5위로 잘린 사례)
_SECTION = {"type": "object", "properties": {
    "top": {"type": "array", "items": _ISSUE},
    "notable": {"type": "array", "items": _ISSUE}},
    "required": ["top", "notable"]}
SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"macro": _SECTION, "industry": _SECTION, "stock": _SECTION},
        "required": ["macro", "industry", "stock"],
    },
    ensure_ascii=False,
)

PROMPT = (
    "다음은 한국 증권가 텔레그램 여러 방에서 최근 24시간 올라온 토픽 목록이다. "
    "각 항목은 id(정수), room(방), category(매크로/산업/종목/기타), topic(한 줄 헤드라인)으로 구성된다.\n"
    "일간 회의 자료용으로, 세 분류(매크로/산업/종목)별로 각각 top 5개 + notable 3개를 뽑아라.\n"
    "- top(5개): 여러 방에서 '많이 언급된' 핵심 이슈. 언급 빈도·여러 방 동시 언급이 높은 순으로 정렬.\n"
    "- notable(3개): 언급은 적지만(때로 특정 방 단독·특종) 해당 분류에 파급이 클 수 있어 '놓치면 안 되는' "
    "이슈. 소수 방에서만 나온 단독·특이 정보를 우선 발굴하되 top 5개와 중복되지 않게.\n"
    "- 아래 '이미 보낸 뉴스' 와 같은 사건(같은 회사·같은 수치·같은 결정의 후속·재탕)은 top/notable 에 넣지 말고 "
    "그 다음 순위의 새 이슈를 뽑는다. 새 사실(다른 회사·다른 숫자·새 결정)만 후보다.\n"
    "각 이슈 필드:\n"
    "- title: 한 줄 제목(핵심을 압축).\n"
    "- summary: 가장 핵심적인 '수치·키워드'만 ' · '(가운뎃점)로 구분해 3~6개 나열. 문장으로 쓰지 말 것. "
    "예) 'WTI +6% · 美10Y 4.5% · 이란 제재유예 철회 · 증시 약세'.\n"
    "- ids: 이 이슈에 해당하는 입력 항목의 id 를 빠짐없이 전부 나열(정수 배열). 건수는 이 배열로 코드가 센다.\n"
    "- link: 이 이슈를 대표하는 실제 기사 URL 1개를 WebSearch/WebFetch 로 찾는다. 언론사·통신사·기업 공식 보도자료의 기사 URL — 사이트 우선순위 없음. 블로그·카페·커뮤니티·SNS·유튜브·단축링크와 로그인·유료 기사(한경 프리미엄 등) 금지. "
    "존재를 확인한 URL 만 적을 것. 못 찾으면 빈 문자열 \"\".\n"
    "category가 '기타'인 항목은 무시. 유효 이슈가 부족하면 가능한 만큼만. 한국어로. 입력:\n"
)


def collect_recent(end, hours=24):
    """[end-hours, end) KST 범위의 토픽을 모든 방에서 모은다 → [{room, topic, category}].

    category 는 processed CSV 의 분류 컬럼(매크로/산업/종목/기타). 구 포맷(컬럼 없음)이면 '기타'.
    """
    start = end - timedelta(hours=hours)
    items = []
    for ch in channels.load_channels():  # 매 실행 재로딩 — 신규 방 토픽도 리포트에 포함(재기동 불필요)
        for r in _read_csv(config.PROCESSED_DIR / f"{ch['alias']}_processed.csv"):
            topic = (r.get("topic") or "").strip()
            if not topic:
                continue
            try:
                dt = datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except Exception:
                continue
            if start <= dt < end:
                cat = (r.get("category") or "").strip() or "기타"
                items.append({"id": len(items), "room": ch["alias"], "topic": topic, "category": cat})
    return items


# 한 번의 claude 호출을 /oh-my-claudecode:team 으로 강제한다(사용자 요청, 2026-09-08) —
# 리드가 분류별 워커에게 집계·링크 탐색을 나눠 주고 최종 JSON 만 출력한다.
# 호스트 상주에서는 run.bat 이 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 을 넣는다 —
# 이 변수가 없으면 /team 이 안 먹으므로 run.bat 을 고칠 때 같이 본다 (매 정시 집계가 이걸 쓴다).
TEAM_PREFIX = (
    "/oh-my-claudecode:team 3:executor 아래 지시를 팀으로 수행하라 — 워커 3명이 매크로/산업/종목을 하나씩 맡아 "
    "이슈 선정·ids 집계·허용 도메인 안 대표 기사 링크 탐색을 하고, 리드는 취합해 최종 결과만 JSON 으로 출력한다. "
    "'이미 보낸 뉴스' 목록은 각 워커에게 그대로 전달해 제외 기준으로 쓰게 한다.\n\n"
)
_SKIP_HOSTS = {"share.google", "naver.me"}  # 단축 링크 — 검색 대상이 못 된다
# 허용 목록 밖 링크를 받을 때 거르는 비(非)기사 사이트 — 이 도메인 또는 그 하위 호스트 (2026-10-04)
# 로그인 필요한 유료 기사 — 네이버 프리미엄콘텐츠(2026-10-09 리스트에서도 삭제) · 한경 프리미엄9 는 www.hankyung.com/article/<숫자>i (2026-10-04 실측: 프리미엄9 목록 48건 전부 i 접미).
# ponytail: i 접미 무료 지면 기사도 같이 빠진다 — 아깝다면 페이지의 '로그인월' 표식으로 좁힌다
_PAYWALLED = re.compile(r"^https?://((www\.)?hankyung\.com/article/\d+i\b|contents\.premium\.naver\.com/)")
_NOT_NEWS = {"youtube.com", "youtu.be", "x.com", "twitter.com", "facebook.com", "instagram.com", "t.me",
             "reddit.com", "threads.net", "linkedin.com", "tistory.com", "brunch.co.kr", "bit.ly"}


def _norm_host(url):
    """URL → 호스트(소문자). 원사이트 그대로 비교한다 — n.news.naver.com 과 news.naver.com 은 다른 사이트."""
    return (re.sub(r"^https?://([^/?#]+).*$", r"\1", url.strip()) if url else "").lower()


def allowed_domains():
    """input/뉴스링크정리.xlsx '사이트' 열(https://n.news.naver.com 같은 원사이트)의 호스트 집합 — link 는 이 안에서만.
    시트는 번호·사이트·사이트명(매체)·비고 4열(2026-09-08). http 로 시작하는 셀이 곧 사이트다."""
    path = config.INPUT_DIR / "뉴스링크정리.xlsx"
    if not path.exists():
        return set()
    import zipfile
    urls = []
    with zipfile.ZipFile(path) as z:  # 문자열은 sharedStrings 또는(openpyxl 저장본) 시트 안 inline <t> 에 있다
        for name in z.namelist():
            if name.startswith("xl/") and name.endswith(".xml"):
                xml = z.read(name).decode("utf-8", "replace")
                urls += [t for t in re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S) if t.startswith("http")]
    return {h for h in (_norm_host(u) for u in urls) if h and h not in _SKIP_HOSTS}


def _url_alive(url):
    """기사 URL 이 실제로 열리는지(HTTP 200~399). 모델이 지어낸 URL 을 거른다."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status < 400
    except Exception:  # noqa: BLE001 — 404·타임아웃·봇 차단(403) 모두 '못 씀'
        return False  # ponytail: 봇 차단하는 유료지(WSJ 등) 새 사이트는 못 들어온다 — 필요하면 엑셀에 손으로 추가


def _site(host):
    """같은 매체의 PC·모바일·www 유무 주소를 하나로 본다 (2026-10-09). 다른 서브도메인(n.news.naver.com 등)은 그대로 구분."""
    return re.sub(r"^(www|m|mobile)\.", "", host)


def accept_link(link, domains, added):
    """link 를 쓸지 판단 → 쓸 URL 또는 "". 유료(로그인) 기사는 버리고, 뉴스 리스트에 있는 사이트면 그대로.
    없으면(2026-10-04 owner) 비기사 사이트가 아니고 실제로 열리는 URL 만 받고, 호스트를 added 에 모은다 → add_domains."""
    link = (link or "").strip()
    host = _norm_host(link)
    if not link.startswith("http") or not host or _PAYWALLED.match(link):
        return ""
    site = _site(host)
    if any(_site(h) == site for h in (*domains, *added)):
        return link
    if (host in _SKIP_HOSTS or any(host == d or host.endswith("." + d) for d in _NOT_NEWS)
            or any(p in ("blog", "cafe", "post") for p in host.split(".")[:-2]) or not _url_alive(link)):
        return ""
    added[host] = link
    return link


def add_domains(added, domains, path=None):
    """새로 받은 사이트를 뉴스링크정리.xlsx 끝에 행으로 붙이고 domains 에도 넣는다 — 다음 회차부터 허용 목록이다.
    번호·사이트·사이트명(호스트로 채움, 사람이 고친다)·비고(추가일·근거 URL). 엑셀이 열려 있어 실패해도 이번 링크는 쓴다."""
    if not added:
        return
    domains |= set(added)
    path = path or config.INPUT_DIR / "뉴스링크정리.xlsx"
    import html
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            files = {n: z.read(n) for n in z.namelist()}
        name = "xl/worksheets/sheet1.xml"
        sheet = files[name].decode("utf-8")
        last = max(int(r) for r in re.findall(r'<row r="(\d+)"', sheet))
        cell = lambda c, r, v: f'<c r="{c}{r}" t="inlineStr"><is><t>{html.escape(v)}</t></is></c>'
        rows = ""
        for k, (host, url) in enumerate(sorted(added.items()), 1):
            r = last + k
            rows += (f'<row r="{r}"><c r="A{r}" t="n"><v>{r - 1}</v></c>' + cell("B", r, "https://" + host)
                     + cell("C", r, host) + cell("D", r, f"{datetime.now(KST):%Y-%m-%d} 자동 추가 — 집계 링크 {url}")
                     + "</row>")
        sheet = sheet.replace("</sheetData>", rows + "</sheetData>")
        sheet = re.sub(r'<dimension ref="A1:D\d+"', f'<dimension ref="A1:D{last + len(added)}"', sheet)
        files[name] = sheet.encode("utf-8")
        tmp = path.with_name(path.name + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in files.items():
                z.writestr(n, b)
        os.replace(tmp, path)
        print(f"[links] 뉴스 리스트에 새 사이트 {len(added)}개 추가: {', '.join(sorted(added))}")
    except Exception as e:  # noqa: BLE001
        print(f"[links] ⚠ 뉴스 리스트 갱신 실패(이번 링크는 사용): {type(e).__name__}: {e}")


def _finalize(items, analysis, domains):
    """모델 출력 후처리 — mentions 는 ids 를 코드가 정확히 센 값, link 는 accept_link 로 거른다(새 사이트는 목록에 추가)."""
    n = len(items)
    added = {}
    for sec in analysis.values():
        for lst in (sec.get("top") or [], sec.get("notable") or []):
            for iss in lst:
                ids = sorted({i for i in (iss.get("ids") or []) if isinstance(i, int) and 0 <= i < n})
                iss["ids"] = ids
                iss["mentions"] = len(ids)
                iss["link"] = accept_link(iss.get("link"), domains, added)
    add_domains(added, domains)
    return analysis


def _sent_block(sent):
    """최근 3일 발송 이력 → 프롬프트 조각. 비었으면 "". 제목·요약만(약 5천 자, 입력 17만 자의 3%)."""
    rows = [{"title": h.get("title"), "summary": h.get("summary")} for h in (sent or []) if h.get("title")]
    if not rows:
        return ""
    return "이미 보낸 뉴스(최근 3일, 같은 사건은 제외): " + json.dumps(rows, ensure_ascii=False) + "\n"


def analyze(items, model=None, team=None, sent=None):
    """claude 1콜로 핫종목/테마/이슈 분석 + 대표 기사 링크 → (분석 dict, cost).

    model/team 을 안 주면 Opus + /team — 2026-09-08 이후 집계 품질의 기준선이다.
    싸게 돌릴 때만 호출부가 model="sonnet", team=False 를 넘긴다.
    sent: news_dedup.load_sent() 형식의 발송 이력 — 모델이 처음부터 새 사건을 고르게 유도한다(2026-09-17).
    확률적 유도일 뿐이고, 실제 중복 차단은 뒤의 news_dedup.find_new 가 한다."""
    if team is None:
        team = True
    domains = allowed_domains()
    prompt = ((TEAM_PREFIX if team else "") + PROMPT
              + _sent_block(sent)
              + "입력 토픽: " + json.dumps(items, ensure_ascii=False))
    proc = subprocess.run(
        [config.CLAUDE_EXE, "-p", "--model", model or MODEL, "--output-format", "json", "--json-schema", SCHEMA,
         "--dangerously-skip-permissions"],  # 2026-09-10: config.CLAUDE_EXE
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 실패(rc={proc.returncode}): {(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    cost = data.get("total_cost_usd", 0.0)
    out = _finalize(items, data.get("structured_output") or json.loads(data["result"]), domains)
    return out, cost + ensure_links(_issues(out), domains, "dashboard")


def _issues(analysis):
    """집계 dict → 전 섹션 top+notable 이슈 평탄화(같은 dict 참조 — 고치면 analysis 도 바뀐다)."""
    return [i for sec in analysis.values() for k in ("top", "notable") for i in (sec.get(k) or [])]


LINK_MODEL = "claude-sonnet-5"
LINK_SCHEMA = json.dumps({"type": "object", "properties": {"links": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "link": {"type": "string"}},
    "required": ["i", "link"]}}}, "required": ["links"]})


def _search_links(issues, domains):
    """sonnet 1콜: 이슈 제목마다 기사 URL 을 WebSearch 로 찾는다 → ({index: url}, cost). 거르기는 accept_link."""
    rows = [{"i": k, "title": it.get("title"), "summary": it.get("summary")} for k, it in enumerate(issues)]
    prompt = ("아래 뉴스 이슈마다 대표 기사 URL 1개를 찾아라. 먼저 ToolSearch 로 WebSearch 를 불러온 뒤 이슈마다 "
              "검색한다. 언론사·통신사·기업 공식 보도자료의 기사 URL — 사이트 우선순위 없음. 블로그·카페·커뮤니티·SNS·유튜브·단축링크와 로그인·유료 기사(한경 프리미엄 등) 금지. 못 찾으면 link 는 \"\".\n"
              "이슈: " + json.dumps(rows, ensure_ascii=False))
    proc = subprocess.run(
        [config.CLAUDE_EXE, "-p", "--model", LINK_MODEL, "--output-format", "json", "--json-schema", LINK_SCHEMA,
         "--dangerously-skip-permissions", "--setting-sources", "", "--strict-mcp-config"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude 실패(rc={proc.returncode}): {(proc.stderr or '')[:300]}")
    data = json.loads(proc.stdout)
    out = data.get("structured_output") or json.loads(data["result"])
    return {r["i"]: r["link"] for r in out.get("links") or [] if isinstance(r.get("i"), int)}, data.get("total_cost_usd", 0.0)


RSS_MIN_SCORE = 0.35  # 2026-11-12 실측: 284건 중 0.35 이상 80건 전부 같은 사건, 0.30~0.35 에 오매칭(에코프로비엠↔에코프로)
_UA = {"User-Agent": "Mozilla/5.0"}


def _bigrams(s):
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _title_score(a, b):
    """제목 유사도 — 글자 bigram 겹침 / 짧은 쪽 크기. 조사·어순이 달라도 같은 사건이면 높다."""
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / max(1, min(len(ga), len(gb)))


def _gnews_decode(link):
    """news.google.com/rss/articles/<id> → 원문 URL. 공식 API 가 아닌 batchexecute 라 깨지면 예외 → 호출부가 건너뛴다."""
    aid = link.split("/articles/")[1].split("?")[0]
    with urllib.request.urlopen(urllib.request.Request(f"https://news.google.com/rss/articles/{aid}", headers=_UA), timeout=10) as r:
        page = r.read().decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
    req = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                     f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts},"{sg}"]']
    body = ("f.req=" + urllib.parse.quote(json.dumps([[req]]))).encode()
    with urllib.request.urlopen(urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
            headers={**_UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}), timeout=10) as r:
        resp = r.read().decode("utf-8")
    return json.loads(json.loads(resp.split("\n\n")[1])[0][2])[1]


def _rss_links(issues, domains, now=None):
    """구글 뉴스 RSS 로 이슈 제목 검색 → {index: 원문 URL} (2026-11-12 owner). 비용 0, 이슈당 1~3 요청.
    3일 내 · 뉴스 리스트 사이트 · 블로그 아님 · 제목 유사도 RSS_MIN_SCORE 이상 중 최고점 1개. 실패한 이슈는 그냥 비운다."""
    import time
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    now = now or datetime.now(KST)
    out = {}
    for k, it in enumerate(issues):
        title = it.get("title") or ""
        q = " ".join(re.sub(r"[\"'“”‘’…·|\[\]()]", " ", title).split()[:8])  # 앞 8어절
        if not q:
            continue
        try:
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=10) as r:
                root = ET.fromstring(r.read())
            best = (RSS_MIN_SCORE, None)
            for item in root.iter("item"):
                src = item.find("source")
                host = _norm_host(src.get("url")) if src is not None else ""
                if (not host or "blog" in host.split(".") or not any(_site(h) == _site(host) for h in domains)
                        or not timedelta(0) <= now - parsedate_to_datetime(item.findtext("pubDate")) <= timedelta(days=3)):
                    continue
                s = _title_score(title, re.sub(r" - [^-]+$", "", item.findtext("title") or ""))  # 끝의 ' - 매체명' 제거
                if s >= best[0]:
                    best = (s, item.findtext("link"))
            if best[1]:
                out[k] = _gnews_decode(best[1])
        except Exception as e:  # noqa: BLE001 — RSS 는 덤이다. 실패하면 sonnet 보충이 맡는다
            print(f"[rss] ⚠ {title[:30]}: {type(e).__name__}: {e}")
        time.sleep(1)  # ponytail: 직렬 1초 간격(24건 ≈ 30초) — 구글 레이트리밋 회피, 느리면 간격만 줄인다
    return out


def ensure_links(issues, domains, tag, search=_search_links, rss=None):
    """link 가 빈 이슈만 모아 링크 전용 sonnet 1콜로 채운다(issues 를 직접 고침) → 추가 cost.

    2026-10-01: CLI 2.1.285 부터 --json-schema 실행이 StructuredOutput 을 서둘러 내게 해 Opus 리드가 /team·검색을
    건너뛰고 link 를 전부 "" 로 냈다(09-30 13:08·10-01 06:59 도구 호출 0). us_open 은 sonnet 별칭 5.5 전환이 원인.
    집계를 다시 돌리지 않고(같은 압박에 또 걸리고 $3~6) 링크 전용 sonnet 1콜($0.2~0.7)로 메운다.
    2026-10-04 owner: 0건일 때만이 아니라 빈 링크가 하나라도 있으면 보충한다(10-04 12:55 카드 24건 중 링크 5건).
    링크는 사이트 우선순위 없이 accept_link 로 거르고, 리스트에 없던 사이트는 뉴스 리스트에 추가한다.
    실패하거나 못 찾으면 경고만 — 링크 없는 카드가 카드 없음보다 낫다."""
    missing = [i for i in issues if not i.get("link")]
    if not missing:
        return 0.0
    # 2026-11-12 owner: sonnet 보충 전에 구글 뉴스 RSS 로 먼저 채운다(무료) — 1달 실측 못 찾은 284건 중 80건 회수
    n, added = 0, {}
    for k, url in (rss or _rss_links)(missing, domains).items():
        if 0 <= k < len(missing) and (url := accept_link(url, domains, added)):
            missing[k]["link"] = url
            n += 1
    print(f"[{tag}] RSS → {n}/{len(missing)}건")
    missing = [i for i in missing if not i.get("link")]
    if not missing:
        return 0.0
    print(f"[{tag}] 링크 없는 후보 {len(missing)}/{len(issues)}건 — 링크 보충 1콜")
    try:
        found, cost = search(missing, domains)
    except Exception as e:  # noqa: BLE001 — 보충 실패가 집계 결과를 버리게 하지 않는다
        print(f"[{tag}] ⚠ 링크 보충 실패 — 링크 없이 진행: {type(e).__name__}: {e}")
        return 0.0
    n, added = 0, {}
    for k, url in found.items():
        if 0 <= k < len(missing) and (url := accept_link(url, domains, added)):
            missing[k]["link"] = url
            n += 1
    add_domains(added, domains)
    print(f"[{tag}] 링크 보충 → {n}/{len(missing)}건 · 최종 {sum(bool(i.get('link')) for i in issues)}/{len(issues)}건 (+${cost:.2f})")
    return cost


def export_analysis(end=None, hours=24, model=None, team=None, sent=None):
    """최근 hours 시간 토픽 집계 → payload 반환 + JSON 저장. 매 정시 작업의 본체다.

    Opus 가 토픽을 의미 단위로 묶고 mentions 를 매긴다. 대시보드가 자체 문장 유사도로
    묶던 걸 이걸로 대체한다 — 그쪽은 같은 주제라도 문장이 다르면 안 묶여 한 주제가
    100장 넘게 흩어졌고(2026-08-12 실측 AI메모리 174글→138클러스터), 반대로 방 고정
    양식이 같으면 남남을 묶었다(실적속보 20개 회사가 한 장).

    모델·팀은 기본 AGG_MODEL/AGG_TEAM(= Opus + /team). 환경변수로만 내린다.

    출력: output/dashboard_analysis.json (임시파일 후 os.replace 로 원자적 교체 —
    대시보드가 쓰는 도중 읽어도 반쪽 JSON 을 보지 않는다).
    """
    if end is None:
        end = datetime.now(KST)
    model = model or AGG_MODEL
    if team is None:
        team = AGG_TEAM
    items = collect_recent(end, hours)
    if not items:
        print(f"[dashboard] 최근 {hours}h 토픽 0건 — 집계 생략")
        return None
    print(f"[dashboard] 최근 {hours}h 토픽 {len(items)}건 — {model}{'+team' if team else ''} 집계 중...")
    analysis, cost = analyze(items, model=model, team=team, sent=sent)
    payload = _analysis_payload(items, analysis, end, hours, cost)
    out = _write_analysis(payload)
    print(f"[dashboard] 집계 저장 (${cost:.3f}) -> {out}")
    return payload


def _analysis_payload(items, analysis, end, hours, cost):
    """분석 결과 → 대시보드 계약 payload."""
    counts = {}
    for i in items:
        counts[i["room"]] = counts.get(i["room"], 0) + 1
    return {
        "generatedAt": datetime.now(KST).isoformat(),
        "windowStart": (end - timedelta(hours=hours)).isoformat(),
        "windowEnd": end.isoformat(),
        "windowHours": hours,
        "topics": len(items),
        "rooms": len(counts),
        "roomCounts": sorted(counts.items(), key=lambda x: -x[1]),
        "costUsd": round(cost, 4),
        "sections": {k: analysis.get(k) or {"top": [], "notable": []}
                     for k in ("macro", "industry", "stock")},
    }


def _write_analysis(payload):
    """집계 JSON 원자적 저장(임시파일 후 os.replace) — 대시보드가 반쪽 JSON 을 읽지 않게."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.OUTPUT_DIR / "dashboard_analysis.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    return out


def main():
    """수동 집계 1회. claude 1콜(기본 Opus+/team, 콜당 $5~9)이 실제로 나간다."""
    ap = argparse.ArgumentParser(prog="src.daily_report")
    ap.add_argument("--date", help="기준 날짜 YYYY-MM-DD (그 날 07:00 기준 최근24h). 생략 시 현재시각")
    ap.add_argument("--hours", type=int, default=24, help="집계 창(시간), 기본 24")
    args = ap.parse_args()
    end = None
    if args.date:
        end = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=7, tzinfo=KST)
    export_analysis(end, args.hours)


if __name__ == "__main__":
    main()
