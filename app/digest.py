"""일일 결산 — 그날 대화를 정리해 디스코드에 게시할 본문을 만든다.

자동 결산은 특정 사용자의 Claude 를 쓸 수 없다 (그 사람이 앱을 꺼놨을 수 있다).
그래서 여기만 서버 키를 쓴다. 개인 조회·정리는 여전히 각자 CLI 다.

고른 모델의 프로바이더 키(ANTHROPIC_API_KEY / OPENAI_API_KEY)가 없으면
통계 결산으로 자동 강등한다 — 키 없이도 돌아간다.
"""

import os
from collections import Counter

import db
import remote

# 고를 수 있는 모델. 가격은 100만 토큰당 (입력/출력).
# 하루 500건 ≈ 입력 20K + 출력 1.5K 기준의 월 비용을 같이 적어둔다.
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
     "note": "codex CLI 가 쓰는 모델. OPENAI_API_KEY 로 동작"},
]
PROVIDER_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
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
    need = PROVIDER_KEY.get(spec()["provider"], "ANTHROPIC_API_KEY")
    lines += ["", "[대화 처음으로 가기](%s)" % head, "",
              "_요약 키가 없어 통계만 냅니다. `%s` 를 넣으면 주제별 정리가 붙습니다._" % need]
    return "\n".join(lines)


def spec(mid=None):
    mid = mid or model()
    return next((m for m in MODELS if m["id"] == mid),
                {"id": mid, "provider": "anthropic"})


def key_present(mid=None):
    return bool(os.getenv(PROVIDER_KEY.get(spec(mid)["provider"], "ANTHROPIC_API_KEY")))


def _anthropic(mid, body):
    import anthropic

    resp = anthropic.Anthropic().messages.create(
        model=mid,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=SYSTEM,
        messages=[{"role": "user", "content": PROMPT + body}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _openai(mid, body):
    import openai

    resp = openai.OpenAI().responses.create(
        model=mid,
        instructions=SYSTEM,
        input=PROMPT + body,
        reasoning={"effort": "medium"},
        max_output_tokens=8000,
    )
    return (getattr(resp, "output_text", "") or "").strip()


def summarize(rows, channel, date):
    """LLM 결산. 키가 없거나 호출이 실패하면 None 을 돌려준다 (호출자가 통계로 강등)."""
    mid = model()
    if not key_present(mid):
        return None

    body = _fmt(rows)
    if len(body) > MAX_CHARS:
        # 앞뒤를 남기고 가운데를 줄인다 — 하루의 시작과 결론이 결산에 제일 중요하다
        half = MAX_CHARS // 2
        body = body[:half] + "\n\n…(중략)…\n\n" + body[-half:]

    try:
        fn = _openai if spec(mid)["provider"] == "openai" else _anthropic
        text = fn(mid, body)
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
