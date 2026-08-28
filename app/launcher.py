"""세팅 콘솔 — 이 프로젝트의 GUI 전부.

디스코드에서 /시작 을 치면 여기 링크가 온다. 슬래시 커맨드로 하던 일은
거의 다 여기로 옮겼다.

  대화     채팅형 뷰어 · 날짜 범위 · 검색
  수집     채널 켜고 끄기 · 과거 대화 가져오기 (서버 관리 권한자만)
  연결     에이전트 계정 · MCP (기본 제공은 클릭 한 번, 나머지는 URL 직접)
  리더보드 발언 집계

orca 패턴: 계정마다 격리된 홈을 만들고 그 안에서 벤더 CLI 자체의 로그인을 돌린다.
자격증명은 이 PC를 떠나지 않는다.

SERVER_URL 이 페어링을 받는 쪽이다. 봇이 다른 머신으로 가면 이 값만 바꾼다.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

import db
import remote

load_dotenv()

ROOT = Path(__file__).resolve().parent          # app/
PROJECT = ROOT.parent                          # 저장소 루트
AGENTS_DIR = PROJECT / "data" / "agents"
# 로컬 전용(gitignore) 먼저, 없으면 저장소에 넣어 배포하는 쪽
MASCOT_DIRS = (PROJECT / "data" / "mascot", ROOT / "mascot")
SESSION = PROJECT / "data" / "session.json"
ACCOUNTS = db.ACCOUNTS_PATH
PORT = int(os.getenv("LAUNCHER_PORT", "8787"))
SERVER_URL = remote.SERVER_URL

AGENTS = {
    "claude": {
        "label": "Claude",
        "home_env": "CLAUDE_CONFIG_DIR",
        "login": ["claude", "auth", "login"],
        "status": ["claude", "auth", "status"],
        "mcp": True,
    },
    "codex": {
        "label": "Codex",
        "home_env": "CODEX_HOME",
        "login": ["codex", "login"],
        "status": ["codex", "login", "status"],
        "mcp": False,  # codex 는 config.toml 직접 편집이라 여기서 다루지 않는다
    },
}

# 기본 제공 MCP — 사용자는 URL 을 입력하지 않는다. 클릭하면 붙고,
# 인증이 필요하면 CLI 가 브라우저를 띄운다.
# URL 이 바뀌면 여기 한 줄만 고친다. 목록에 없는 건 '온디맨드' 탭에서 붙인다.
MCP_CATALOG = {
    "notion": {"label": "Notion", "url": "https://mcp.notion.com/mcp",
               "desc": "요약·아이디어를 노션 DB에 기록"},
    "figma": {"label": "Figma", "url": "https://mcp.figma.com/mcp",
              "desc": "디자인 파일 읽기·생성"},
    "linear": {"label": "Linear", "url": "https://mcp.linear.app/mcp",
               "desc": "이슈·프로젝트"},
    "sentry": {"label": "Sentry", "url": "https://mcp.sentry.dev/mcp",
               "desc": "에러 추적"},
    "github": {"label": "GitHub", "url": "https://api.githubcopilot.com/mcp/",
               "desc": "저장소·이슈·PR"},
}

# 이 프로젝트 자신. stdio 라 URL 이 없고, 경로는 자동으로 채운다.
SELF_MCP = {
    "name": "discord-collector",
    "label": "Discord 수집기 (이 프로젝트)",
    "desc": "수집된 대화를 Claude Code에서 조회",
    "args": [sys.executable, str(ROOT / "mcp_server.py")],
}

jobs = {}


# --- 저장/실행 유틸 ---------------------------------------------------------

def load_accounts():
    return db.local_accounts()


def save_accounts(accts):
    ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS.write_text(json.dumps(accts, ensure_ascii=False, indent=2), "utf-8")


def agent_env(agent, home):
    return {**os.environ, AGENTS[agent]["home_env"]: str(home)}


def run_cli(agent, home, args, timeout=60):
    try:
        return subprocess.run(
            args, env=agent_env(agent, home), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            shell=os.name == "nt",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def identity(agent, home):
    """CLI 자신에게 물어본다 — 자격증명 파일 형식을 추측하지 않는다."""
    out = run_cli(agent, home, AGENTS[agent]["status"], timeout=30).stdout
    try:
        d = json.loads(out)
        if not d.get("loggedIn", True):
            return None
        return d.get("email") or d.get("account") or "로그인됨"
    except json.JSONDecodeError:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", out)
        if m:
            return m.group(0)
        return out.strip().splitlines()[0] if out.strip() else None


def post_json(url, payload):
    """SERVER_URL 로 보내는 유일한 요청. 새 의존성 없이 stdlib 로 끝낸다."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, {"ok": False, "error": str(e)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 502, {"ok": False, "error": f"서버({SERVER_URL})에 연결하지 못했습니다: {e}"}


def load_session():
    """마지막 연동 정보. 봇 DB 가 멀면 여기서 UI 를 복원한다 (표시 전용)."""
    if not SESSION.exists():
        return {}
    try:
        return json.loads(SESSION.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_session(**kw):
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    SESSION.write_text(json.dumps({**load_session(), **kw}, ensure_ascii=False, indent=2), "utf-8")


def state_rev():
    """UI 가 이걸 폴링해서 바뀐 것만 다시 그린다. 계정·연동·채널·메시지·봇상태."""
    try:
        a = ACCOUNTS.stat().st_mtime_ns
    except OSError:
        a = 0
    conn = db.connect()
    try:
        st = db.stats(conn)
        links = len(db.linked_identities(conn))
    finally:
        conn.close()
    b = bot_bridge()
    return "%d.%d.%d.%d.%d" % (a, links, st["channels"], st["messages"],
                              int(bool(b and b.ready())))


_icon = None


def bot_icon():
    """봇 아바타 URL. 한 번만 물어보고 캐시한다. 실패하면 None (글자 로고로 대체)."""
    global _icon
    if _icon is not None:
        return _icon or None
    _icon = ""
    tok = os.getenv("DISCORD_TOKEN")
    if not tok:
        return None
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": "Bot " + tok,
                     "User-Agent": "DiscordBot (abstraction-console, 0.1)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("avatar"):
            _icon = "https://cdn.discordapp.com/avatars/%s/%s.png?size=64" % (d["id"], d["avatar"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return _icon or None


def find_account(home):
    for a in load_accounts():
        if a["home"] == home:
            return a
    return None


def primary_account():
    accts = load_accounts()
    return next((a for a in accts if a["agent"] == "claude"), accts[0] if accts else None)


def bot_bridge():
    """봇 모듈. 콘솔만 단독 실행 중이면 None."""
    return sys.modules.get("bot")


def do_login(job_id, agent):
    job = jobs[job_id]
    spec = AGENTS[agent]
    home = AGENTS_DIR / agent / job_id
    home.mkdir(parents=True, exist_ok=True)
    job["home"] = str(home)
    try:
        p = subprocess.Popen(
            spec["login"], env=agent_env(agent, home), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, shell=os.name == "nt",
        )
        for line in p.stdout:
            line = line.rstrip()
            if line:
                job["lines"].append(line)
            m = re.search(r"https?://\S+", line)
            if m and not job.get("url"):
                # 브라우저가 저절로 안 열리면 사용자가 이 링크를 직접 누른다
                job["url"] = m.group(0)
        p.wait(timeout=600)
    except Exception as e:
        job["lines"].append("[오류] %s: %s" % (type(e).__name__, e))

    who = identity(agent, home)
    if who:
        accts = [a for a in load_accounts() if a["home"] != str(home)]
        accts.append({"agent": agent, "identity": who, "home": str(home)})
        save_accounts(accts)
        job["identity"] = who
        job["ok"] = True
        if AGENTS[agent]["mcp"]:
            # 이 프로젝트의 MCP 는 자동으로 붙인다. 사용자가 할 일이 없다.
            run_cli(agent, home,
                    ["claude", "mcp", "add", SELF_MCP["name"], "--"] + SELF_MCP["args"],
                    timeout=60)
            job["lines"].append("discord-collector MCP 자동 연결됨")
    else:
        job["lines"].append("[실패] 로그인이 완료되지 않았습니다.")
    job["done"] = True


# --- 인증 · 프록시 -----------------------------------------------------------

def auth_link(request):
    """원격 콘솔이 보낸 토큰으로 사용자를 식별한다. 토큰이 없으면 None."""
    tok = request.headers.get("X-Abstraction-Token", "")
    if not tok:
        return None
    conn = db.connect()
    try:
        return db.link_by_token(conn, tok)
    finally:
        conn.close()


def allowed(request):
    # ponytail: 토큰이거나 루프백이거나. 팀 밖에 공개할 거면 제대로 된 인증이 필요하다
    host = (request.client.host if request.client else "") or ""
    return bool(auth_link(request)) or host in ("127.0.0.1", "::1", "localhost")


async def forward(request):
    """클라이언트 모드 — 이 요청을 그대로 서버로 넘긴다."""
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
    st, out = await run_in_threadpool(
        remote._req, request.method, request.url.path,
        dict(request.query_params) if request.method == "GET" else None, body)
    return JSONResponse(out, status_code=st)


def shared(fn):
    """서버의 DB·봇이 필요한 엔드포인트. 클라이언트면 넘기고, 서버면 신분을 본다."""
    async def wrap(request):
        if remote.is_remote():
            return await forward(request)
        if not allowed(request):
            return JSONResponse({"error": "인증이 필요합니다."}, status_code=401)
        return await fn(request)
    wrap.__name__ = getattr(fn, "__name__", "shared")
    return wrap


# --- 기본 라우트 ------------------------------------------------------------

async def page(request):
    # 요청마다 읽는다 — 파일이 작고, 고칠 때 서버를 껐다 켜지 않아도 된다
    return HTMLResponse(
        (ROOT / "console.html").read_text(encoding="utf-8").replace("__AGENTS__", json.dumps(
            {k: v["label"] for k, v in AGENTS.items()}, ensure_ascii=False))
        .replace("__MCP__", json.dumps(MCP_CATALOG, ensure_ascii=False))
        .replace("__SELFMCP__", json.dumps(
            {k: SELF_MCP[k] for k in ("name", "label", "desc")}, ensure_ascii=False))
        .replace("__CODE__", json.dumps(request.query_params.get("code", "")))
    )


async def api_me(request):
    """이 콘솔이 누구 것이고 무엇을 할 수 있는가."""
    acct = primary_account()
    out = {"account": acct, "linked": False, "user_name": None, "is_manager": False,
           "bot_ready": False, "server_url": SERVER_URL, "remote": remote.is_remote()}
    b = bot_bridge()
    out["bot_ready"] = bool(b and b.ready())
    out["icon"] = await run_in_threadpool(bot_icon)
    if acct:
        conn = db.connect()
        try:
            row = db.link_by_identity(conn, acct["identity"])
        finally:
            conn.close()
        if row:
            out.update(linked=True, user_name=row["user_name"],
                       is_manager=bool(row["is_manager"]))
            save_session(identity=acct["identity"], agent=acct["agent"],
                         user_name=row["user_name"], is_manager=bool(row["is_manager"]))
    if not out["linked"]:
        # 서버 DB 가 아직/이미 없을 때 마지막 연동을 화면에 복원한다.
        # 실제 권한 판정은 항상 DB 로 한다 (_manager_ok).
        sess = load_session()
        if sess.get("identity") and acct and sess["identity"] == acct["identity"]:
            out.update(user_name=sess.get("user_name"), stale=True)
    out["rev"] = await run_in_threadpool(state_rev)
    out["mascot"] = mascot_names()
    return JSONResponse(out)


async def api_state(request):
    """폴링 전용 — 가볍게 유지한다 (아이콘 조회 같은 건 하지 않는다)."""
    b = bot_bridge()
    return JSONResponse({"rev": await run_in_threadpool(state_rev),
                         "bot_ready": bool(b and b.ready())})


MASCOT_STATES = ("idle", "working", "happy", "sleep")
MASCOT_EXT = (".png", ".webp", ".gif", ".jpg", ".svg")


def mascot_file(name):
    for d in MASCOT_DIRS:
        for ext in MASCOT_EXT:
            f = d / (name + ext)
            if f.exists():
                return f
    return None


def mascot_names():
    """넣어둔 커스텀 이미지 목록. 없으면 콘솔이 내장 SVG 를 쓴다."""
    return [st for st in MASCOT_STATES if mascot_file(st) or mascot_file("hebi")]


async def mascot(request):
    st = request.path_params["state"]
    if st not in MASCOT_STATES:
        return JSONResponse({"error": "unknown state"}, 404)
    f = mascot_file(st) or mascot_file("hebi")
    return FileResponse(f) if f else JSONResponse({"error": "no asset"}, 404)


async def api_accounts(request):
    accts = load_accounts()
    conn = db.connect()
    try:
        linked = db.linked_identities(conn)
    finally:
        conn.close()
    for a in accts:
        a["linked"] = a.get("identity") in linked
        a["mcp_capable"] = AGENTS.get(a["agent"], {}).get("mcp", False)
    return JSONResponse(accts)


async def api_link(request):
    agent = request.path_params["agent"]
    if agent not in AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"lines": [], "done": False, "ok": False, "identity": None}
    threading.Thread(target=do_login, args=(job_id, agent), daemon=True).start()
    return JSONResponse({"id": job_id})


async def api_job(request):
    jid = request.path_params["job_id"]
    if jid in jobs:                      # 로그인 등 이 PC 에서 도는 작업
        return JSONResponse(jobs[jid])
    if remote.is_remote():               # 백필처럼 서버에서 도는 작업
        return await forward(request)
    return JSONResponse({"lines": ["없는 작업"], "done": True})


async def api_unlink(request):
    body = await request.json()
    home = body.get("home", "")
    save_accounts([a for a in load_accounts() if a["home"] != home])
    try:
        # 격리 홈 안쪽만 지운다 — 경로를 넘겨받으므로 반드시 확인한다
        if home and Path(home).resolve().is_relative_to(AGENTS_DIR.resolve()):
            shutil.rmtree(home, ignore_errors=True)
    except (ValueError, OSError):
        pass
    return JSONResponse({"ok": True})


# --- 페어링 -----------------------------------------------------------------

async def api_pair(request):
    """서버 쪽. 디스코드가 발급한 코드를 소진하고 바인딩을 만든다."""
    body = await request.json()
    code, agent, ident = body.get("code", ""), body.get("agent", ""), body.get("identity", "")
    if not (code and agent and ident):
        return JSONResponse({"ok": False, "error": "코드와 계정이 모두 필요합니다."}, 400)
    conn = db.connect()
    try:
        got = db.redeem_pair_code(conn, code, agent, ident)
    finally:
        conn.close()
    if not got:
        return JSONResponse(
            {"ok": False, "error": "코드가 틀렸거나 만료됐습니다. 디스코드에서 /시작 을 다시 실행하세요."},
            400,
        )
    return JSONResponse({"ok": True, "user_name": got[1], "token": got[2]})


async def api_discord_link(request):
    """클라이언트 쪽. 이 PC의 계정을 SERVER_URL 에 페어링한다."""
    body = await request.json()
    acct = find_account(body.get("home", "")) or primary_account()
    if not acct:
        return JSONResponse({"ok": False, "error": "먼저 에이전트 계정을 연결하세요."}, 400)
    status, out = await run_in_threadpool(
        post_json,
        SERVER_URL.rstrip("/") + "/api/pair",
        {"code": body.get("code", ""), "agent": acct["agent"], "identity": acct["identity"]},
    )
    return JSONResponse(out, status_code=status)


# --- MCP --------------------------------------------------------------------

def _mcp_names(agent, home):
    out = run_cli(agent, home, ["claude", "mcp", "list"]).stdout
    return [
        m.group(1) for line in out.splitlines()
        if (m := re.match(r"\s*([\w-]+):", line))
    ]


async def api_mcp_list(request):
    acct = find_account(request.query_params.get("home", "")) or primary_account()
    if not acct or not AGENTS.get(acct["agent"], {}).get("mcp"):
        return JSONResponse({"servers": [], "note": "Claude 계정을 연결하면 MCP를 붙일 수 있습니다."})
    # CLI 호출이라 스레드로 보낸다 — 안 그러면 목록 뽑는 동안 서버 전체가 멈춘다
    names = await run_in_threadpool(_mcp_names, acct["agent"], acct["home"])
    return JSONResponse({"servers": names})


async def api_mcp_add(request):
    body = await request.json()
    acct = find_account(body.get("home", "")) or primary_account()
    if not acct:
        return JSONResponse({"ok": False, "error": "계정을 찾을 수 없습니다."}, 400)
    key = body.get("key", "")
    if key == SELF_MCP["name"]:
        args = ["claude", "mcp", "add", SELF_MCP["name"], "--"] + SELF_MCP["args"]
        name = SELF_MCP["name"]
    elif key in MCP_CATALOG:
        name, url = key, MCP_CATALOG[key]["url"]
        args = ["claude", "mcp", "add", "--transport", "http", name, url]
    else:
        # 온디맨드 — 지원 목록에 없는 서버는 사용자가 주소를 준다
        name, url = body.get("name", "").strip(), body.get("url", "").strip()
        if not re.fullmatch(r"[\w-]{1,64}", name):
            return JSONResponse({"ok": False, "error": "이름은 영문·숫자·-·_ 만 됩니다."}, 400)
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"ok": False, "error": "주소는 http:// 또는 https:// 로 시작해야 합니다."}, 400)
        args = ["claude", "mcp", "add", "--transport", "http", name, url]
    r = await run_in_threadpool(run_cli, acct["agent"], acct["home"], args, 120)
    return JSONResponse({"ok": r.returncode == 0, "name": name,
                         "output": (r.stdout + r.stderr).strip()[-1500:]})


async def api_mcp_remove(request):
    body = await request.json()
    acct = find_account(body.get("home", "")) or primary_account()
    name = body.get("name", "").strip()
    if not acct or not re.fullmatch(r"[\w-]{1,64}", name):
        return JSONResponse({"ok": False, "error": "잘못된 요청입니다."}, 400)
    r = await run_in_threadpool(
        run_cli, acct["agent"], acct["home"], ["claude", "mcp", "remove", name], 60)
    return JSONResponse({"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[-1500:]})


# --- 대화 -------------------------------------------------------------------

@shared
async def api_channels(request):
    conn = db.connect()
    try:
        return JSONResponse([dict(c) for c in db.list_channels(conn)])
    finally:
        conn.close()


@shared
async def api_conversation(request):
    q = request.query_params
    channel, date, until = q.get("channel", ""), q.get("date", ""), q.get("until", "")
    for d in (date, until):
        if d and not db.valid_date(d):
            return JSONResponse({"error": f"날짜 형식이 잘못됐습니다: {d}"}, 400)
    if not (channel and date):
        return JSONResponse({"error": "채널과 날짜가 필요합니다."}, 400)
    if until and until < date:
        return JSONResponse({"error": "끝 날짜가 시작 날짜보다 앞섭니다."}, 400)
    conn = db.connect()
    try:
        rows = db.get_conversation(conn, channel, date, until or None)
    finally:
        conn.close()
    return JSONResponse({"messages": remote.to_json(rows)})


@shared
async def api_search(request):
    q = request.query_params
    if not q.get("q"):
        return JSONResponse({"messages": []})
    conn = db.connect()
    try:
        rows = db.search_messages(
            conn, q["q"], q.get("channel") or None, q.get("since") or None,
            q.get("until") or None, min(int(q.get("limit", 200)), 500),
        )
    finally:
        conn.close()
    return JSONResponse({"messages": remote.to_json(rows)})


@shared
async def api_days(request):
    q = request.query_params
    conn = db.connect()
    try:
        rows = db.day_counts(conn, q.get("channel") or None, min(int(q.get("days", 14)), 120))
    finally:
        conn.close()
    return JSONResponse([{"date": d, "count": n} for d, n in rows])


# --- 수집 관리 (서버 관리 권한자만) ------------------------------------------

def _manager_ok(request=None):
    row = auth_link(request) if request is not None else None
    if row is None:
        acct = primary_account()
        if not acct:
            return False, "에이전트 계정을 먼저 연결하세요."
        conn = db.connect()
        try:
            row = db.link_by_identity(conn, acct["identity"])
        finally:
            conn.close()
    if not row:
        return False, "디스코드 연동을 먼저 하세요."
    if not row["is_manager"]:
        return False, "서버 관리 권한이 필요합니다."
    return True, None


@shared
async def api_guild_channels(request):
    b = bot_bridge()
    if not b:
        return JSONResponse({"error": "봇이 실행 중이 아닙니다. start.bat 으로 실행하세요."}, 503)
    try:
        return JSONResponse(await run_in_threadpool(b.guild_channels))
    except Exception as e:
        return JSONResponse({"error": str(e)}, 503)


@shared
async def api_collect(request):
    ok, why = _manager_ok(request)
    if not ok:
        return JSONResponse({"ok": False, "error": why}, 403)
    b = bot_bridge()
    if not b:
        return JSONResponse({"ok": False, "error": "봇이 실행 중이 아닙니다."}, 503)
    body = await request.json()
    cid, on = body.get("channel_id", ""), bool(body.get("on"))
    try:
        if on:
            name = await run_in_threadpool(
                b.start_collect, cid, bool(body.get("notice", True)))
            return JSONResponse({"ok": True, "message": f"#{name} 수집 시작"})
        await run_in_threadpool(b.stop_collect, cid)
        return JSONResponse({"ok": True, "message": "수집 중지"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)


@shared
async def api_backfill(request):
    ok, why = _manager_ok(request)
    if not ok:
        return JSONResponse({"ok": False, "error": why}, 403)
    b = bot_bridge()
    if not b:
        return JSONResponse({"ok": False, "error": "봇이 실행 중이 아닙니다."}, 503)
    body = await request.json()
    cid = body.get("channel_id", "")
    since, until, days = body.get("since", ""), body.get("until", ""), body.get("days")
    for d in (since, until):
        if d and not db.valid_date(d):
            return JSONResponse({"ok": False, "error": f"날짜 형식이 잘못됐습니다: {d}"}, 400)
    if since and until and until < since:
        return JSONResponse({"ok": False, "error": "끝 날짜가 시작 날짜보다 앞섭니다."}, 400)

    after = before = None
    if since:
        after = datetime.fromisoformat(db.kst_day_range(since)[0])
        before = datetime.fromisoformat(db.kst_day_range(until)[1]) if until else None
        span = f"{since}~{until or '오늘'}"
    else:
        days = int(days or 90)
        span = f"최근 {days}일"

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"count": 0, "done": False, "ok": False, "span": span, "lines": []}

    def work():
        try:
            n = b.run_backfill(cid, after, before, days if not since else None,
                               jobs[job_id])
            jobs[job_id].update(count=n, ok=True)
        except Exception as e:
            jobs[job_id]["lines"].append(str(e))
        jobs[job_id]["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return JSONResponse({"ok": True, "id": job_id, "span": span})


@shared
async def api_optout(request):
    """내 수집 거부 상태. 디스코드 연동이 돼 있어야 누구인지 안다."""
    row = auth_link(request)
    acct = primary_account()
    conn = db.connect()
    try:
        if row is None:
            row = db.link_by_identity(conn, acct["identity"]) if acct else None
        if not row:
            return JSONResponse({"ok": False, "error": "디스코드 연동을 먼저 하세요."}, 400)
        if request.method == "GET":
            return JSONResponse({"ok": True, "opted_out": db.is_opted_out(conn, row["user_id"])})
        body = await request.json()
        if body.get("out"):
            n = db.opt_out(conn, row["user_id"])
            return JSONResponse({"ok": True, "opted_out": True,
                                 "message": f"제외했습니다. 저장된 {n}건도 조회에서 빠집니다."})
        db.opt_in(conn, row["user_id"])
        return JSONResponse({"ok": True, "opted_out": False,
                             "message": "앞으로의 메시지부터 다시 수집합니다."})
    finally:
        conn.close()


@shared
async def api_leaderboard(request):
    days = request.query_params.get("days", "7")
    channel = request.query_params.get("channel") or None
    since = None
    if days.isdigit():
        since = (datetime.now(db.KST).date() - timedelta(days=int(days) - 1)).isoformat()
    conn = db.connect()
    try:
        return JSONResponse({"rows": db.leaderboard(conn, since, channel), "since": since})
    finally:
        conn.close()


app = Starlette(routes=[
    Route("/", page),
    Route("/leaderboard", page),
    Route("/api/me", api_me),
    Route("/api/state", api_state),
    Route("/mascot/{state}", mascot),
    Route("/api/accounts", api_accounts),
    Route("/api/link/{agent}", api_link, methods=["POST"]),
    Route("/api/job/{job_id}", api_job),
    Route("/api/unlink", api_unlink, methods=["POST"]),
    Route("/api/pair", api_pair, methods=["POST"]),
    Route("/api/discord-link", api_discord_link, methods=["POST"]),
    Route("/api/mcp", api_mcp_list),
    Route("/api/mcp/add", api_mcp_add, methods=["POST"]),
    Route("/api/mcp/remove", api_mcp_remove, methods=["POST"]),
    Route("/api/channels", api_channels),
    Route("/api/conversation", api_conversation),
    Route("/api/search", api_search),
    Route("/api/days", api_days),
    Route("/api/guild-channels", api_guild_channels),
    Route("/api/collect", api_collect, methods=["POST"]),
    Route("/api/backfill", api_backfill, methods=["POST"]),
    Route("/api/optout", api_optout, methods=["GET", "POST"]),
    Route("/api/leaderboard", api_leaderboard),
])

if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
