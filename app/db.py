"""SQLite 저장소 — 스키마와 공용 쿼리.

봇(쓰기)과 MCP 서버(읽기)가 별도 프로세스라 WAL 모드로 연다.
시각은 UTC로 저장하고, 조회는 KST 날짜로 받는다 (팀이 KST로 말하므로).
"""

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("COLLECTOR_DB") or PROJECT / "data" / "messages.db")
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

-- 디스코드 사용자 <-> 본인 CLI 계정 바인딩. 이게 없으면 조회를 거부한다.
CREATE TABLE IF NOT EXISTS links (
  user_id    TEXT PRIMARY KEY,
  user_name  TEXT NOT NULL,
  agent      TEXT NOT NULL,
  identity   TEXT NOT NULL,
  linked_at  TEXT NOT NULL,
  is_manager INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_identity ON links(identity);

CREATE TABLE IF NOT EXISTS pair_codes (
  code       TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  user_name  TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  is_manager INTEGER NOT NULL DEFAULT 0
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


def valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


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


def get_conversation(conn, channel, date, until=None):
    """date 하루치. until 을 주면 date~until (양끝 포함) 범위."""
    start = kst_day_range(date)[0]
    end = kst_day_range(until or date)[1]
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


# --- CLI 연동 --------------------------------------------------------------

PAIR_TTL_MIN = 10


def new_pair_code(conn, user_id, user_name, is_manager=False):
    """페어링 코드 발급. 사용자당 하나만 살아 있게 한다.

    서버 관리 권한을 코드에 실어 보낸다 — 콘솔에서 수집 관리 메뉴를 열지 말지
    이걸로 정한다. 권한이 바뀌면 /시작 을 다시 치면 갱신된다.
    """
    conn.execute("DELETE FROM pair_codes WHERE user_id=?", (str(user_id),))
    code = secrets.token_hex(3).upper()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=PAIR_TTL_MIN)).isoformat()
    conn.execute(
        "INSERT INTO pair_codes VALUES (?,?,?,?,?)",
        (code, str(user_id), user_name, expires, int(is_manager)),
    )
    return code, PAIR_TTL_MIN


def redeem_pair_code(conn, code, agent, identity):
    """코드를 소진하고 바인딩을 만든다. 성공하면 (user_id, user_name)."""
    conn.execute("DELETE FROM pair_codes WHERE expires_at < ?", (now(),))
    row = conn.execute(
        "SELECT * FROM pair_codes WHERE code=?", (code.strip().upper(),)
    ).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM pair_codes WHERE code=?", (code.strip().upper(),))
    conn.execute(
        "INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?)",
        (row["user_id"], row["user_name"], agent, identity, now(), row["is_manager"]),
    )
    return row["user_id"], row["user_name"]


def get_link(conn, user_id):
    return conn.execute(
        "SELECT * FROM links WHERE user_id=?", (str(user_id),)
    ).fetchone()


def unlink(conn, user_id):
    return conn.execute("DELETE FROM links WHERE user_id=?", (str(user_id),)).rowcount > 0


def linked_identities(conn):
    """연동된 CLI 계정 식별자 집합. MCP 게이트가 이걸로 판정한다."""
    return {r["identity"] for r in conn.execute("SELECT identity FROM links")}


ACCOUNTS_PATH = Path(os.getenv("COLLECTOR_ACCOUNTS") or PROJECT / "data" / "accounts.json")


def local_accounts():
    """이 PC의 등록기에 등록된 CLI 계정들. 봇 DB가 아니라 로컬 파일이다."""
    if not ACCOUNTS_PATH.exists():
        return []
    try:
        return json.loads(ACCOUNTS_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def leaderboard(conn, since=None, channel=None):
    """발언자별 집계. SQL 날짜연산 대신 파이썬에서 KST로 접는다
    (created_at 의 마이크로초까지 SQLite date() 가 안전하게 못 읽음)."""
    sql = f"SELECT m.author_id, m.author_name, m.channel_name, m.created_at {_VISIBLE}"
    params = []
    if since:
        sql += " AND m.created_at >= ?"
        params.append(kst_day_range(since)[0])
    if channel:
        sql += " AND (m.channel_id=? OR m.channel_name=?)"
        params += [str(channel), str(channel)]

    agg = {}
    for r in conn.execute(sql, params):
        e = agg.setdefault(
            r["author_id"],
            {"author": r["author_name"], "messages": 0, "days": set(), "channels": set()},
        )
        e["author"] = r["author_name"]
        e["messages"] += 1
        e["days"].add(datetime.fromisoformat(r["created_at"]).astimezone(KST).date())
        e["channels"].add(r["channel_name"])
    out = [
        {"author": v["author"], "messages": v["messages"],
         "days": len(v["days"]), "channels": len(v["channels"])}
        for v in agg.values()
    ]
    return sorted(out, key=lambda x: -x["messages"])


def link_by_identity(conn, identity):
    """이 콘솔이 누구 것인지. 수집 관리 메뉴를 열지 말지 여기서 정한다."""
    return conn.execute(
        "SELECT * FROM links WHERE identity=? ORDER BY linked_at DESC LIMIT 1",
        (identity,),
    ).fetchone()
