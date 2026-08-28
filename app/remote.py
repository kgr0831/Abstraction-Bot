"""로컬이냐 원격이냐를 여기서만 판단한다.

봇과 DB 는 한 곳에서만 돈다. 팀원 PC 의 콘솔에는 DB 가 없으므로 조회를
서버로 넘겨야 한다. launcher 와 mcp_server 가 둘 다 이 모듈을 쓴다.

SERVER_URL 이 자기 자신이면 로컬 모드 — db 를 직접 읽는다.
다른 주소면 클라이언트 모드 — 그 주소의 HTTP API 를 탄다.

새 의존성 없이 stdlib urllib 로 끝낸다.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import db
from datetime import datetime

PORT = int(os.getenv("LAUNCHER_PORT", "8787"))
SELF = "http://127.0.0.1:%d" % PORT
CONFIG = db.PROJECT / "data" / "config.json"


def _configured():
    """SERVER_URL 환경변수 > data/config.json > 자기 자신."""
    if os.getenv("SERVER_URL"):
        return os.environ["SERVER_URL"]
    try:
        return json.loads(CONFIG.read_text("utf-8")).get("server_url") or SELF
    except (OSError, json.JSONDecodeError, AttributeError):
        return SELF


SERVER_URL = _configured().rstrip("/")
SESSION = db.PROJECT / "data" / "session.json"
TIMEOUT = 30


def is_remote():
    return SERVER_URL not in (SELF, "http://localhost:%d" % PORT)


def token():
    """페어링 때 서버가 준 클라이언트 토큰. 원격 요청의 신분증이다."""
    if not SESSION.exists():
        return ""
    try:
        return json.loads(SESSION.read_text("utf-8")).get("token", "")
    except (json.JSONDecodeError, OSError):
        return ""


def _req(method, path, params=None, body=None):
    url = SERVER_URL + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    tok = token()
    if tok:
        headers["X-Abstraction-Token"] = tok
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, {"error": "서버 오류 (%s)" % e.code}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 503, {"error": "서버(%s)에 연결하지 못했습니다: %s" % (SERVER_URL, e)}


def get(path, **params):
    return _req("GET", path, params=params)


def post(path, body):
    return _req("POST", path, body=body)


# --- 조회 (mcp_server 와 콘솔이 같이 쓴다) ----------------------------------
# 로컬이면 db 를 직접, 원격이면 서버 API 를. 반환 형태는 둘이 같아야 한다.


def to_json(rows):
    """db 행 -> 콘솔/MCP 가 같이 쓰는 한 가지 모양. 시각은 KST ISO."""
    out = []
    for r in rows:
        keys = r.keys()
        out.append({
            "id": r["id"],
            "author": r["author_name"],
            "content": r["content"],
            "at": datetime.fromisoformat(r["created_at"]).astimezone(db.KST).isoformat(),
            "channel": r["channel_name"],
            "reply_to": r["reply_to_id"] if "reply_to_id" in keys else None,
            "thread": r["thread_id"] if "thread_id" in keys else None,
            "files": len(json.loads(r["attachments"]))
                     if ("attachments" in keys and r["attachments"]) else 0,
            "guild_id": r["guild_id"],
            "channel_id": r["channel_id"],
        })
    return out


def _local(fn, *a, **kw):
    conn = db.connect()
    try:
        return [dict(r) for r in fn(conn, *a, **kw)]
    finally:
        conn.close()


def channels():
    if is_remote():
        st, body = get("/api/channels")
        return body if st == 200 and isinstance(body, list) else []
    return _local(db.list_channels)


def conversation(channel, date, until=None):
    if is_remote():
        st, body = get("/api/conversation", channel=channel, date=date, until=until or "")
        return (body or {}).get("messages", []) if st == 200 else []
    conn = db.connect()
    try:
        return to_json(db.get_conversation(conn, channel, date, until))
    finally:
        conn.close()


def search(query, channel=None, since=None, until=None, limit=200):
    if is_remote():
        st, body = get("/api/search", q=query, channel=channel or "", since=since or "",
                       until=until or "", limit=limit)
        return (body or {}).get("messages", []) if st == 200 else []
    conn = db.connect()
    try:
        return to_json(db.search_messages(conn, query, channel, since, until, limit))
    finally:
        conn.close()


def days(channel=None, n=7):
    if is_remote():
        st, body = get("/api/days", channel=channel or "", days=n)
        return [(d["date"], d["count"]) for d in body] if st == 200 and body else []
    conn = db.connect()
    try:
        return db.day_counts(conn, channel, n)
    finally:
        conn.close()


def error_hint():
    """원격인데 서버가 안 잡힐 때 사용자에게 보일 말."""
    return ("서버(%s)에 연결하지 못했습니다. 주소가 맞는지, 서버가 켜져 있는지 확인하세요."
            % SERVER_URL)
