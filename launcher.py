"""에이전트 CLI 계정 등록기.

orca 패턴: 계정마다 격리된 홈 디렉터리를 만들고 그 안에서 벤더 CLI 자체의
로그인을 돌린다. 사용자가 하는 일은 브라우저에서 로그인하는 것뿐이고,
자격증명은 이 PC를 떠나지 않는다.

실행: 등록.vbs 더블클릭 (또는 python launcher.py)
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

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).parent
AGENTS_DIR = ROOT / "data" / "agents"
ACCOUNTS = ROOT / "data" / "accounts.json"
PORT = 8787

# 격리 홈을 주입할 환경변수와, 그 환경에서 돌릴 로그인/상태 명령
AGENTS = {
    "claude": {
        "label": "Claude",
        "home_env": "CLAUDE_CONFIG_DIR",
        "login": ["claude", "auth", "login"],
        "status": ["claude", "auth", "status"],
    },
    "codex": {
        "label": "Codex",
        "home_env": "CODEX_HOME",
        "login": ["codex", "login"],
        "status": ["codex", "login", "status"],
    },
}

jobs = {}


def load_accounts():
    return json.loads(ACCOUNTS.read_text("utf-8")) if ACCOUNTS.exists() else []


def save_accounts(accts):
    ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS.write_text(json.dumps(accts, ensure_ascii=False, indent=2), "utf-8")


def identity(agent, home):
    """CLI 자신에게 물어본다 — 자격증명 파일 형식을 추측하지 않는다."""
    spec = AGENTS[agent]
    env = {**os.environ, spec["home_env"]: str(home)}
    try:
        out = subprocess.run(
            spec["status"], env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            shell=os.name == "nt",
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
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
        env = {**os.environ, spec["home_env"]: str(home)}
        p = subprocess.Popen(
            spec["login"], env=env, stdout=subprocess.PIPE,
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


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8">
<title>에이전트 계정 등록</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{color-scheme:light dark}
 body{font:16px/1.6 system-ui,'Malgun Gothic',sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
 h1{font-size:22px;margin:0 0 4px} p.sub{color:#888;margin:0 0 28px}
 .card{border:1px solid #8883;border-radius:12px;padding:16px;margin:12px 0;display:flex;
       align-items:center;justify-content:space-between;gap:12px}
 .who{font-weight:600} .meta{color:#888;font-size:13px}
 button{font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#4f46e5;
        color:#fff;cursor:pointer} button:hover{background:#4338ca}
 button.ghost{background:transparent;color:#888;border:1px solid #8884;padding:6px 12px;font-size:14px}
 #log{white-space:pre-wrap;background:#8881;border-radius:10px;padding:14px;margin-top:16px;
      font:13px ui-monospace,monospace;max-height:240px;overflow:auto;display:none}
 a.big{display:inline-block;margin-top:10px;padding:10px 18px;background:#059669;color:#fff;
       border-radius:10px;text-decoration:none}
</style>
<h1>에이전트 계정 등록</h1>
<p class="sub">버튼을 누르면 브라우저에서 로그인 창이 열립니다. 로그인만 하면 끝입니다.</p>
<div id="accounts"></div>
<div id="add"></div>
<div id="log"></div>
<script>
const AG = __AGENTS__;
const $ = id => document.getElementById(id);
let polling = null;

function card(inner, btn){ const d=document.createElement('div'); d.className='card';
  d.innerHTML=inner; if(btn) d.appendChild(btn); return d; }

async function refresh(){
  const accts = await (await fetch('/api/accounts')).json();
  const box = $('accounts'); box.textContent='';
  if(!accts.length) box.appendChild(card('<div class="meta">등록된 계정이 없습니다.</div>'));
  for(const a of accts){
    const b=document.createElement('button'); b.className='ghost'; b.textContent='연결 해제';
    b.onclick=()=>unlink(a.home);
    box.appendChild(card('<div><div class="who"></div><div class="meta"></div></div>', b));
    const d=box.lastChild; d.querySelector('.who').textContent=a.identity;
    d.querySelector('.meta').textContent=AG[a.agent]||a.agent;
  }
  const add = $('add'); add.textContent='';
  for(const [k,v] of Object.entries(AG)){
    const b=document.createElement('button'); b.textContent='연결'; b.onclick=()=>link(k);
    add.appendChild(card('<div><div class="who">'+v+' 계정 추가</div>'+
      '<div class="meta">브라우저 로그인만 하면 됩니다</div></div>', b));
  }
}

async function link(agent){
  const log = $('log'); log.style.display='block'; log.textContent='로그인 창을 여는 중...';
  const {id} = await (await fetch('/api/link/'+agent,{method:'POST'})).json();
  clearInterval(polling);
  let linked = false;
  polling = setInterval(async ()=>{
    const j = await (await fetch('/api/job/'+id)).json();
    log.textContent = j.lines.join('\\n') || '진행 중...';
    if(j.url && !linked){ linked=true;
      const a=document.createElement('a'); a.className='big'; a.href=j.url; a.target='_blank';
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
    body:JSON.stringify({home:home})});
  refresh();
}
refresh();
</script>"""


async def page(request):
    labels = json.dumps({k: v["label"] for k, v in AGENTS.items()}, ensure_ascii=False)
    return HTMLResponse(PAGE.replace("__AGENTS__", labels))


async def api_accounts(request):
    return JSONResponse(load_accounts())


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


app = Starlette(routes=[
    Route("/", page),
    Route("/api/accounts", api_accounts),
    Route("/api/link/{agent}", api_link, methods=["POST"]),
    Route("/api/job/{job_id}", api_job),
    Route("/api/unlink", api_unlink, methods=["POST"]),
])

if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
