"""사용자 세팅 콘솔 + 페어링 서버.

디스코드에서 /시작 을 치면 이 페이지 링크가 온다. 여기서 전부 끝난다:
  1. 디스코드 연동 (페어링 코드)
  2. 에이전트 CLI 계정 연결 (Claude / Codex)
  3. MCP 연결 (Notion / Figma / 직접 입력)

orca 패턴: 계정마다 격리된 홈을 만들고 그 안에서 벤더 CLI 자체의 로그인을 돌린다.
사용자가 하는 일은 브라우저에서 로그인하는 것뿐이고 자격증명은 이 PC를 떠나지 않는다.

SERVER_URL 이 곧 페어링을 받는 쪽이다. 봇이 다른 머신으로 가면 이 값만 바꾼다.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import webbrowser
from pathlib import Path

import urllib.error
import urllib.request

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import db

ROOT = Path(__file__).parent
AGENTS_DIR = ROOT / "data" / "agents"
ACCOUNTS = db.ACCOUNTS_PATH
PORT = int(os.getenv("LAUNCHER_PORT", "8787"))
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:%d" % PORT)

# 격리 홈을 주입할 환경변수와, 그 환경에서 돌릴 로그인/상태 명령
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

# URL 이 바뀌면 여기 한 줄만 고친다. 목록에 없는 건 '직접 추가' 로 붙인다.
MCP_CATALOG = {
    "notion": {"label": "Notion", "url": "https://mcp.notion.com/mcp"},
    "figma": {"label": "Figma", "url": "https://mcp.figma.com/mcp"},
}

jobs = {}


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
    else:
        job["lines"].append("[실패] 로그인이 완료되지 않았습니다.")
    job["done"] = True


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


def find_account(home):
    for a in load_accounts():
        if a["home"] == home:
            return a
    return None


# --- 라우트 -----------------------------------------------------------------

async def page(request):
    return HTMLResponse(
        PAGE.replace("__AGENTS__", json.dumps(
            {k: v["label"] for k, v in AGENTS.items()}, ensure_ascii=False))
        .replace("__MCP__", json.dumps(MCP_CATALOG, ensure_ascii=False))
        .replace("__CODE__", json.dumps(request.query_params.get("code", "")))
    )


async def leaderboard_page(request):
    return HTMLResponse(BOARD)


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
    return JSONResponse(
        jobs.get(request.path_params["job_id"], {"lines": ["없는 작업"], "done": True})
    )


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
    return JSONResponse({"ok": True, "user_name": got[1]})


async def api_discord_link(request):
    """클라이언트 쪽. 이 PC의 계정을 SERVER_URL 에 페어링한다."""
    body = await request.json()
    acct = find_account(body.get("home", ""))
    if not acct:
        return JSONResponse({"ok": False, "error": "먼저 에이전트 계정을 연결하세요."}, 400)
    status, out = await run_in_threadpool(
        post_json,
        SERVER_URL.rstrip("/") + "/api/pair",
        {"code": body.get("code", ""), "agent": acct["agent"], "identity": acct["identity"]},
    )
    return JSONResponse(out, status_code=status)


async def api_mcp_list(request):
    acct = find_account(request.query_params.get("home", ""))
    if not acct or not AGENTS.get(acct["agent"], {}).get("mcp"):
        return JSONResponse({"servers": [], "note": "이 에이전트는 MCP 관리를 지원하지 않습니다."})
    out = run_cli(acct["agent"], acct["home"], ["claude", "mcp", "list"]).stdout
    names = [
        m.group(1)
        for line in out.splitlines()
        if (m := re.match(r"\s*([\w-]+):", line))
    ]
    return JSONResponse({"servers": names, "raw": out.strip()})


async def api_mcp_add(request):
    body = await request.json()
    acct = find_account(body.get("home", ""))
    if not acct:
        return JSONResponse({"ok": False, "error": "계정을 찾을 수 없습니다."}, 400)
    name, url = body.get("name", "").strip(), body.get("url", "").strip()
    if not re.fullmatch(r"[\w-]{1,64}", name) or not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "이름 또는 URL 형식이 잘못됐습니다."}, 400)
    r = run_cli(acct["agent"], acct["home"],
                ["claude", "mcp", "add", "--transport", "http", name, url], timeout=90)
    ok = r.returncode == 0
    return JSONResponse({"ok": ok, "output": (r.stdout + r.stderr).strip()[-1500:]})


async def api_mcp_remove(request):
    body = await request.json()
    acct = find_account(body.get("home", ""))
    name = body.get("name", "").strip()
    if not acct or not re.fullmatch(r"[\w-]{1,64}", name):
        return JSONResponse({"ok": False, "error": "잘못된 요청입니다."}, 400)
    r = run_cli(acct["agent"], acct["home"], ["claude", "mcp", "remove", name])
    return JSONResponse({"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[-1500:]})


async def api_leaderboard(request):
    days = request.query_params.get("days", "7")
    since = None
    if days.isdigit():
        from datetime import datetime, timedelta

        since = (datetime.now(db.KST).date() - timedelta(days=int(days) - 1)).isoformat()
    conn = db.connect()
    try:
        rows = db.leaderboard(conn, since)
        channels = [dict(c) for c in db.list_channels(conn)]
    finally:
        conn.close()
    return JSONResponse({"rows": rows, "channels": len(channels), "since": since})


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8">
<title>세팅 콘솔</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{color-scheme:light dark;--line:#8883;--dim:#888}
 body{font:16px/1.6 system-ui,'Malgun Gothic',sans-serif;max-width:680px;margin:36px auto;padding:0 20px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:34px 0 8px;color:var(--dim);
    text-transform:uppercase;letter-spacing:.06em}
 p.sub{color:var(--dim);margin:0 0 8px}
 .card{border:1px solid var(--line);border-radius:12px;padding:16px;margin:10px 0;display:flex;
       align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
 .who{font-weight:600} .meta{color:var(--dim);font-size:13px}
 .ok{color:#059669;font-size:13px;font-weight:600}
 .warn{color:#d97706;font-size:13px;font-weight:600}
 button{font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#4f46e5;
        color:#fff;cursor:pointer} button:hover{background:#4338ca}
 button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line);
              padding:6px 12px;font-size:14px}
 input,select{font:inherit;padding:9px 12px;border-radius:9px;border:1px solid var(--line);
        background:transparent;color:inherit}
 input[type=text]{min-width:150px}
 #log{white-space:pre-wrap;background:#8881;border-radius:10px;padding:14px;margin-top:12px;
      font:13px ui-monospace,monospace;max-height:240px;overflow:auto;display:none}
 a.big{display:inline-block;margin-top:10px;padding:10px 18px;background:#059669;color:#fff;
       border-radius:10px;text-decoration:none}
 a.plain{color:#4f46e5}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
</style>
<h1>세팅 콘솔</h1>
<p class="sub">여기서 전부 끝납니다. 순서대로 하시면 됩니다.
 · <a class="plain" href="/leaderboard">리더보드</a></p>

<h2>1 · 에이전트 계정</h2>
<div id="accounts"></div>
<div id="add"></div>

<h2>2 · 디스코드 연동</h2>
<div class="card">
  <div><div class="who">연동 코드 입력</div>
  <div class="meta">디스코드에서 <b>/시작</b> 을 치면 나오는 6자리 코드</div></div>
  <div class="row">
    <input type="text" id="code" placeholder="ABC123" maxlength="6" autocomplete="off">
    <button onclick="pair()">연동</button>
  </div>
</div>
<div id="pairmsg" class="meta"></div>

<h2>3 · MCP 연결</h2>
<div class="card">
  <div><div class="who">대상 계정</div><div class="meta" id="mcpnote">—</div></div>
  <select id="mcpacct" onchange="loadMcp()"></select>
</div>
<div id="mcplist"></div>
<div class="card">
  <div><div class="who">직접 추가</div><div class="meta">목록에 없는 MCP 서버</div></div>
  <div class="row">
    <input type="text" id="mname" placeholder="이름" maxlength="64">
    <input type="text" id="murl" placeholder="https://...">
    <button onclick="addMcp(document.getElementById('mname').value,
                            document.getElementById('murl').value)">추가</button>
  </div>
</div>
<div id="log"></div>
<script>
const AG = __AGENTS__, CATALOG = __MCP__, PRESET = __CODE__;
const $ = id => document.getElementById(id);
let polling = null, ACCTS = [];

function card(html, ...els){ const d=document.createElement('div'); d.className='card';
  d.innerHTML=html; const w=document.createElement('div'); w.className='row';
  els.forEach(e=>w.appendChild(e)); d.appendChild(w); return d; }
function btn(text, fn, ghost){ const b=document.createElement('button');
  b.textContent=text; b.onclick=fn; if(ghost) b.className='ghost'; return b; }
function esc(s){ const d=document.createElement('div'); d.textContent=s??''; return d.innerHTML; }

async function refresh(){
  ACCTS = await (await fetch('/api/accounts')).json();
  const box=$('accounts'); box.textContent='';
  if(!ACCTS.length) box.appendChild(card('<div class="meta">등록된 계정이 없습니다. 아래에서 연결하세요.</div>'));
  for(const a of ACCTS){
    const state = a.linked ? '<span class="ok">디스코드 연동됨</span>'
                           : '<span class="warn">디스코드 미연동</span>';
    box.appendChild(card('<div><div class="who">'+esc(a.identity)+'</div>'+
      '<div class="meta">'+esc(AG[a.agent]||a.agent)+' · '+state+'</div></div>',
      btn('연결 해제', ()=>unlink(a.home), true)));
  }
  const add=$('add'); add.textContent='';
  for(const [k,v] of Object.entries(AG))
    add.appendChild(card('<div><div class="who">'+esc(v)+' 계정 추가</div>'+
      '<div class="meta">브라우저 로그인만 하면 됩니다</div></div>', btn('연결', ()=>link(k))));

  const sel=$('mcpacct'); sel.textContent='';
  const usable = ACCTS.filter(a=>a.mcp_capable);
  for(const a of usable){ const o=document.createElement('option');
    o.value=a.home; o.textContent=a.identity; sel.appendChild(o); }
  $('mcpnote').textContent = usable.length ? 'Claude 계정에 MCP를 붙입니다'
                                           : 'Claude 계정을 먼저 연결하세요';
  loadMcp();
}

async function link(agent){
  const log=$('log'); log.style.display='block'; log.textContent='로그인 창을 여는 중...';
  const {id} = await (await fetch('/api/link/'+agent,{method:'POST'})).json();
  clearInterval(polling); let shown=false;
  polling = setInterval(async ()=>{
    const j = await (await fetch('/api/job/'+id)).json();
    log.textContent = j.lines.join('\\n') || '진행 중...';
    if(j.url && !shown){ shown=true; const a=document.createElement('a');
      a.className='big'; a.href=j.url; a.target='_blank';
      a.textContent='브라우저가 안 열렸다면 여기를 누르세요';
      log.appendChild(document.createElement('br')); log.appendChild(a); }
    if(j.done){ clearInterval(polling);
      log.appendChild(document.createTextNode(
        j.ok ? '\\n\\n연결 완료: '+j.identity : '\\n\\n연결에 실패했습니다.'));
      refresh(); }
    log.scrollTop = log.scrollHeight;
  }, 700);
}

async function unlink(home){
  if(!confirm('이 계정 연결을 해제할까요?')) return;
  await fetch('/api/unlink',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({home})});
  refresh();
}

async function pair(){
  const code=$('code').value.trim(), msg=$('pairmsg');
  if(!ACCTS.length){ msg.innerHTML='<span class="warn">먼저 1번에서 에이전트 계정을 연결하세요.</span>'; return; }
  if(!code){ msg.innerHTML='<span class="warn">코드를 입력하세요.</span>'; return; }
  msg.textContent='연동 중...';
  const home = ($('mcpacct').value) || ACCTS[0].home;
  const r = await (await fetch('/api/discord-link',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({code, home})})).json();
  msg.innerHTML = r.ok ? '<span class="ok">'+esc(r.user_name)+' 님으로 연동됐습니다.</span>'
                       : '<span class="warn">'+esc(r.error||'연동 실패')+'</span>';
  if(r.ok){ $('code').value=''; refresh(); }
}

async function loadMcp(){
  const home=$('mcpacct').value, box=$('mcplist'); box.textContent='';
  if(!home){ return; }
  const r = await (await fetch('/api/mcp?home='+encodeURIComponent(home))).json();
  const have = new Set(r.servers||[]);
  for(const [k,v] of Object.entries(CATALOG)){
    const on = have.has(k);
    box.appendChild(card('<div><div class="who">'+esc(v.label)+
      (on?' <span class="ok">연결됨</span>':'')+'</div>'+
      '<div class="meta">'+esc(v.url)+'</div></div>',
      on ? btn('해제', ()=>removeMcp(k), true) : btn('연결', ()=>addMcp(k, v.url))));
  }
  for(const name of (r.servers||[]))
    if(!CATALOG[name] && name!=='discord-collector')
      box.appendChild(card('<div><div class="who">'+esc(name)+'</div>'+
        '<div class="meta">직접 추가함</div></div>', btn('해제', ()=>removeMcp(name), true)));
}

async function addMcp(name, url){
  const home=$('mcpacct').value, log=$('log');
  if(!home){ alert('Claude 계정을 먼저 연결하세요.'); return; }
  log.style.display='block'; log.textContent=name+' 연결 중...';
  const r = await (await fetch('/api/mcp/add',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({home,name,url})})).json();
  log.textContent = (r.output||'') + (r.ok ? '\\n\\n연결됨. 처음 쓸 때 브라우저에서 로그인하라고 나올 수 있습니다.'
                                           : '\\n\\n실패했습니다.');
  loadMcp();
}

async function removeMcp(name){
  const home=$('mcpacct').value, log=$('log');
  log.style.display='block'; log.textContent=name+' 해제 중...';
  const r = await (await fetch('/api/mcp/remove',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({home,name})})).json();
  log.textContent = r.output || (r.ok?'해제됨':'실패');
  loadMcp();
}

if(PRESET) $('code').value = PRESET;
refresh();
</script>"""


BOARD = """<!doctype html><html lang="ko"><meta charset="utf-8">
<title>리더보드</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{color-scheme:light dark;--line:#8883;--dim:#888}
 body{font:16px/1.6 system-ui,'Malgun Gothic',sans-serif;max-width:680px;margin:36px auto;padding:0 20px}
 h1{font-size:22px;margin:0 0 4px} p.sub{color:var(--dim);margin:0 0 20px}
 a.plain{color:#4f46e5}
 button{font:inherit;padding:8px 14px;border-radius:9px;border:1px solid var(--line);
        background:transparent;color:inherit;cursor:pointer;margin-right:6px}
 button.on{background:#4f46e5;color:#fff;border-color:#4f46e5}
 table{width:100%;border-collapse:collapse;margin-top:18px}
 th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}
 th{font-size:13px;color:var(--dim);font-weight:600}
 td.n{text-align:right;font-variant-numeric:tabular-nums}
 .rank{color:var(--dim);width:34px}
 .bar{height:6px;background:#4f46e5;border-radius:3px;margin-top:4px}
 .empty{color:var(--dim);padding:24px 0}
</style>
<h1>리더보드</h1>
<p class="sub">수집된 채널 기준 발언 집계 · <a class="plain" href="/">세팅 콘솔</a></p>
<div id="tabs"></div>
<div id="out"></div>
<script>
const $ = id => document.getElementById(id);
const PERIODS = [['7','최근 7일'],['30','최근 30일'],['all','전체']];
let cur = '7';
function esc(s){ const d=document.createElement('div'); d.textContent=s??''; return d.innerHTML; }

function tabs(){ const t=$('tabs'); t.textContent='';
  for(const [k,label] of PERIODS){ const b=document.createElement('button');
    b.textContent=label; if(k===cur) b.className='on';
    b.onclick=()=>{cur=k; tabs(); load();}; t.appendChild(b); } }

async function load(){
  const r = await (await fetch('/api/leaderboard?days='+cur)).json();
  const out=$('out');
  if(!r.rows.length){ out.innerHTML='<div class="empty">아직 수집된 대화가 없습니다. '+
    '디스코드에서 <b>/수집시작</b> 을 실행하세요.</div>'; return; }
  const max = r.rows[0].messages;
  out.innerHTML = '<table><thead><tr><th class="rank">#</th><th>이름</th>'+
    '<th class="n">메시지</th><th class="n">활동일</th><th class="n">채널</th></tr></thead><tbody>'+
    r.rows.map((x,i)=>'<tr><td class="rank">'+(i+1)+'</td><td>'+esc(x.author)+
      '<div class="bar" style="width:'+Math.max(2,Math.round(x.messages/max*100))+'%"></div></td>'+
      '<td class="n">'+x.messages+'</td><td class="n">'+x.days+'</td>'+
      '<td class="n">'+x.channels+'</td></tr>').join('')+'</tbody></table>';
}
tabs(); load();
</script>"""


app = Starlette(routes=[
    Route("/", page),
    Route("/leaderboard", leaderboard_page),
    Route("/api/accounts", api_accounts),
    Route("/api/link/{agent}", api_link, methods=["POST"]),
    Route("/api/job/{job_id}", api_job),
    Route("/api/unlink", api_unlink, methods=["POST"]),
    Route("/api/pair", api_pair, methods=["POST"]),
    Route("/api/discord-link", api_discord_link, methods=["POST"]),
    Route("/api/mcp", api_mcp_list),
    Route("/api/mcp/add", api_mcp_add, methods=["POST"]),
    Route("/api/mcp/remove", api_mcp_remove, methods=["POST"]),
    Route("/api/leaderboard", api_leaderboard),
])

if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
