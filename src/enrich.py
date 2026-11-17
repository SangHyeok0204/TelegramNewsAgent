"""링크 본문·PDF 첨부 텍스트 추출 (2026-09-10).

수집 시 메시지의 첫 http(s) URL 을 따라가 기사 본문을, PDF 첨부는 텍스트를 뽑아
DB(url_text, attach_text)에 넣는다. 청킹(processed) 입력에 제목이 아니라 **실제 내용**이
들어가게 하는 것이 목적이다 (그전엔 첨부만 있는 글 하루 152건이 빈 텍스트로 들어갔다).

2026-09-16 (7일 실측 5,778건 중 본문 없음 1,406 · 제목만 648 의 원인별 수정):
- 링크 대상이 PDF 인 글(buly.kr → 하나증권 리서치, bbs2.shinhansec.com 신한 리서치)은 비HTML 이라 버려졌다
  → PDF 면 메모리에서 바로 텍스트를 뽑아 url_text 로.
- naver.me 는 link.naver.com/bridge?url=<기사> 브리지로 끝나 본문이 없었다 → 안의 URL 을 한 번 더 따라간다.
- 조선비즈는 본문이 <p> 가 아니라 페이지 안 JSON(Fusion.globalContent.content_elements) 에,
  이데일리는 itemprop="articleBody" div 에 <br> 로 들어 있다 → 본문 셀렉터 둘 추가.
- wallstreetcn.com 은 SPA 라 HTML 에 본문이 없다 → 공개 JSON API 로.
- PDF 는 pdf-inspector(제목·불릿 구조 보존) 를 먼저 쓰고 pypdf 로 폴백한다 (extract_pdf_text 참고).

원칙: 실패는 전부 빈 문자열로 삼킨다. 수집이 링크 하나 때문에 멈추면 안 된다.
표준 라이브러리 + pypdf + pdf-inspector 만 쓴다 (requests·bs4·playwright 를 늘리지 않는다).
"""
import io
import json
import logging
import re
import urllib.parse
import urllib.request
from html import unescape

URL_MAX_CHARS = 1500      # 기사 본문 저장 상한
URL_TIMEOUT = 6           # 한 URL 당 응답 대기(초)
URL_MAX_BYTES = 300_000   # 내려받는 HTML 상한
PDF_MAX_BYTES = 15 * 1024 * 1024   # 이보다 큰 PDF 는 받지 않는다 (첨부·링크 공통)
PDF_MAX_PAGES = 8
PDF_MAX_CHARS = 4000
# pdf-inspector 채택 기준 (2026-09-16, 수집 PDF 19개 실측): 두 실패 유형을 잡는다
PDF_MIN_ALPHA_RATIO = 0.9   # 글자 수가 pypdf 의 90% 미만 → 글자 누락·인코딩 깨짐
PDF_TABLE_LINE_RATIO = 0.5  # 앞 PDF_HEAD_CHARS 에서 표(|) 줄이 절반 이상 → 본문이 표로 오인돼 셀 조각
PDF_HEAD_CHARS = 800        # processed.EXTRA_CHARS 와 같은 크기 — claude 가 실제로 보는 앞부분

# 깨진 PDF 마다 "invalid pdf header" 를 상주 로그에 찍지 않는다 (반환값 "" 로 충분)
logging.getLogger("pypdf").setLevel(logging.CRITICAL)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 기사 본문이 없는 호스트 — 받아 봐야 로그인/스크립트 껍데기라 시간만 쓴다
SKIP_HOSTS = ("t.me", "telegram.me", "telegram.org", "youtube.com", "youtu.be",
              "twitter.com", "x.com", "instagram.com", "facebook.com")
# 브리지 페이지 — 진짜 기사 URL 이 url= 쿼리에 있다 (naver.me 단축 URL 이 여기로 리다이렉트된다)
BRIDGE_HOSTS = ("link.naver.com",)
_WSCN = re.compile(r"wallstreetcn\.com/articles/(\d+)")
WSCN_API = "https://api-one.wallstcn.com/apiv1/content/articles/{id}?extract=0"

_DROP = re.compile(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_META_CHARSET = re.compile(rb'charset=["\']?\s*([\w\-]+)', re.I)
_FUSION_CONTENT = re.compile(r'"content":"((?:[^"\\]|\\.)*)"')  # Arc(Fusion) CMS 의 본문 조각들


def _plain(fragment):
    """HTML 조각 → 한 줄 텍스트."""
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", fragment))).strip()


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _skip_host(url):
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def _bridge_target(url):
    """link.naver.com/bridge?url=<기사> 같은 브리지면 안의 URL, 아니면 ""."""
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() in BRIDGE_HOSTS:
        inner = urllib.parse.parse_qs(parts.query).get("url")
        if inner and inner[0].startswith("http"):
            return inner[0]
    return ""


def _open(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"})
    return urllib.request.urlopen(req, timeout=timeout)


def _wallstreetcn(article_id, max_chars, timeout):
    """wallstreetcn.com 은 SPA — 공개 JSON API 가 제목·본문(HTML) 을 준다."""
    try:
        with _open(WSCN_API.format(id=article_id), timeout) as resp:
            data = json.loads(resp.read(URL_MAX_BYTES)).get("data") or {}
        return _plain(data.get("title") or "")[:200], _plain(data.get("content") or "")[:max_chars]
    except Exception:
        return "", ""


def _html_body(html):
    """본문 후보를 순서대로 보고 200자 이상인 첫 것, 없으면 가장 긴 것.
    <article> → itemprop=articleBody(schema.org, 이데일리 등) → Fusion JSON(조선비즈) → <p> 모음."""
    cands = []
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
    if m:
        cands.append(_plain(m.group(1)))
    m = re.search(r'(?is)itemprop=["\']articleBody["\'][^>]*>(.{0,40000})', html)
    if m:  # 닫는 </div> 를 찾지 않는다 — 중첩 div 에서 첫 </div> 로 자르면 본문이 잘리고, 본문은 어차피 앞에 온다
        cands.append(_plain(m.group(1)))
    i = html.find("Fusion.globalContent")
    if i >= 0:
        parts = []
        for raw in _FUSION_CONTENT.findall(html[i:i + 200_000]):
            try:
                t = _plain(json.loads('"' + raw + '"'))
            except ValueError:
                continue
            if len(t) > 20:
                parts.append(t)
        cands.append(" ".join(parts))
    ps = [t for t in (_plain(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", html)) if len(t) > 20]
    cands.append(" ".join(ps))
    for c in cands:
        if len(c) >= 200:
            return c
    return max(cands, key=len) if cands else ""


def fetch_url_text(url, max_chars=URL_MAX_CHARS, timeout=URL_TIMEOUT, _hop=0):
    """기사 URL → (제목, 본문). 실패·SKIP_HOSTS 는 ("", "").

    링크 대상이 PDF 면 (제목 "", PDF 텍스트). 브리지 페이지는 안의 URL 을 한 번(_hop) 더 따라간다.
    (readability 급 파서는 안 쓴다 — 토픽 한 문장을 뽑는 데는 이 정도면 된다.)
    """
    if not url or _skip_host(url):
        return "", ""
    m = _WSCN.search(url)
    if m:
        return _wallstreetcn(m.group(1), max_chars, timeout)
    try:
        with _open(url, timeout) as resp:
            inner = _bridge_target(resp.geturl())  # naver.me → 302 → link.naver.com/bridge?url=...
            if inner and _hop == 0:
                return fetch_url_text(inner, max_chars, timeout, _hop=1)
            ctype = resp.headers.get_content_type()
            if ctype == "application/pdf":
                if int(resp.headers.get("Content-Length") or 0) > PDF_MAX_BYTES:
                    return "", ""
                return "", extract_pdf_text(resp.read(PDF_MAX_BYTES))[:max_chars]
            if ctype != "text/html":
                return "", ""   # 이미지·JSON 은 여기서 안 다룬다
            raw = resp.read(URL_MAX_BYTES)
            enc = resp.headers.get_content_charset()
        if not enc:  # 국내 언론사는 아직 euc-kr 이 섞여 있다
            m = _META_CHARSET.search(raw[:4096])
            enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
        html = raw.decode(enc, "replace")
    except Exception:
        return "", ""

    html = _DROP.sub(" ", html)
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = _plain(m.group(1))[:200] if m else ""
    return title, _html_body(html)[:max_chars]


def _pypdf_text(src, max_pages, max_chars):
    """pypdf 평문. src 는 경로 또는 bytes."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(src) if isinstance(src, bytes) else str(src))
        if reader.is_encrypted:
            return ""
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) >= max_chars:
                break
        return re.sub(r"\s+", " ", " ".join(parts)).strip()
    except Exception:
        return ""


def _inspector_md(src, max_pages):
    """pdf-inspector markdown (제목·불릿·표 구조). 실패는 ""."""
    try:
        import pdf_inspector
        data = src if isinstance(src, bytes) else open(src, "rb").read()
        pages = list(range(1, min(max_pages, pdf_inspector.detect_pdf_bytes(data).page_count) + 1))
        md = pdf_inspector.process_pdf_bytes(data, pages=pages).markdown or ""
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", md)).strip()
    except Exception:
        return ""


def _alpha(s):
    return sum(c.isalpha() for c in s)


def _inspector_ok(md, plain):
    """pdf-inspector 출력을 믿을지: 글자 수가 pypdf 에 못 미치거나 앞부분이 표 조각이면 False.
    (자체 confidence·has_encoding_issues 는 실측에서 실패 파일에 1.00·False 를 줘 기준으로 못 쓴다.)"""
    if not md or _alpha(md) < PDF_MIN_ALPHA_RATIO * _alpha(plain):
        return False
    lines = [l for l in md[:PDF_HEAD_CHARS].splitlines() if l.strip()]
    return not lines or sum(l.lstrip().startswith("|") for l in lines) / len(lines) < PDF_TABLE_LINE_RATIO


def extract_pdf_text(src, max_pages=PDF_MAX_PAGES, max_chars=PDF_MAX_CHARS):
    """PDF(경로 또는 bytes) → 앞 max_pages 쪽 텍스트. 암호화·이미지 전용·비 PDF 는 "".

    2026-09-16: pdf-inspector markdown 을 먼저 쓰고 _inspector_ok 가 아니면 pypdf 평문.
    둘 다 1초 미만이라 항상 둘 다 돌리고 고른다 (실패 유형을 사전에 판별할 신뢰도 값이 없다).
    """
    plain = _pypdf_text(src, max_pages, max_chars)
    md = _inspector_md(src, max_pages)
    return (md if _inspector_ok(md, plain) else plain)[:max_chars]


class _FakeMsg:
    """_demo 용 가짜 텔레그램 메시지."""

    def __init__(self, message="", photo=None, document=None, web_preview=None):
        self.id, self.message = 1, message
        self.photo, self.document, self.web_preview = photo, document, web_preview


class _FakeDoc:
    def __init__(self, mime, size=1000):
        self.mime_type, self.size = mime, size


def _demo():
    """자체 점검 — 네트워크·텔레그램 없이 도는 것만 (assert)."""
    assert _plain("<b>가</b>\n <i>나</i>") == "가 나"
    assert fetch_url_text("https://t.me/foo/1") == ("", "")
    assert fetch_url_text("") == ("", "")
    assert extract_pdf_text(__file__) == ""          # 비 PDF
    assert extract_pdf_text("없는파일.pdf") == ""     # 없는 파일
    assert extract_pdf_text(b"not a pdf") == ""      # 비 PDF bytes
    # 2026-09-16 브리지·본문 셀렉터·pdf-inspector 채택 기준
    assert _bridge_target("https://link.naver.com/bridge?url=https%3A%2F%2Fn.news.naver.com%2Fa%2F1&dst=x") \
        == "https://n.news.naver.com/a/1"
    assert _bridge_target("https://n.news.naver.com/a/1") == ""
    assert _WSCN.search("https://wallstreetcn.com/articles/3781558#from=ios").group(1) == "3781558"
    body = "본문 " * 60
    assert _html_body(f'<div itemprop="articleBody" class="x"><div>{body}</div></div>').startswith("본문 본문")
    fusion = 'Fusion.globalContent={"content_elements":[{"content":"' + "가나다 " * 30 + '","type":"text"}]}'
    assert _html_body(fusion).startswith("가나다 가나다")
    assert _html_body("<p>" + "짧은글 " * 60 + "</p>").startswith("짧은글")
    assert _inspector_ok("# 제목\n- 불릿 " + "가" * 100, "가" * 100)
    assert not _inspector_ok("가" * 50, "가" * 100)                    # 글자 누락
    assert not _inspector_ok("|a|b|\n|---|---|\n|c|d|\n머리말", "x")  # 앞부분이 표 조각
    assert not _inspector_ok("", "가" * 10)

    from . import collect  # 여기서만 import (collect 가 이 모듈을 쓴다)
    assert collect._first_url("보세요 https://a.com/b?x=1). 끝") == "https://a.com/b?x=1"
    assert collect._first_url("링크 없음") == ""
    # media_type 판정: 첨부 없음 / 사진 / webpage / 비 PDF 문서 / 큰 PDF(다운로드 안 함)
    e = lambda m: collect._enrich(None, -100, m)
    assert e(_FakeMsg("그냥 글")) == (None, None, None, None)
    assert e(_FakeMsg(photo=object()))[1] == "photo"
    assert e(_FakeMsg(web_preview=object(), photo=object()))[1] == "webpage"
    assert e(_FakeMsg(document=_FakeDoc("image/jpeg"))) == (None, "document/jpeg", None, None)
    big = e(_FakeMsg(document=_FakeDoc("application/pdf", 99 * 1024 * 1024)))
    assert big == (None, "document/pdf", None, None), big   # 15MB 초과 → 다운로드 안 함
    assert e(_FakeMsg("https://t.me/x/1"))[3] is None       # SKIP_HOSTS → url_text 없음
    print("[enrich] self-check ok")


if __name__ == "__main__":
    _demo()
