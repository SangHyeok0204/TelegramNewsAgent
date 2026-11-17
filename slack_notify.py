# -*- coding: utf-8 -*-
"""Agents 공용 Slack 알림 모듈 (2026-09-10).

계약:
- 이 파일은 `Agents/` 아래 **모든 에이전트가 공유**한다. 에이전트마다 venv 가 다르고
  일부는 컨테이너에서 돌기 때문에 표준 라이브러리(urllib·json)만 쓴다.
  서드파티 의존성을 추가하지 않는다.
- Slack 앱은 [NewsAgent] 하나다. 키는 `Agents/.env` 한 곳에서만 읽는다:
  SLACK_BOT_TOKEN(xoxb, chat.postMessage) · SLACK_CHANNEL_ID(기본 채널).
- 토큰 값은 출력하지도, 로그에 남기지도 않는다.
- Slack 이 에러를 돌려줘도 예외를 던지지 않는다. 응답 dict(ok/ts/channel/error)를 그대로
  반환하고 로그·재시도 판단은 호출자가 한다.
- 카드 문장은 짧고 쉬운 말(ELI5). 헤더 + 불릿 + 컨텍스트 3단 구조, 값은 굵게.

사용:
    import slack_notify
    slack_notify.send_card("제목", lines=["줄1", "줄2"], context="출처 · 시각")
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://slack.com/api/chat.postMessage"
TIMEOUT_SEC = 10

# Slack Block Kit 제한
HEADER_MAX = 150
SECTION_MAX = 3000
FIELD_MAX = 2000
FIELDS_MAX = 10          # 섹션당 필드 10개가 상한 — 그 이상은 섹션을 나눈다


def _env_path(path=None) -> str:
    """기본값: 이 파일과 같은 폴더의 `.env` (= Agents/.env)."""
    return path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=None) -> dict:
    """`Agents/.env` 의 KEY=VALUE 를 읽어 os.environ 과 합친 view 를 돌려준다.

    이미 os.environ 에 있는 키는 덮어쓰지 않는다(컨테이너 env_file 이 우선).
    os.environ 자체는 건드리지 않는다 — 호출자 프로세스 환경을 오염시키지 않기 위해서다.
    """
    merged = {}
    try:
        with open(_env_path(path), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                merged[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass                                    # .env 없음 → 환경변수만으로 동작
    merged.update({k: v for k, v in os.environ.items() if v})
    return merged


def _cut(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def card(title, lines=None, fields=None, context=None, emoji=None) -> list:
    """헤더 + 불릿 + (필드) + 컨텍스트 3단 카드 blocks 를 만든다.

    lines   : 불릿 문자열 리스트 → 한 섹션에 "\\n" 으로 합침(mrkdwn)
    fields  : (label, value) 리스트 → "*label*\\nvalue" 필드, 최대 10개(10개씩 섹션 분할)
    context : 작은 회색 글씨 한 줄(출처 · 시각 등)
    emoji   : 헤더 앞에 붙일 이모지
    """
    head = f"{emoji} {title}" if emoji else str(title)
    blocks = [{"type": "header",
               "text": {"type": "plain_text", "text": _cut(head, HEADER_MAX), "emoji": True}}]

    if lines:
        body = "\n".join(str(x) for x in lines)
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": _cut(body, SECTION_MAX)}})

    items = list(fields or [])[:FIELDS_MAX]
    for i in range(0, len(items), FIELDS_MAX):
        chunk = items[i:i + FIELDS_MAX]
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": _cut(f"*{label}*\n{value}", FIELD_MAX)}
            for label, value in chunk
        ]})

    if context:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": _cut(context, SECTION_MAX)}]})
    return blocks


def lint_blocks(blocks) -> list:
    """카드 형식 하네스 (2026-09-26 owner, 레퍼런스 = TelegramNewsAgent us_close 카드).
    블록 ≤50 · header ≤150자 · 텍스트 ≤3,000자 · fields ≤10 · 한 줄 ≤140자(URL 제외) · 줄바꿈 없는 문단 ≤200자 · carousel 금지.
    table 은 허용(2026-09-28 owner) — 대신 Slack 제한(≤100행·≤20열·열 수 일치·전체 ≤10,000자)을 검사한다.
    위반 목록을 돌려준다(비면 통과). 표준 라이브러리만."""
    out = []
    if len(blocks) > 50:
        out.append(f"블록 {len(blocks)}개 > 50")
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t == "carousel":
            out.append(f"#{i} carousel 블록 금지")
        if t == "table":
            rows, cols = b.get("rows") or [], b.get("column_settings") or []
            if len(rows) > 100:
                out.append(f"#{i} table {len(rows)}행 > 100")
            if cols and {len(r) for r in rows} != {len(cols)}:
                out.append(f"#{i} table 열 수 불일치(column_settings {len(cols)})")
            if max((len(r) for r in rows), default=0) > 20:
                out.append(f"#{i} table 20열 초과")
            if len(json.dumps(rows, ensure_ascii=False)) > 10000:
                out.append(f"#{i} table 10,000자 초과")
        texts = []
        if t == "header" and len(b.get("text", {}).get("text", "")) > 150:
            out.append(f"#{i} header 150자 초과")
        elif t == "section":
            if b.get("fields"):
                if len(b["fields"]) > 10:
                    out.append(f"#{i} fields {len(b['fields'])}개 > 10")
                texts = [f.get("text", "") for f in b["fields"]]
            if b.get("text"):
                texts.append(b["text"].get("text", ""))
        elif t == "context":
            texts = [e.get("text", "") for e in b.get("elements", []) if e.get("type") == "mrkdwn"]
        for s in texts:
            if len(s) > 3000:
                out.append(f"#{i} 텍스트 3,000자 초과")
            for line in s.split("\n"):
                if len(line) > 140 and "http" not in line:
                    out.append(f"#{i} 한 줄 {len(line)}자 > 140")
            if "\n" not in s and len(s) > 200 and "http" not in s:
                out.append(f"#{i} 줄바꿈 없는 문단 {len(s)}자 > 200")
    return out


def post(blocks=None, text="", channel=None, thread_ts=None, token=None) -> dict:
    """chat.postMessage. 성공/실패 모두 파싱된 응답 dict 를 반환한다(예외 없음).

    text 는 알림(푸시·미리보기) 폴백 문구다. 네트워크 실패도 {"ok": False, "error": ...} 로 돌린다.
    """
    env = load_env()
    token = token or env.get("SLACK_BOT_TOKEN", "")
    channel = channel or env.get("SLACK_CHANNEL_ID", "")
    if not token:
        return {"ok": False, "error": "missing_SLACK_BOT_TOKEN"}
    if not channel:
        return {"ok": False, "error": "missing_SLACK_CHANNEL_ID"}

    payload = {"channel": channel, "text": _cut(text, SECTION_MAX),
               "unfurl_links": False, "unfurl_media": False}  # 2026-09-13 owner: 링크 미리보기(원문 발췌·이미지) 끔 — 하이퍼링크만
    if blocks:
        for p in lint_blocks(blocks):   # 2026-09-26 owner: 카드 형식 하네스(공용) — 위반은 경고만, 발송은 막지 않는다
            print(f"[card-lint] {text[:20]} — {p}")
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:          # Slack 은 4xx 에도 JSON 본문을 준다
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"http_{e.code}"}
    except Exception as e:                       # 타임아웃·DNS·TLS — 토큰은 절대 싣지 않는다
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def post_webhook(url, blocks=None, text="") -> dict:
    """Incoming Webhook 으로 보낸다(2026-10-10). 응답이 JSON 이 아니라 "ok" 문자열이라 ts 는 없다. 예외 없음."""
    payload = {"text": _cut(text, SECTION_MAX), "unfurl_links": False, "unfurl_media": False}
    if blocks:
        payload["blocks"] = blocks
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            body = r.read().decode("utf-8")
            return {"ok": body == "ok", "error": None if body == "ok" else body}
    except urllib.error.HTTPError as e:          # 웹훅은 4xx 본문이 invalid_blocks 같은 짧은 문자열이다
        return {"ok": False, "error": f"http_{e.code}: {e.read().decode('utf-8', 'replace')[:100]}"}
    except Exception as e:                       # URL 은 비밀값이라 싣지 않는다
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def send_card(title, lines=None, fields=None, context=None,
              channel=None, thread_ts=None, token=None) -> dict:
    """카드를 만들어 바로 보낸다. 알림 폴백 text = 제목 + 첫 줄.
    token: 다른 Slack 앱(예: MacroAgent 봇)으로 보낼 때만 넘긴다. 기본은 .env 의 SLACK_BOT_TOKEN."""
    first = str(lines[0]) if lines else ""
    return post(blocks=card(title, lines, fields, context),
                text=f"{title} — {first}".strip(" —"),
                channel=channel, thread_ts=thread_ts, token=token)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print('사용: python slack_notify.py "제목" ["줄1" "줄2" ...]')
        raise SystemExit(2)
    if not load_env().get("SLACK_CHANNEL_ID"):
        print("SLACK_CHANNEL_ID 가 비어 있습니다 — Agents/.env 의 SLACK_CHANNEL_ID 를 채운 뒤 다시 실행하세요."
              " (Slack 앱 [NewsAgent] 를 채널에 초대하고 채널 ID(C...)를 넣으면 됩니다.)")
        raise SystemExit(2)
    res = send_card(args[0], lines=args[1:] or None, context="slack_notify 테스트")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    raise SystemExit(0 if res.get("ok") else 1)
