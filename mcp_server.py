"""수집된 대화를 에이전트에 노출하는 MCP 서버.

읽기 전용이다. 삭제된 메시지와 수집중단 사용자는 db 계층에서 이미 빠진다.
MCP가 툴을 워커 스레드에서 실행하고 SQLite 커넥션은 스레드에 묶이므로,
커넥션은 호출마다 연다 (읽기 전용 단발 조회라 비용이 없다).
"""

import json
from contextlib import contextmanager
from datetime import datetime

from mcp.server.mcpserver import MCPServer

import db

mcp = MCPServer("discord-collector")

LINK = "https://discord.com/channels/{guild}/{channel}/{msg}"


@contextmanager
def _db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _kst(iso):
    """UTC ISO -> 'MM-DD HH:MM' KST."""
    return datetime.fromisoformat(iso).astimezone(db.KST).strftime("%m-%d %H:%M")


def _transcript(rows):
    """행 목록 -> 사람이 읽는 대화록. 근거 인용을 위해 메시지 ID를 남긴다."""
    out, threads = [], {}
    for r in rows:
        prefix = ""
        if r["thread_id"]:
            n = threads.setdefault(r["thread_id"], len(threads) + 1)
            prefix += f"[스레드{n}] "
        if r["reply_to_id"]:
            prefix += f"[→{r['reply_to_id']}] "
        line = f"{_kst(r['created_at'])} {r['author_name']}: {prefix}{r['content']}"
        if r["attachments"]:
            line += f"  (첨부 {len(json.loads(r['attachments']))}개)"
        out.append(f"{line}  ⟨{r['id']}⟩")
    return "\n".join(out)


@mcp.tool()
def list_channels() -> str:
    """수집 중인 채널과 각 채널의 메시지 수, 마지막 수집 시각을 반환한다."""
    with _db() as conn:
        rows = db.list_channels(conn)
    if not rows:
        return "수집 중인 채널이 없습니다. 디스코드에서 /수집시작 을 실행하세요."
    return "\n".join(
        f"#{r['channel_name']} (id={r['channel_id']}) — {r['message_count']}건"
        + (f", 최근 {_kst(r['last_message_at'])} KST" if r["last_message_at"] else ", 비어있음")
        for r in rows
    )


@mcp.tool()
def get_conversation(channel: str, date: str) -> str:
    """하루치 대화 원문을 시간순으로 반환한다.

    Args:
        channel: 채널 이름(예: '아이디어') 또는 채널 ID
        date: KST 기준 날짜, 'YYYY-MM-DD'

    각 줄 끝의 ⟨메시지ID⟩ 와 헤더의 링크 틀로 원문 링크를 만들 수 있다.
    """
    with _db() as conn:
        rows = db.get_conversation(conn, channel, date)
    if not rows:
        return f"{date} {channel} 에 대화가 없습니다."
    tmpl = LINK.format(guild=rows[0]["guild_id"], channel=rows[0]["channel_id"], msg="{메시지ID}")
    return (
        f"# {channel} · {date} (KST) · {len(rows)}건\n원문 링크 틀: {tmpl}\n\n"
        + _transcript(rows)
    )


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
    with _db() as conn:
        rows = db.search_messages(
            conn, query, channel or None, since or None, until or None, limit
        )
    if not rows:
        return f"'{query}' 검색 결과 없음"
    return f"# '{query}' {len(rows)}건\n\n" + "\n".join(
        f"{_kst(r['created_at'])} #{r['channel_name']} {r['author_name']}: {r['content']}  ⟨{r['id']}⟩"
        for r in rows
    )


@mcp.tool()
def recent_days(channel: str = "", days: int = 7) -> str:
    """최근 며칠간 KST 날짜별 메시지 수. 어느 날짜를 정리할지 고를 때 먼저 부른다."""
    with _db() as conn:
        rows = db.day_counts(conn, channel or None, days)
    return "\n".join(f"{d}  {n}건" for d, n in rows)


if __name__ == "__main__":
    mcp.run()
