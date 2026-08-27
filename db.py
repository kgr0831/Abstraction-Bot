"""SQLite 저장소 — 스키마와 공용 쿼리.

봇(쓰기)과 MCP 서버(읽기)가 별도 프로세스라 WAL 모드로 연다.
시각은 UTC로 저장하고, 조회는 KST 날짜로 받는다 (팀이 KST로 말하므로).
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("COLLECTOR_DB") or Path(__file__).parent / "data" / "messages.db")
KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id           TEXT PRIMARY KEY,
  guild_id     TEXT NOT NULL,
  channel_id   TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  author_id    TEXT NOT NULL,
  author_name  TEXT NOT NULL,
  content      TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  edited_at    TEXT,
  reply_to_id  TEXT,
  thread_id    TEXT,
  attachments  TEXT,
  deleted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_ch_time ON messages(channel_id, created_at);

CREATE TABLE IF NOT EXISTS channels (
  channel_id   TEXT PRIMARY KEY,
  guild_id     TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  added_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS optouts (
  user_id  TEXT PRIMARY KEY,
  opted_at TEXT NOT NULL
);
"""


def connect(path=DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def kst_day_range(date_str):
    """'2026-08-27'(KST) -> UTC ISO 경계 [start, end)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    return d.astimezone(timezone.utc).isoformat(), (d + timedelta(days=1)).astimezone(
        timezone.utc
    ).isoformat()


# --- 화이트리스트 -----------------------------------------------------------

def add_channel(conn, channel_id, guild_id, channel_name):
    conn.execute(
        "INSERT OR REPLACE INTO channels VALUES (?,?,?,?)",
        (str(channel_id), str(guild_id), channel_name, now()),
    )


def remove_channel(conn, channel_id):
    cur = conn.execute("DELETE FROM channels WHERE channel_id=?", (str(channel_id),))
    return cur.rowcount > 0


def is_collected(conn, channel_id):
    return (
        conn.execute(
            "SELECT 1 FROM channels WHERE channel_id=?", (str(channel_id),)
        ).fetchone()
        is not None
    )


def list_channels(conn):
    return conn.execute(
        """SELECT c.channel_id, c.channel_name, c.added_at,
                  (SELECT COUNT(*) FROM messages m
                    WHERE m.channel_id=c.channel_id AND m.deleted=0) AS message_count,
                  (SELECT MAX(created_at) FROM messages m
                    WHERE m.channel_id=c.channel_id AND m.deleted=0) AS last_message_at
             FROM channels c ORDER BY c.channel_name"""
    ).fetchall()


# --- 옵트아웃 ---------------------------------------------------------------

def opt_out(conn, user_id):
    """수집 거부 등록. 이미 쌓인 메시지도 삭제 플래그를 세운다."""
    conn.execute(
        "INSERT OR REPLACE INTO optouts VALUES (?,?)", (str(user_id), now())
    )
    cur = conn.execute(
        "UPDATE messages SET deleted=1 WHERE author_id=?", (str(user_id),)
    )
    return cur.rowcount


def opt_in(conn, user_id):
    cur = conn.execute("DELETE FROM optouts WHERE user_id=?", (str(user_id),))
    return cur.rowcount > 0


def is_opted_out(conn, user_id):
    return (
        conn.execute(
            "SELECT 1 FROM optouts WHERE user_id=?", (str(user_id),)
        ).fetchone()
        is not None
    )


# --- 메시지 -----------------------------------------------------------------

def upsert_message(conn, msg):
    """discord.Message -> 행 하나. 편집 이력은 남기지 않고 덮어쓴다.

    스레드 메시지는 부모 채널 소속으로 저장한다 — 하루치 요약에 스레드 논의가
    같이 딸려와야 맥락이 산다.
    """
    ch = msg.channel
    in_thread = ch.type.name.endswith("thread")
    parent = getattr(ch, "parent", None) if in_thread else ch
    home = parent or ch
    conn.execute(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
        "COALESCE((SELECT deleted FROM messages WHERE id=?),0))",
        (
            str(msg.id),
            str(msg.guild.id) if msg.guild else "",
            str(home.id),
            getattr(home, "name", "") or "",
            str(msg.author.id),
            msg.author.display_name,
            msg.content,
            msg.created_at.isoformat(),
            msg.edited_at.isoformat() if msg.edited_at else None,
            str(msg.reference.message_id) if msg.reference else None,
            str(ch.id) if in_thread else None,
            json.dumps([a.url for a in msg.attachments], ensure_ascii=False)
            if msg.attachments
            else None,
            str(msg.id),
        ),
    )


def update_content(conn, message_id, content, edited_at):
    conn.execute(
        "UPDATE messages SET content=?, edited_at=? WHERE id=?",
        (content, edited_at, str(message_id)),
    )


def mark_deleted(conn, message_id):
    conn.execute("UPDATE messages SET deleted=1 WHERE id=?", (str(message_id),))


# --- 조회 (MCP 서버가 쓰는 것) ----------------------------------------------

_VISIBLE = """
  FROM messages m
 WHERE m.deleted=0
   AND m.author_id NOT IN (SELECT user_id FROM optouts)
"""


def get_conversation(conn, channel, date):
    start, end = kst_day_range(date)
    return conn.execute(
        f"""SELECT m.id, m.guild_id, m.channel_id, m.channel_name, m.author_name,
                   m.content, m.created_at, m.reply_to_id, m.thread_id, m.attachments
            {_VISIBLE}
              AND (m.channel_id=? OR m.channel_name=?)
              AND m.created_at >= ? AND m.created_at < ?
            ORDER BY m.created_at""",
        (str(channel), str(channel), start, end),
    ).fetchall()


def search_messages(conn, query, channel=None, since=None, until=None, limit=200):
    # ponytail: LIKE 전문검색. 수만 건 넘어 느려지면 FTS5 trigram 토크나이저로 교체
    #           (한국어는 unicode61 말고 trigram이어야 부분일치가 됨)
    sql = f"""SELECT m.id, m.guild_id, m.channel_id, m.channel_name, m.author_name,
                     m.content, m.created_at
              {_VISIBLE} AND m.content LIKE ?"""
    params = [f"%{query}%"]
    if channel:
        sql += " AND (m.channel_id=? OR m.channel_name=?)"
        params += [str(channel), str(channel)]
    if since:
        sql += " AND m.created_at >= ?"
        params.append(kst_day_range(since)[0])
    if until:
        sql += " AND m.created_at < ?"
        params.append(kst_day_range(until)[1])
    sql += " ORDER BY m.created_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def stats(conn):
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MAX(m.created_at) AS last {_VISIBLE}"
    ).fetchone()
    return {
        "messages": row["n"],
        "last_message_at": row["last"],
        "channels": conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        "optouts": conn.execute("SELECT COUNT(*) FROM optouts").fetchone()[0],
    }


def day_counts(conn, channel=None, days=7):
    """최근 days일의 KST 날짜별 메시지 수. 어느 날짜를 정리할지 고를 때 쓴다."""
    from collections import Counter

    since = (datetime.now(KST).date() - timedelta(days=days - 1)).isoformat()
    sql = f"SELECT m.created_at {_VISIBLE} AND m.created_at >= ?"
    params = [kst_day_range(since)[0]]
    if channel:
        sql += " AND (m.channel_id=? OR m.channel_name=?)"
        params += [str(channel), str(channel)]
    counts = Counter(
        datetime.fromisoformat(r["created_at"]).astimezone(KST).date().isoformat()
        for r in conn.execute(sql, params)
    )
    today = datetime.now(KST).date()
    return [
        ((today - timedelta(days=i)).isoformat(), counts.get((today - timedelta(days=i)).isoformat(), 0))
        for i in range(days)
    ]
