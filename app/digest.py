"""일일 결산 — 그날 대화를 정리해 디스코드에 게시할 본문을 만든다.

요약은 콘솔에 연동된 CLI(claude / codex)를 비대화로 불러서 한다.
API 키를 쓰지 않는다 — 그 계정의 구독으로 돈다.
연동된 CLI 가 없으면 통계 결산으로 자동 강등한다.
"""

import os
import shutil
import subprocess
import tempfile
from collections import Counter

import db
import remote

# 고를 수 있는 모델. 연동한 CLI 가 그 계정의 구독으로 돌리므로 별도 과금이 없다.
# price/month 는 API 로 직접 부를 때의 참고값일 뿐이다.
MODELS = [
    {"id": "claude-opus-5", "label": "Opus 5", "provider": "anthropic",
     "price": "$5 / $25", "month": "약 6,000원",
     "note": "기본. 주제 묶기와 아이디어 판정이 제일 정확"},
    {"id": "claude-fable-5", "label": "Fable 5", "provider": "anthropic",
     "price": "$10 / $50", "month": "약 12,000원",
     "note": "가장 강력. 결산엔 과할 수 있음"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5", "provider": "anthropic",
     "price": "$2 / $10", "month": "약 2,400원",
     "note": "일상 결산엔 충분"},
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5", "provider": "anthropic",
     "price": "$1 / $5", "month": "약 1,200원",
     "note": "가장 쌈. 긴 대화에선 묶음 품질이 떨어짐"},
    {"id": "gpt-5.6-terra", "label": "Codex (Terra)", "provider": "openai",
     "price": "—", "month": "—",
     "note": "연동한 Codex 계정으로 돈다"},
]
DEFAULT_MODEL = "claude-opus-5"
MODEL_IDS = {m["id"] for m in MODELS}


def model():
    """콘솔 설정 > DIGEST_MODEL 환경변수 > 기본값."""
    conn = db.connect()
    try:
        chosen = db.get_setting(conn, "digest_model")
    except Exception:  # noqa: BLE001 — 설정 못 읽어도 결산은 돌아야 한다
        chosen = None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return chosen or os.getenv("DIGEST_MODEL") or DEFAULT_MODEL


MAX_CHARS = int(os.getenv("DIGEST_MAX_CHARS", "60000"))   # 하루치 원문 상한
LINK = "https://discord.com/channels/{g}/{c}/{m}"
SYSTEM = "너는 팀의 대화를 정리하는 서기다. 없는 말을 만들지 않는다."

PROMPT = """아래는 디스코드 한 채널의 하루치 대화 원문이다. 팀이 다음날 아침에
읽을 일일 결산을 쓴다.

규칙:
- 주제 단위로 묶는다. 시간이 아니라 화제로 자른다.
- 결론이 안 난 주제는 "미결"로 명시한다. 억지로 결론을 지어내지 않는다.
- 잡담·인사·일정 조율은 주제로 세우지 않는다. 필요하면 마지막에 한 줄.
- 원문에 없는 내용을 채워 넣지 않는다. 대화가 짧으면 결산도 짧게 둔다.
- 사업화 가능한 아이디어는 문제·대상 사용자·왜 지금 이 셋이 모두 대화에서
  식별될 때만 적는다. 하나라도 없으면 적지 않는다.

형식 (디스코드 마크다운, 2000자 이내):
**오늘의 주제**
- 주제명 — 무엇을 이야기했고 어떻게 결론났는지 한두 문장

**결정된 것** (없으면 이 항목 생략)
**미결** (없으면 생략)
**아이디어** (셋을 다 갖춘 것만, 없으면 생략)

대화 원문:
"""


def _fmt(rows):
    return "\n".join(
        "%s %s: %s" % (r["at"][11:16], r["author"], r["content"]) for r in rows
    )


def stats(rows, channel, date):
    """키가 없을 때의 결산. 사실만 적는다 — 지어내는 것보다 낫다."""
    if not rows:
        return None
    who = Counter(r["author"] for r in rows)
    hours = Counter(r["at"][11:13] for r in rows)
    peak = hours.most_common(1)[0]
    first = rows[0]
    head = LINK.format(g=first["guild_id"], c=first["channel_id"], m=first["id"])
    lines = [
        "**%s · #%s 일일 결산**" % (date, channel),
        "",
        "메시지 %d건 · 참여 %d명 · 가장 활발했던 시간 %s시 (%d건)"
        % (len(rows), len(who), peak[0], peak[1]),
        "",
        "**많이 말한 사람**",
    ]
    lines += ["· %s — %d건" % (n, c) for n, c in who.most_common(5)]
    lines += ["", "[대화 처음으로 가기](%s)" % head, "",
              "_연동된 CLI 가 없어 통계만 냅니다. 콘솔의 '연결' 탭에서 "
              "Claude 또는 Codex 를 붙이면 주제별 정리가 붙습니다._"]
    return "\n".join(lines)


def spec(mid=None):
    mid = mid or model()
    return next((m for m in MODELS if m["id"] == mid),
                {"id": mid, "provider": "anthropic"})


def key_present(mid=None):
    """이름은 남겨두지만 이제는 '연동된 CLI 가 있나' 를 뜻한다. API 키는 안 쓴다."""
    return cli_ready()


# 연동된 CLI 로 부른다. API 키가 없어도 그 계정의 구독으로 돈다.
# 수집된 디스코드 메시지는 신뢰할 수 없는 입력이라 도구를 전부 막고,
# 빈 임시 디렉터리에서 돌린다 — 인젝션이 통해도 만질 게 없다.
TOOLS_OFF = ["Read", "Write", "Edit", "NotebookEdit", "Bash", "Glob", "Grep",
             "WebFetch", "WebSearch", "Task", "TodoWrite"]
HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
CLI_TIMEOUT = int(os.getenv("CLI_TIMEOUT", "600"))


def linked_cli():
    """콘솔에 등록된 CLI 계정. 고른 모델의 제공자와 맞는 걸 우선 고른다."""
    accts = db.local_accounts()
    if not accts:
        return None
    want = spec()["provider"]
    agent = "codex" if want == "openai" else "claude"
    return next((a for a in accts if a.get("agent") == agent), accts[0])


def cli_ready():
    return linked_cli() is not None


def _cli(system, prompt, timeout=None):
    """등록된 CLI 를 비대화로 부른다. 실패하면 RuntimeError.

    프롬프트는 stdin 으로 넘긴다 — 명령행 인자로 주면 Windows 의 ~32KB 한계에
    걸리고, 줄바꿈이 섞이면 셸에서 잘린다. 그래서 shell 도 쓰지 않는다.
    """
    acct = linked_cli()
    if not acct:
        raise RuntimeError("연동된 CLI 계정이 없습니다. 콘솔의 '연결' 탭에서 붙이세요.")
    agent, home = acct["agent"], acct["home"]
    mid = model()
    if agent == "claude":
        argv = ["claude", "-p", "--model", mid,
                "--append-system-prompt", system, "--disallowed-tools"] + TOOLS_OFF
        stdin = prompt
    else:
        argv = ["codex", "exec", "--model", mid]
        stdin = system + "\n\n" + prompt      # codex 는 stdin 을 지시로 읽는다
    exe = shutil.which(argv[0])
    if not exe:
        raise RuntimeError(
            "%s CLI 를 찾을 수 없습니다. 이 서버에 설치돼 있어야 합니다." % argv[0])
    argv[0] = exe
    env = {**os.environ, HOME_ENV.get(agent, "CLAUDE_CONFIG_DIR"): str(home)}
    with tempfile.TemporaryDirectory(prefix="abst-cli-") as cwd:
        try:
            r = subprocess.run(argv, input=stdin, env=env, cwd=cwd,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout or CLI_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError("CLI 응답이 너무 오래 걸려 중단했습니다.")
    if r.returncode != 0:
        raise RuntimeError("CLI 실행 실패: %s" % (r.stderr or r.stdout or "")[-300:])
    return (r.stdout or "").strip()


def summarize(rows, channel, date):
    """LLM 결산. CLI 가 없거나 실패하면 None (호출자가 통계로 강등)."""
    if not cli_ready():
        return None

    body = _fmt(rows)
    if len(body) > MAX_CHARS:
        # 앞뒤를 남기고 가운데를 줄인다 — 하루의 시작과 결론이 결산에 제일 중요하다
        half = MAX_CHARS // 2
        body = body[:half] + "\n\n…(중략)…\n\n" + body[-half:]

    try:
        text = _cli(SYSTEM, PROMPT + body)
    except Exception:  # noqa: BLE001 — 결산 하나 때문에 봇이 죽으면 안 된다
        return None

    if not text:
        return None
    first = rows[0]
    head = LINK.format(g=first["guild_id"], c=first["channel_id"], m=first["id"])
    return "**%s · #%s 일일 결산** · %d건\n\n%s\n\n[대화 처음으로 가기](%s)" % (
        date, channel, len(rows), text, head)


def build(channel_id, channel_name, date):
    """그날 결산 본문. 대화가 없으면 None."""
    rows = remote.conversation(channel_id, date)
    if not rows:
        return None
    return summarize(rows, channel_name, date) or stats(rows, channel_name, date)


def chunks(text, limit=1900):
    """디스코드 2000자 제한. 줄 단위로 자른다."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


def yesterday_kst():
    from datetime import datetime, timedelta

    return (datetime.now(db.KST).date() - timedelta(days=1)).isoformat()


# --- 묻기 (콘솔 채팅) -------------------------------------------------------

ASK_SYSTEM = """너는 팀의 디스코드 대화를 읽고 답하는 조수다.

- 아래 '자료'에 있는 것만 근거로 답한다. 없는 내용을 지어내지 않는다.
- 자료에 답이 없으면 "그 기간 대화에는 없습니다" 라고 말하고,
  어떤 채널·날짜를 보면 될지 제안한다.
- 인용할 때는 줄 끝의 ⟨메시지ID⟩ 를 함께 적어 근거를 남긴다.
- 사업 아이디어를 물으면 문제·대상 사용자·왜 지금 셋이 모두 대화에서 식별될 때만
  아이디어로 세운다. 하나라도 없으면 "근거 부족" 으로 남긴다.
- 한국어로, 짧고 사실 위주로 답한다."""

ASK_MAX_CHARS = int(os.getenv("ASK_MAX_CHARS", "80000"))


def context_block(rows, channel, span):
    """모델에게 물려줄 자료. 대화록 그대로 준다."""
    body = "\n".join(
        "%s %s: %s  ⟨%s⟩" % (r["at"][11:16], r["author"], r["content"], r["id"])
        for r in rows
    )
    if len(body) > ASK_MAX_CHARS:
        half = ASK_MAX_CHARS // 2
        body = body[:half] + "\n\n…(중략)…\n\n" + body[-half:]
    return "자료 — #%s · %s (KST) · %d건\n\n%s" % (channel, span, len(rows), body)


def ask(history, context):
    """대화 기록 + 자료로 한 번 묻는다. (텍스트, 오류) 를 돌려준다.

    CLI 는 턴을 기억하지 않으므로 지난 대화를 프롬프트에 같이 적어 보낸다.
    """
    if not cli_ready():
        return None, "연동된 CLI 계정이 없습니다. 콘솔의 '연결' 탭에서 붙이세요."
    turns = ["%s: %s" % ("나" if m["role"] == "user" else "너", m["content"])
             for m in history[:-1]]
    prompt = context
    if turns:
        prompt += "\n\n지난 대화:\n" + "\n".join(turns)
    prompt += "\n\n질문: " + history[-1]["content"]
    try:
        text = _cli(ASK_SYSTEM, prompt)
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    return (text or None), (None if text else "빈 응답을 받았습니다.")
