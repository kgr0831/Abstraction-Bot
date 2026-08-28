"""수집된 대화를 에이전트에 노출하는 MCP 서버.

읽기 전용이다. 삭제된 메시지와 수집중단 사용자는 서버 쪽에서 이미 빠진다.
데이터가 로컬 SQLite 인지 원격 서버인지는 remote 모듈이 판단한다.
"""

from datetime import datetime

from mcp.server.mcpserver import MCPServer

import db
import remote

mcp = MCPServer("discord-collector")

LINK = "https://discord.com/channels/{guild}/{channel}/{msg}"


def _gate():
    """본인 CLI 가 연동돼 있어야 조회를 허용한다. 통과면 None, 아니면 안내 문구."""
    mine = {a.get("identity") for a in db.local_accounts() if a.get("identity")}
    if not mine:
        return (
            "이 PC에 등록된 CLI 계정이 없습니다.\n"
            "콘솔(%s)에서 Claude 또는 Codex를 연결하세요." % remote.SELF
        )
    if remote.is_remote():
        # 원격이면 페어링 때 받은 토큰이 있어야 서버가 응답한다
        if not remote.token():
            return ("디스코드 연동이 안 돼 있습니다.\n"
                    "디스코드에서 `/시작` 을 실행하고 콘솔에서 코드를 입력하세요.")
        return None
    conn = db.connect()
    try:
        linked = db.linked_identities(conn)
    finally:
        conn.close()
    if not (mine & linked):
        return (
            "등록된 CLI 계정이 디스코드에 연동되지 않았습니다.\n"
            "디스코드에서 `/시작` 을 실행하고, 나온 코드를 콘솔의 '디스코드 연동' 칸에 "
            "입력하세요.\n(이 PC의 계정: %s)" % ", ".join(sorted(mine))
        )
    return None


def _kst(iso):
    """to_json 이 이미 KST ISO 로 준다 — 'MM-DD HH:MM' 만 뽑는다."""
    return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")


def _transcript(rows):
    """행 목록 -> 사람이 읽는 대화록. 근거 인용을 위해 메시지 ID를 남긴다."""
    out, threads = [], {}
    for r in rows:
        prefix = ""
        if r.get("thread"):
            n = threads.setdefault(r["thread"], len(threads) + 1)
            prefix += f"[스레드{n}] "
        if r.get("reply_to"):
            prefix += f"[→{r['reply_to']}] "
        line = f"{_kst(r['at'])} {r['author']}: {prefix}{r['content']}"
        if r.get("files"):
            line += f"  (첨부 {r['files']}개)"
        out.append(f"{line}  ⟨{r['id']}⟩")
    return "\n".join(out)


@mcp.tool()
def list_channels() -> str:
    """수집 중인 채널과 각 채널의 메시지 수, 마지막 수집 시각을 반환한다."""
    if (blocked := _gate()):
        return blocked
    rows = remote.channels()
    if not rows:
        return "수집 중인 채널이 없습니다. 콘솔의 '수집' 탭에서 채널을 켜세요."
    out = []
    for r in rows:
        line = f"#{r['channel_name']} (id={r['channel_id']}) — {r['message_count']}건"
        if r.get("last_message_at"):
            line += ", 최근 " + datetime.fromisoformat(
                r["last_message_at"]).astimezone(db.KST).strftime("%m-%d %H:%M") + " KST"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def get_conversation(channel: str, date: str, until: str = "") -> str:
    """대화 원문을 시간순으로 반환한다. 하루치 또는 날짜 범위.

    Args:
        channel: 채널 이름(예: '아이디어') 또는 채널 ID
        date: KST 시작 날짜, 'YYYY-MM-DD'
        until: KST 끝 날짜(포함). 비우면 date 하루치.
               주간 정리는 date='2026-08-20', until='2026-08-26' 처럼 부른다.

    각 줄 끝의 ⟨메시지ID⟩ 와 헤더의 링크 틀로 원문 링크를 만들 수 있다.
    """
    for d in (date, until):
        if d and not db.valid_date(d):
            return f"날짜 형식이 잘못됐습니다: '{d}' (YYYY-MM-DD 여야 함)"
    if until and until < date:
        return f"끝 날짜({until})가 시작 날짜({date})보다 앞섭니다."
    if (blocked := _gate()):
        return blocked
    rows = remote.conversation(channel, date, until or None)
    span = f"{date}~{until}" if until else date
    if not rows:
        return f"{span} {channel} 에 대화가 없습니다."
    tmpl = LINK.format(guild=rows[0]["guild_id"], channel=rows[0]["channel_id"], msg="{메시지ID}")
    return (f"# {channel} · {span} (KST) · {len(rows)}건\n원문 링크 틀: {tmpl}\n\n"
            + _transcript(rows))


@mcp.tool()
def search_messages(
    query: str, channel: str = "", since: str = "", until: str = "", limit: int = 200
) -> str:
    """메시지 본문을 부분일치로 검색한다 (최신순).

    Args:
        query: 검색어
        channel: 채널 이름 또는 ID. 비우면 전체
        since: KST 시작 날짜 'YYYY-MM-DD' (포함)
        until: KST 종료 날짜 'YYYY-MM-DD' (포함)
        limit: 최대 건수, 기본 200
    """
    if (blocked := _gate()):
        return blocked
    rows = remote.search(query, channel or None, since or None, until or None, limit)
    if not rows:
        return f"'{query}' 검색 결과 없음"
    return f"# '{query}' {len(rows)}건\n\n" + "\n".join(
        f"{_kst(r['at'])} #{r['channel']} {r['author']}: {r['content']}  ⟨{r['id']}⟩"
        for r in rows
    )


@mcp.tool()
def recent_days(channel: str = "", days: int = 7) -> str:
    """최근 며칠간 KST 날짜별 메시지 수. 어느 날짜를 정리할지 고를 때 먼저 부른다."""
    if (blocked := _gate()):
        return blocked
    return "\n".join(f"{d}  {n}건" for d, n in remote.days(channel or None, days))


if __name__ == "__main__":
    mcp.run()
