# -*- coding: utf-8 -*-
"""미국장 마감 브리핑 + 채권·환 동향 카드 — 정형 게시물 3종을 파싱해 LLM 없이 조립 (2026-09-26 owner 설계).

소스(방 · 게시물 · 시각 KST):
  A 한투해투「美 증시 마감 브리핑」06:31 자동     → 주요지수 · 상승/하락 TOP3 · 거래량/거래대금 TOP5 · 마감 시황 문단
  B 투행자료창고「🇺🇸 미국장 장마감 리포트」05:20 자동 → 지수 더블체크 · 업종/시장 폭 · 국채 2Y/10Y · DXY/WTI/금 · 주요 변동 종목 5 · 관전 포인트
  C 키움「채권 Daily 안예하」07:15±15 사람       → 미 국채 2/5/10/30Y · NDF/DXY/엔 · 국내 국채 3/5/10/30Y · WTI/금 · 동향 문단 (B 더블체크)
  D KB 채권(임재균·박문현) — 수집 여부 확인 중. 국내 국채 더블체크용.
카드 2장: ① 마감 브리핑(A+B, A 도착 후) ② 채권·환 동향(C(+D), C 도착 후, B 값과 대조).
뉴스 집계(daily_report)와 독립 — DB 만 읽는다. LLM 호출 0.

    python -m src.us_close --dry-run   # 최신 게시물로 카드 2장 텍스트 미리보기(발송 없음)
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from . import channels, config

KST = timezone(timedelta(hours=9))
ROOM = {"A": "한투해투", "B": "투행자료창고", "C": "키움증권리서치센터", "D": "KB채권"}
HEAD = {"A": "美 증시 마감 브리핑", "B": "🇺🇸 미국장 장마감 리포트", "C": "[키움 채권 안예하]",
        "D": "KB증권 리서치본부 채권크레딧팀"}   # D: 08:01~08:15 한국 영업일 「채권시장 일일동향」 — 국고채 민평3사 기준
NUM = r"([+-]?[\d,]+(?:\.\d+)?)"


def _f(s):
    return float(str(s).replace(",", "").replace("$", "").replace("+", ""))


def latest(kind: str, hours: int = 30):
    """방의 최신 해당 게시물 → (dt_kst, text) 또는 None."""
    cid = {c["alias"]: c["id"] for c in channels.load_channels()}.get(ROOM[kind])
    if cid is None:
        return None
    conn = sqlite3.connect(config.DB_PATH)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    for d, t in conn.execute("select date_utc,text from messages where chat_id=? and date_utc>=? order by date_utc desc", (cid, since)):
        if (t or "").lstrip().startswith(HEAD[kind]):
            return datetime.fromisoformat(d).astimezone(KST), t
    return None


# ── 파서 ──
def _section(text: str, title: str, stop: str) -> str:
    """title 로 시작하는 줄부터 다음 stop 문자로 시작하는 줄 전까지."""
    lines = text.split("\n")
    out, on = [], False
    for l in lines:
        if l.strip().startswith(title):
            on = True
            continue
        if on and l.strip().startswith(stop):
            break
        if on:
            out.append(l)
    return "\n".join(out).strip()


def parse_a(text: str) -> dict:
    """한투해투 — 지수·TOP3·TOP5·시황."""
    def rows(sec):
        out = []
        for m in re.finditer(r"^- (.+?) \(([A-Z.\-]+)\): " + NUM + r" \((?:🔴▲|🔵▼)? ?" + NUM + r"%\)", sec, re.M):
            out.append({"name": m.group(1), "ticker": m.group(2), "px": _f(m.group(3)), "chg": _f(m.group(4))})
        return out
    idx = {}
    for m in re.finditer(r"^- (.+?): " + NUM + r" \((?:🔴▲|🔵▼)? ?" + NUM + r"%\)", _section(text, "■주요지수", "■"), re.M):
        idx[m.group(1).strip()] = {"px": _f(m.group(2)), "chg": _f(m.group(3))}
    return {"summary": _section(text, "■마감 시황", "■").lstrip("- ").strip(), "index": idx,
            "up3": rows(_section(text, "■상승 TOP3", "■")), "down3": rows(_section(text, "■하락 TOP3", "■")),
            "vol5": rows(_section(text, "■거래량 TOP5", "■")), "val5": rows(_section(text, "■거래대금 TOP5", "■"))}


def parse_b(text: str) -> dict:
    """투행 — 지수·VIX·국채·달러/WTI/금·업종·시장폭·변동 종목·관전 포인트."""
    glance = _section(text, "■ 한눈에 보는 마감", "■")
    idx = {}
    for m in re.finditer(r"^• (S&P 500|Nasdaq Composite|Dow|Russell 2000|VIX): " + NUM + r" \(" + NUM + r"%\)", glance, re.M):
        idx[m.group(1)] = {"px": _f(m.group(2)), "chg": _f(m.group(3))}
    ust = dict(re.findall(r"(2년|10년) " + NUM + r"%", glance))
    fx = {k: _f(v) for k, v in re.findall(r"(DXY|WTI|금) \$?" + NUM, glance)}
    fxchg = {k: _f(v) for k, v in re.findall(r"(DXY|WTI|금) \$?[\d,.]+ \(" + NUM + r"%\)", glance)}
    sec = _section(text, "■ 업종과 시장 폭", "■")
    strong = re.search(r"강세: (.+)", sec); weak = re.search(r"약세: (.+)", sec)
    breadth = re.search(r"상승 (\d+) · 보합 (\d+) · 하락 (\d+)", sec)
    movers = []
    for m in re.finditer(r"^• (.+?)\(([A-Z.\-]+)\) \$" + NUM + r" \(" + NUM + r"%\): (.+)$", _section(text, "■ 미국 상장 주요 변동 종목", "■"), re.M):
        movers.append({"name": m.group(1), "ticker": m.group(2), "px": _f(m.group(3)), "chg": _f(m.group(4)), "why": m.group(5).strip()})
    nxt = [l.strip("• ").strip() for l in _section(text, "■ 다음 세션 관전 포인트", "■").split("\n") if l.strip().startswith("•")]
    obs = re.search(r"관측 시각: ([^\n]+)", text)
    return {"index": idx, "ust": {k: _f(v) for k, v in ust.items()}, "fx": fx, "fxchg": fxchg,
            "strong": strong.group(1).strip() if strong else "", "weak": weak.group(1).strip() if weak else "",
            "breadth": tuple(int(x) for x in breadth.groups()) if breadth else None,
            "movers": movers, "next": nxt, "observed": obs.group(1).strip() if obs else "",
            "flow": _section(text, "■ 장중 흐름", "■")}


def parse_c(text: str) -> dict:
    """키움 채권 — 미 국채·스프레드·외환·국내 국채·상품·동향."""
    def kv(sec, pat=r"^- (.+?): " + NUM + r"%? \(" + NUM + r"(bp|%|원)?"):
        out = {}
        for m in re.finditer(pat, sec, re.M):
            out[m.group(1).strip()] = {"v": _f(m.group(2)), "chg": _f(m.group(3)), "unit": m.group(4) or ""}
        return out
    us = kv(_section(text, "◆ 미 채권시장", "◆"))
    kr = kv(_section(text, "◆ 국내 국채시장", "◆"))
    fxsec = _section(text, "◆ 외환시장", "◆")
    fx = kv(fxsec)
    ndf = re.search(r"NDF 환율\(1개월물\): " + NUM + r"원, (.+)", fxsec)
    cm = {}
    for m in re.finditer(r"^- (WTI|브렌트유|금): " + NUM + r" \(" + NUM + r", " + NUM + r"%\)", _section(text, "◆ 상품시장", "*"), re.M):
        cm[m.group(1)] = {"v": _f(m.group(2)), "chg": _f(m.group(3)), "pct": _f(m.group(4))}
    date = re.search(r"채권 Daily 코멘트\(([\d.]+)\)", text)
    return {"us": us, "kr": kr, "fx": fx, "ndf": (_f(ndf.group(1)), ndf.group(2).strip()) if ndf else None, "cmdty": cm,
            "comment": _section(text, "[미 채권시장 동향]", "["), "date": date.group(1) if date else ""}


def parse_kb(text: str) -> dict:
    """KB 채권크레딧팀 「채권시장 일일동향」 — 미 국채·국고채(민평3사)·크레딧 스프레드·외환·WTI. 형식 '항목: 값 (변동)'."""
    def kv(sec):
        out = {}
        for m in re.finditer(r"^(.+?): " + NUM + r"\s*(?:%|bp)?\s*(?:\(\$/bbl\)\s*)?\(" + NUM, sec, re.M):
            out[m.group(1).strip()] = {"v": _f(m.group(2)), "chg": _f(m.group(3))}
        return out
    date = re.search(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일 채권시장 일일동향", text)
    return {"us": kv(_section(text, "■ 미국 채권시장", "■")), "kr": kv(_section(text, "■ 국고채 금리", "■")),
            "credit": kv(_section(text, "■ 국내 크레딧 스프레드", "■")), "fx": kv(_section(text, "■ 외환시장", "■")),
            "cmdty": kv(_section(text, "■ 상품시장", "■")),
            "date": f"{date.group(1)}-{int(date.group(2)):02d}-{int(date.group(3)):02d}" if date else ""}


# ── 더블체크 ──
def _chk(a, b, tol, unit=""):
    """두 값 비교 → (✅|⚠️, 차이 문자열). 어느 한쪽이 없으면 (·, '')."""
    if a is None or b is None:
        return "·", ""
    d = a - b
    return ("✅" if abs(d) <= tol else "⚠️"), f"{d:+.2f}{unit}"


IDX_MAP = {"S&P500": "S&P 500", "Nasdaq": "Nasdaq Composite", "Dow Jones": "Dow", "Russell2000": "Russell 2000"}


# ── 장 개장 여부 (주말·휴장) ──
# NYSE·KRX 휴장일은 MacroAgent 가 investing holiday-calendar 로 연 1회 만든 CSV 를 그대로 읽는다(네트워크 없음).
HOLIDAY_DIR = config.DATA_ROOT.parent.parent / "MacroAgent" / "data" / "매크로모니터" / "schedule"


def _holidays(prefix: str, year: int) -> set:
    import csv
    try:
        with open(HOLIDAY_DIR / f"{prefix}_holidays_{year}.csv", encoding="utf-8-sig", newline="") as f:
            return {r["date"] for r in csv.DictReader(f) if "Early close" not in (r.get("note") or "")}
    except OSError:
        return set()


def us_session_for(now_kst: datetime):
    """오늘 아침 카드가 다룰 미국 세션(ET 날짜) — 직전 ET 거래일이 어제(KST 기준 밤사이 마감)면 그 날짜, 아니면 None.
    KST 06:00 기준 ET 는 전날 16:00~17:00 이므로 '어제 ET 날짜' 가 곧 밤사이 마감한 세션이다."""
    d = (now_kst - timedelta(days=1)).date()
    if d.weekday() >= 5 or d.isoformat() in _holidays("us", d.year):
        return None
    return d


def kr_business_day(d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _holidays("kr", d.year)


def market_status(now_kst: datetime) -> dict:
    """{us_session: date|None, kr_open_today: bool, note: str} — 카드 발송 여부의 근거."""
    us = us_session_for(now_kst)
    kr = kr_business_day(now_kst.date())
    note = []
    if us is None:
        note.append("밤사이 미국장 휴장(주말·NYSE 휴일) → 마감 카드 없음")
    if not kr:
        note.append("오늘 한국 휴장 → 키움·KB 미발행, 채권 카드는 투행 값만")
    return {"us_session": us, "kr_open_today": kr, "note": " · ".join(note)}


# ── Block Kit (Slack) ──
def _hdr(t):  return {"type": "header", "text": {"type": "plain_text", "text": t[:150], "emoji": True}}
def _sec(t):  return {"type": "section", "text": {"type": "mrkdwn", "text": t[:2990]}}
def _ctx(t):  return {"type": "context", "elements": [{"type": "mrkdwn", "text": t[:2000]}]}
def _div():   return {"type": "divider"}
def _tiles(items):  # [mrkdwn] → section.fields (2열 타일, 최대 10)
    return {"type": "section", "fields": [{"type": "mrkdwn", "text": t} for t in items[:10]]}
def _tile(label, value):  return f"*{label}*\n{value}"
def _arrow(chg):  return "🔺" if chg > 0 else "🔻" if chg < 0 else "▪️"
def _pct(v, chg, fmt=",.2f", unit="%", prefix=""):
    return f"{_arrow(chg)} *{prefix}{v:{fmt}}*  `{chg:+.2f}{unit}`"


# 대조 임계치 (2026-09-26 owner): 넘으면 값을 보여주지 않고 '⛔ 오류' 로 대체 + 로그. 같은 세션일 때만 대조한다.
ERR = "⛔ 오류"
THRESH = {"index": 0.001, "ust": 0.05, "kr": 0.01, "dxy": 0.5, "wti": 0.02, "gold": 0.02}   # 비율 또는 절대값(%p·pt)
_errors: list[str] = []      # 이번 빌드에서 숨긴 항목 — 로그용


def _guard(label, v, other, tol, relative=False):
    """v 를 other 와 대조 — 임계치 초과면 ERR 을 돌려주고 기록. other 가 없으면 대조 없이 통과."""
    if other is None or v is None:
        return False
    lim = abs(other) * tol if relative else tol
    if abs(v - other) > lim:
        _errors.append(f"{label}: {v} vs {other} (허용 {lim:.4g})")
        return True
    return False


def summary_lines(text: str, n: int = 3) -> list[str]:
    """한 문단(세미콜론/마침표 나열) → 짧은 줄 n개. '~했음' 어미 정리, 88자에서 자름."""
    out = []
    for clause in re.split(r";\s*", text):
        c = clause.strip().rstrip(".")
        c = re.sub(r"(했음|됐음)$", "", c).strip()
        c = re.sub(r"음$", "다", c)
        c = re.sub(r"^(다만|한편|반면|또한)\s*", "", c)
        if len(c) < 8:
            continue
        out.append(c[:88] + ("…" if len(c) > 88 else ""))
        if len(out) >= n:
            break
    return out


def close_blocks(a: dict, b: dict | None, session) -> list:
    """카드 ①. a=한투(필수) b=투행(있으면 업종·금리·변동 종목 + 지수 대조). 출처 표기 없음."""
    _errors.clear()
    b = b or {"index": {}, "ust": {}, "fx": {}, "fxchg": {}, "strong": "", "weak": "", "breadth": None, "movers": [], "next": []}
    wd = "월화수목금토일"[session.weekday()]
    blocks = [_hdr(f"🇺🇸 미국장 마감 · {session} ({wd})")]
    blocks.append(_sec("\n".join(f"• {l}" for l in summary_lines(a["summary"]))))
    blocks.append(_div())
    tiles = []
    for k, v in a["index"].items():
        bv = b["index"].get(IDX_MAP.get(k, ""))
        bad = _guard(f"지수 {k}", v["px"], bv["px"] if bv else None, THRESH["index"], relative=True)
        tiles.append(_tile(k, ERR if bad else _pct(v["px"], v["chg"])))
    if "VIX" in b["index"]:
        vv = b["index"]["VIX"]; tiles.append(_tile("VIX", _pct(vv["px"], vv["chg"], ".2f")))
    blocks.append(_sec("*📊 지수*")); blocks.append(_tiles(tiles))
    if b["breadth"]:
        up, fl, dn = b["breadth"]
        blocks.append(_sec("*🧭 업종 · 시장 폭*"))
        blocks.append(_tiles([_tile("강세 업종", "🔺 " + b["strong"]), _tile("약세 업종", "🔻 " + b["weak"]),
                              _tile("S&P500 상승 / 하락", f"*{up}* / *{dn}*"), _tile("상승 비중", f"*{up/(up+fl+dn):.0%}*")]))
    if b["ust"] or b["fx"]:
        blocks.append(_div())
        ust2, ust10 = b["ust"].get("2년"), b["ust"].get("10년")
        blocks.append(_sec("*💵 금리 · 환 · 원자재*"))
        blocks.append(_tiles([
            _tile("미 국채 2Y", f"*{ust2}%*" if ust2 else "-"),
            _tile("미 국채 10Y", f"*{ust10}%*  `10Y-2Y {(ust10-ust2)*100:+.0f}bp`" if ust2 and ust10 else "-"),
            _tile("DXY", _pct(b["fx"].get("DXY", 0), b["fxchg"].get("DXY", 0), ".2f")),
            _tile("WTI", _pct(b["fx"].get("WTI", 0), b["fxchg"].get("WTI", 0), ".2f", prefix="$")),
            _tile("금", _pct(b["fx"].get("금", 0), b["fxchg"].get("금", 0), prefix="$"))]))
    if b["movers"]:
        blocks.append(_div())
        blocks.append(_sec("*🏷️ 주요 변동 종목*"))
        blocks.append(_tiles([_tile(f"{m['name']} ({m['ticker']})", f"{_arrow(m['chg'])} `{m['chg']:+.2f}%`") for m in b["movers"][:6]]))
    blocks.append(_sec("*🥇 상승 · 하락 TOP3*\n"
                       "🔺 " + " · ".join(f"*{r['ticker']}* `{r['chg']:+.2f}%`" for r in a["up3"]) + "\n"
                       "🔻 " + " · ".join(f"*{r['ticker']}* `{r['chg']:+.2f}%`" for r in a["down3"])))
    blocks.append(_sec("*📦 거래량 · 거래대금 TOP5*\n"
                       "거래량  " + " · ".join(f"{r['ticker']} `{r['chg']:+.1f}%`" for r in a["vol5"]) + "\n"
                       "거래대금  " + " · ".join(f"{r['ticker']} `{r['chg']:+.1f}%`" for r in a["val5"])))
    if b["next"]:
        blocks.append(_sec("*📅 다음 세션*\n" + "\n".join(f"• {n[:90]}" for n in b["next"][:3])))
    return blocks


def bond_blocks(c: dict | None, b: dict | None, kb: dict | None, session, reduced: bool = False) -> list:
    """카드 ②. c=키움(정상), kb=KB(국고채 대조·크레딧·달러원), b=투행(미 국채·환 대조). reduced=한국 휴장 → 투행 값만."""
    _errors.clear()
    blocks = [_hdr(f"📉 채권 · 환 동향 · {session} 마감")]
    if reduced or not c:
        b = b or {"ust": {}, "fx": {}, "fxchg": {}}
        u2, u10 = b["ust"].get("2년"), b["ust"].get("10년")
        blocks.append(_sec("• 한국 휴장 — 미 국채·환·원자재만"))
        blocks.append(_tiles([_tile("미 국채 2Y", f"*{u2}%*" if u2 else "-"),
                              _tile("미 국채 10Y", f"*{u10}%*  `10Y-2Y {(u10-u2)*100:+.0f}bp`" if u2 and u10 else "-"),
                              _tile("DXY", _pct(b["fx"].get("DXY", 0), b["fxchg"].get("DXY", 0), ".2f")),
                              _tile("WTI", _pct(b["fx"].get("WTI", 0), b["fxchg"].get("WTI", 0), ".2f", prefix="$")),
                              _tile("금", _pct(b["fx"].get("금", 0), b["fxchg"].get("금", 0), prefix="$"))]))
        return blocks
    b = b or {"ust": {}, "fx": {}}
    blocks.append(_sec("\n".join(f"• {l}" for l in summary_lines(c["comment"].replace(". ", "; "), 3))))
    blocks.append(_div())
    sp = " · ".join(f"{k} `{v['v']:+.1f}bp`" for k, v in c["us"].items() if k in ("10Y-2Y", "기준금리-2Y", "30Y-10Y"))
    blocks.append(_sec("*🇺🇸 미 국채*\n" + sp))
    us = []
    for k in ("2년물", "5년물", "10년물", "30년물"):
        v = c["us"].get(k)
        if not v: continue
        bad = _guard(f"미 국채 {k}", v["v"], b["ust"].get(k.replace("물", "")), THRESH["ust"])
        us.append(_tile(f"미 {k}", ERR if bad else f"{_arrow(v['chg'])} *{v['v']:.3f}%*  `{v['chg']:+.1f}bp`"))
    blocks.append(_tiles(us))
    fut = " · ".join(f"{k} `{v['v']:.2f} ({v['chg']:+.0f})`" for k, v in c["kr"].items() if "KTB" in k)
    blocks.append(_sec("*🇰🇷 국고채* (민평3사)\n" + fut))
    kr = []
    for k in ("3년물", "5년물", "10년물", "30년물"):
        v = c["kr"].get(k)
        if not v: continue
        kv = (kb or {}).get("kr", {}).get(k)
        bad = _guard(f"국고 {k}", v["v"], kv["v"] if kv else None, THRESH["kr"])
        kr.append(_tile(f"국고 {k}", ERR if bad else f"{_arrow(v['chg'])} *{v['v']:.3f}%*  `{v['chg']:+.1f}bp`"))
    blocks.append(_tiles(kr))
    if kb and kb.get("credit"):
        blocks.append(_sec("*🏦 크레딧 스프레드* (국고 3년 대비)"))
        blocks.append(_tiles([_tile(k, f"*{v['v']:.1f}bp*  `{v['chg']:+.1f}`") for k, v in list(kb["credit"].items())[:10]]))
    blocks.append(_div())
    fxp = []
    if kb and kb.get("fx", {}).get("달러원"):
        w = kb["fx"]["달러원"]; fxp.append(_tile("달러원 현물", f"{_arrow(w['chg'])} *{w['v']:,.1f}*  `{w['chg']:+.1f}원`"))
    if c["ndf"]:
        fxp.append(_tile("NDF 1M", f"*{c['ndf'][0]:,.2f}*\n_{c['ndf'][1][:26]}_"))
    dxy = c["fx"].get("달러인덱스")
    if dxy:
        bad = _guard("달러인덱스", dxy["v"], b["fx"].get("DXY"), THRESH["dxy"])
        fxp.append(_tile("달러인덱스", ERR if bad else f"{_arrow(dxy['chg'])} *{dxy['v']:.3f}*  `{dxy['chg']:+.3f}`"))
    if c["fx"].get("달러/엔"):
        y = c["fx"]["달러/엔"]; fxp.append(_tile("달러/엔", f"{_arrow(y['chg'])} *{y['v']:.2f}*  `{y['chg']:+.2f}`"))
    for k, key in (("WTI", "wti"), ("금", "gold")):
        v = c["cmdty"].get(k)
        if v:
            bad = _guard(k, v["v"], b["fx"].get(k), THRESH[key], relative=True)
            fxp.append(_tile(k, ERR if bad else f"{_arrow(v['pct'])} *{v['v']:,.2f}*  `{v['pct']:+.2f}%`"))
    blocks.append(_sec("*💱 환 · 원자재*"))
    blocks.append(_tiles(fxp))
    return blocks


# ── 게시물 → 세션 날짜 ──
def _a_session(text):
    m = re.search(r"美 증시 마감 브리핑 \((\d{4})\.(\d{2})\.(\d{2})\)", text)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - timedelta(days=1) if m else None   # 게시일(KST) 전날 = ET 세션


def _b_session(text):
    m = re.search(r"미국 세션: (\d{4}-\d{2}-\d{2})", text)
    return date.fromisoformat(m.group(1)) if m else None


def _c_date(text):
    m = re.search(r"채권 Daily 코멘트\((\d{2})\.(\d{2})\.(\d{2})", text)
    return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _d_date(text):
    m = re.search(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일 채권시장 일일동향", text)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ── 발송 ──
def _send(blocks, text):
    from . import slack_cards
    problems = slack_cards.lint_blocks(blocks)
    if problems:
        raise RuntimeError("카드 형식 위반: " + "; ".join(problems[:5]))
    return slack_cards.send_blocks(blocks, text)


def close_inputs(now: datetime):
    """카드 ① 재료. 반환 {session, a, b, ready}. a·b 는 세션이 맞는 (dt, parsed) 또는 None."""
    session = us_session_for(now)
    if not session:
        return None
    A, B = latest("A", 26), latest("B", 26)   # 06:31·05:20 게시 — 늦은 재시작에도 잡히게 26h, 세션 날짜 검증이 옛 글을 거른다
    a = (A[0], parse_a(A[1])) if A and _a_session(A[1]) == session else None
    b = (B[0], parse_b(B[1])) if B and _b_session(B[1]) == session else None
    return {"session": session, "a": a, "b": b, "ready": bool(a and b)}


def send_close(now: datetime, deadline: bool = False) -> dict:
    """카드 ① — 06:40 부터 한투+투행이 갖춰지면, 07:00(deadline) 이 지나면 있는 것만으로."""
    inp = close_inputs(now)
    if not inp:
        return {"sent": False, "why": "미국장 휴장"}
    if not inp["a"]:
        return {"sent": False, "why": "한투 미수신" + (" — 포기" if deadline else " — 대기")}
    if not inp["b"] and not deadline:
        return {"sent": False, "why": "투행 미수신 — 대기"}
    blocks = close_blocks(inp["a"][1], inp["b"][1] if inp["b"] else None, inp["session"])
    if not inp["b"]:
        blocks.append(_ctx("일부 데이터 미수신"))
    res = _send(blocks, f"미국장 마감 · {inp['session']}")
    return {"sent": bool(res.get("ok")), "ts": res.get("ts"), "errors": list(_errors), "partial": not inp["b"]}


def bond_inputs(now: datetime):
    """카드 ② 재료. 한국 영업일: 키움·KB 오늘자. 휴장: 투행(직전 미국 세션) 만."""
    st = market_status(now)
    today = now.date()
    if st["kr_open_today"]:
        C, D = latest("C", 26), latest("D", 26)
        c = (C[0], parse_c(C[1])) if C and _c_date(C[1]) == today else None
        d = (D[0], parse_kb(D[1])) if D and _d_date(D[1]) == today else None
        B = latest("B", 30)
        b = parse_b(B[1]) if B else None
        # 키움 글은 '직전 미국 세션' 을 다룬다 — 투행 세션이 그 날짜일 때만 대조
        if B and st["us_session"] and _b_session(B[1]) != st["us_session"]:
            b = None
        return {"mode": "full", "session": today, "c": c, "d": d, "b": b, "ready": bool(c and d)}
    if st["us_session"]:
        B = latest("B", 26)
        b = parse_b(B[1]) if B and _b_session(B[1]) == st["us_session"] else None
        return {"mode": "reduced", "session": st["us_session"], "b": b, "ready": bool(b)}
    return None


def send_bond(now: datetime, deadline: bool = False) -> dict:
    inp = bond_inputs(now)
    if not inp:
        return {"sent": False, "why": "미국·한국 모두 휴장"}
    if inp["mode"] == "reduced":
        if not inp["b"]:
            return {"sent": False, "why": "투행 미수신" + (" — 포기" if deadline else " — 대기")}
        blocks = bond_blocks(None, inp["b"], None, inp["session"], reduced=True)
    else:
        if not inp["c"]:
            return {"sent": False, "why": "키움 미수신" + (" — 포기" if deadline else " — 대기")}
        if not inp["d"] and not deadline:
            return {"sent": False, "why": "KB 미수신 — 대기"}
        blocks = bond_blocks(inp["c"][1], inp["b"], inp["d"][1] if inp["d"] else None, inp["session"])
        if not inp["d"]:
            blocks.append(_ctx("일부 데이터 미수신"))
    res = _send(blocks, f"채권·환 동향 · {inp['session']}")
    return {"sent": bool(res.get("ok")), "ts": res.get("ts"), "errors": list(_errors), "mode": inp["mode"]}


def main():
    """--dry-run: 최신 게시물로 두 카드 blocks 를 만들고 lint 만 한다(발송 없음). --send-close / --send-bond: 즉시 발송(마감 무시)."""
    from . import slack_cards
    now = datetime.now(KST)
    if "--send-close" in sys.argv:
        print(send_close(now, deadline=True)); return 0
    if "--send-bond" in sys.argv:
        print(send_bond(now, deadline=True)); return 0
    A, B, C, D = latest("A", 200), latest("B", 200), latest("C", 200), latest("D", 200)
    if not (A and B):
        print("한투/투행 게시물 없음"); return 1
    pa, pb = parse_a(A[1]), parse_b(B[1])
    blocks = close_blocks(pa, pb, _b_session(B[1]))
    print(f"① 블록 {len(blocks)} · lint {slack_cards.lint_blocks(blocks) or 'ok'} · 숨김 {_errors}")
    if C:
        pc = parse_c(C[1]); kb = parse_kb(D[1]) if D else None
        blocks = bond_blocks(pc, pb, kb, _c_date(C[1]))
        print(f"② 블록 {len(blocks)} · lint {slack_cards.lint_blocks(blocks) or 'ok'} · 숨김 {_errors}")
        blocks = bond_blocks(None, pb, None, _b_session(B[1]), reduced=True)
        print(f"②축소 블록 {len(blocks)} · lint {slack_cards.lint_blocks(blocks) or 'ok'}")
    print("status", market_status(now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
