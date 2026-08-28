"""디스코드 수집 봇.

하는 일은 하나뿐이다 — 화이트리스트 채널의 메시지를 SQLite에 넣는다.
요약·클러스터링·아이디어 추출은 MCP 서버 너머의 에이전트가 한다.

운영은 대부분 웹 콘솔(launcher.py)에서 한다. 여기 남은 슬래시 커맨드는
디스코드 안에서 반드시 닿아야 하는 것들뿐이다 (진입점 · 현황 · 수집 거부).
콘솔은 아래 브리지 함수로 봇 루프에 일을 시킨다.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import db
import digest

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

LAUNCHER_URL = os.getenv("LAUNCHER_URL", "http://127.0.0.1:8787")

NOTICE = (
    "**대화 수집이 시작되었습니다.**\n"
    "이 채널의 메시지가 저장되어 날짜별 요약과 아이디어 정리에 사용됩니다.\n"
    "원하지 않으시면 `/수집중단` — 앞으로도, 이미 저장된 것도 제외됩니다. "
    "언제든 `/수집재개` 로 되돌릴 수 있습니다."
)

conn = db.connect()
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True


class Collector(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        log.info("슬래시 커맨드 동기화 완료")


client = Collector()


def collectible(channel, author_id):
    """이 메시지를 저장해도 되는가."""
    home = getattr(channel, "parent", None) or channel
    return db.is_collected(conn, home.id) and not db.is_opted_out(conn, author_id)


@client.event
async def on_ready():
    log.info("%s 접속. 서버 %d개", client.user, len(client.guilds))
    if not digest_tick.is_running():
        digest_tick.start()


@client.event
async def on_message(msg):
    if msg.author.bot or not msg.guild:
        return
    if collectible(msg.channel, msg.author.id):
        db.upsert_message(conn, msg)


@client.event
async def on_raw_message_edit(payload):
    # raw 이벤트라 캐시에 없는 옛 메시지도 잡힌다
    content = payload.data.get("content")
    if content is None:
        return
    db.update_content(conn, payload.message_id, content, payload.data.get("edited_timestamp"))


@client.event
async def on_raw_message_delete(payload):
    db.mark_deleted(conn, payload.message_id)


# --- 웹 콘솔용 브리지 --------------------------------------------------------
# 콘솔은 별도 스레드에서 돌기 때문에 봇 루프에 코루틴을 넘겨야 한다.


class NotReady(RuntimeError):
    pass


def ready():
    return client.is_ready() and not client.is_closed()


def bridge_conn():
    """콘솔에서 부르는 DB 작업용 커넥션.

    모듈 전역 conn 은 봇 루프 스레드의 것이고 SQLite 커넥션은 스레드에 묶인다.
    콘솔은 스레드풀에서 브리지를 부르므로 여기서 매번 새로 연다.
    """
    return db.connect()


def _call(coro, timeout=120):
    if not ready():
        coro.close()
        raise NotReady("봇이 아직 디스코드에 접속하지 않았습니다.")
    return asyncio.run_coroutine_threadsafe(coro, client.loop).result(timeout)


def guild_channels():
    """수집 대상이 될 수 있는 텍스트 채널. 읽기 권한 여부도 같이 준다."""
    if not ready():
        raise NotReady("봇이 아직 디스코드에 접속하지 않았습니다.")
    out = []
    c = bridge_conn()
    try:
        for g in client.guilds:
            for ch in g.text_channels:
                perms = ch.permissions_for(g.me)
                out.append({
                    "id": str(ch.id),
                    "name": ch.name,
                    "guild": g.name,
                    "guild_id": str(g.id),
                    "readable": bool(perms.read_message_history and perms.view_channel),
                    "collected": db.is_collected(c, ch.id),
                })
    finally:
        c.close()
    return sorted(out, key=lambda x: (x["guild"], x["name"]))


def start_collect(channel_id, notice=True):
    ch = client.get_channel(int(channel_id))
    if ch is None:
        raise NotReady("채널을 찾을 수 없습니다. 봇이 그 서버에 있는지 확인하세요.")
    c = bridge_conn()
    try:
        db.add_channel(c, ch.id, ch.guild.id, ch.name)
    finally:
        c.close()
    if notice:
        _call(ch.send(NOTICE))
    return ch.name


def stop_collect(channel_id):
    c = bridge_conn()
    try:
        return db.remove_channel(c, channel_id)
    finally:
        c.close()


async def _backfill(ch, after, before, job):
    # 봇 루프 스레드에서 돈다 (_call 이 넘긴다) — 전역 conn 을 그대로 써도 된다.
    n = 0
    async for msg in ch.history(limit=None, after=after, before=before, oldest_first=True):
        if msg.author.bot or db.is_opted_out(conn, msg.author.id):
            continue
        db.upsert_message(conn, msg)
        n += 1
        if job is not None and n % 50 == 0:
            job["count"] = n
    if job is not None:
        job["count"] = n
    return n


def run_backfill(channel_id, after=None, before=None, days=None, job=None):
    """콘솔이 부른다. after/before 는 UTC datetime, 없으면 days 로 계산."""
    ch = client.get_channel(int(channel_id))
    if ch is None:
        raise NotReady("채널을 찾을 수 없습니다.")
    if after is None:
        after = datetime.now(timezone.utc) - timedelta(days=days or 90)
    return _call(_backfill(ch, after, before, job), timeout=3600)


# --- 일일 결산 --------------------------------------------------------------

DIGEST_CHANNEL = "일일결산"


async def ensure_digest_channel(guild_id=None, name=DIGEST_CHANNEL):
    """결산 채널을 찾거나 만든다. 권한이 없으면 알려준다."""
    g = client.get_guild(int(guild_id)) if guild_id else (client.guilds[0] if client.guilds else None)
    if g is None:
        raise NotReady("서버를 찾을 수 없습니다.")
    found = discord.utils.get(g.text_channels, name=name)
    if found:
        return found
    if not g.me.guild_permissions.manage_channels:
        raise NotReady(
            "봇에게 '채널 관리' 권한이 없어 채널을 못 만듭니다. "
            "디스코드에서 #%s 채널을 직접 만들거나, 기존 채널을 고르세요." % name)
    return await g.create_text_channel(name, topic="매일 자동으로 올라오는 대화 결산")


def setup_digest(guild_id=None, hour=9, channel_id=None, model=None, retention=None):
    """콘솔이 부른다. 채널·시각·모델을 저장한다."""
    ch = client.get_channel(int(channel_id)) if channel_id else _call(ensure_digest_channel(guild_id))
    if ch is None:
        raise NotReady("채널을 찾을 수 없습니다.")
    c = bridge_conn()
    try:
        db.set_setting(c, "digest_channel_id", ch.id)
        db.set_setting(c, "digest_hour", int(hour))
        if model:
            db.set_setting(c, "digest_model", model)
        if retention is not None:
            db.set_setting(c, "retention_days", max(0, int(retention)))
    finally:
        c.close()
    return {"id": str(ch.id), "name": ch.name, "hour": int(hour), "model": digest.model()}


def digest_config():
    c = bridge_conn()
    try:
        cid = db.get_setting(c, "digest_channel_id")
        return {"channel_id": cid,
                "channel_name": getattr(client.get_channel(int(cid)), "name", None) if cid else None,
                "hour": int(db.get_setting(c, "digest_hour", "9")),
                "last": db.get_setting(c, "digest_last"),
                "retention_days": db.retention_days(c),
                "model": digest.model(),
                "models": digest.MODELS,
                "has_key": digest.key_present(),
                "provider": digest.spec()["provider"]}
    finally:
        c.close()


async def post_digest(channel_id, date):
    """수집 채널마다 그날 결산을 만들어 결산 채널에 올린다."""
    ch = client.get_channel(int(channel_id))
    if ch is None:
        raise NotReady("결산 채널을 찾을 수 없습니다.")
    c = bridge_conn()
    try:
        srcs = [dict(r) for r in db.list_channels(c)]
    finally:
        c.close()
    loop = asyncio.get_running_loop()
    posted = 0
    for src in srcs:
        # digest.build 는 LLM 을 부르는 블로킹 호출이라 봇 루프를 막으면 안 된다
        text = await loop.run_in_executor(
            None, digest.build, src["channel_id"], src["channel_name"], date)
        if not text:
            continue
        for part in digest.chunks(text):
            await ch.send(part)
        posted += 1
    if not posted:
        await ch.send("%s 에는 수집된 대화가 없었습니다." % date)
    log.info("결산 게시 %s: 채널 %d개", date, posted)
    return posted


def run_digest_now(date=None):
    """콘솔의 '지금 결산' 버튼."""
    cfg = digest_config()
    if not cfg["channel_id"]:
        raise NotReady("결산 채널을 먼저 정하세요.")
    return _call(post_digest(cfg["channel_id"], date or digest.yesterday_kst()), timeout=900)


def purge_tick():
    """하루 한 번 보존 기간이 지난 메시지를 지운다. 결산 설정과 무관하게 돈다."""
    today = datetime.now(db.KST).date().isoformat()
    c = bridge_conn()
    try:
        if db.get_setting(c, "last_purge") == today:
            return 0
        db.set_setting(c, "last_purge", today)
        n = db.purge_old(c)
    finally:
        c.close()
    if n:
        log.info("보존 기간 정리: %d건 삭제", n)
    return n


@tasks.loop(minutes=1)
async def digest_tick():
    """설정된 시각(KST)이 지나면 전날 결산을 올리고, 하루 한 번 오래된 원문을 지운다."""
    try:
        await asyncio.get_running_loop().run_in_executor(None, purge_tick)
    except Exception as e:  # noqa: BLE001
        log.warning("정리 실패: %s", e)
    c = bridge_conn()
    try:
        cid = db.get_setting(c, "digest_channel_id")
        hour = int(db.get_setting(c, "digest_hour", "9"))
        last = db.get_setting(c, "digest_last", "")
        target = digest.yesterday_kst()
        if not cid or datetime.now(db.KST).hour < hour or last == target:
            return
        # 게시 전에 먼저 찍는다 — 실패해도 같은 날 여러 번 올리지 않는다
        db.set_setting(c, "digest_last", target)
    finally:
        c.close()
    try:
        await post_digest(cid, target)
    except Exception as e:  # noqa: BLE001
        log.warning("결산 실패 %s: %s", target, e)


@digest_tick.before_loop
async def _wait_ready():
    await client.wait_until_ready()


# --- 슬래시 커맨드 (디스코드 안에서 반드시 닿아야 하는 것만) ------------------


@client.tree.command(name="시작", description="내 세팅 콘솔을 엽니다 — 여기서 거의 다 됩니다")
async def setup(interaction: discord.Interaction):
    perms = interaction.user.guild_permissions if interaction.guild else None
    is_manager = bool(perms and perms.manage_guild)
    existing = db.get_link(conn, interaction.user.id)
    code, ttl = db.new_pair_code(
        conn, interaction.user.id, interaction.user.display_name, is_manager
    )
    head = (
        f"현재 연동: **{existing['identity']}** ({existing['agent']})\n\n"
        if existing
        else ""
    )
    menu = (
        "· 대화 보기 — 채팅형 뷰어, 날짜 범위, 검색\n"
        "· 에이전트 계정 연결 — 브라우저 로그인만\n"
        "· MCP 연결 — Notion · Figma 등 클릭 한 번\n"
        "· 리더보드\n"
    )
    if is_manager:
        menu += "· **수집 관리** — 채널 켜고 끄기, 과거 대화 가져오기\n"
    await interaction.response.send_message(
        head
        + f"**세팅 콘솔 → {LAUNCHER_URL}/?code={code}**\n"
        f"(연동 코드 `{code}` · {ttl}분 유효 · 자동으로 채워집니다)\n\n"
        + menu
        + "\n링크가 안 열리면 본인 PC에서 `start.bat` 을 먼저 실행하세요.\n"
        "**반드시 본인 CLI 계정으로만 동작합니다.**",
        ephemeral=True,
    )


@client.tree.command(name="상태", description="수집 현황을 봅니다")
async def status(interaction: discord.Interaction):
    s = db.stats(conn)
    lines = [
        f"**수집 채널 {s['channels']}개 · 메시지 {s['messages']}건 · 수집중단 {s['optouts']}명**"
    ]
    for c in db.list_channels(conn):
        last = (c["last_message_at"] or "-")[:16].replace("T", " ")
        lines.append(f"· #{c['channel_name']} — {c['message_count']}건, 최근 {last} UTC")
    lines.append(f"\n자세히 → {LAUNCHER_URL}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@client.tree.command(name="리더보드", description="발언 집계를 봅니다")
async def leaderboard_cmd(interaction: discord.Interaction):
    top = db.leaderboard(conn, None)[:5]
    lines = [f"**리더보드 → {LAUNCHER_URL}/#board**", ""]
    if top:
        lines += [
            f"{i}. {r['author']} — {r['messages']}건 / {r['days']}일"
            for i, r in enumerate(top, 1)
        ]
    else:
        lines.append("아직 수집된 대화가 없습니다. 콘솔의 **수집 관리** 에서 채널을 켜세요.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@client.tree.command(name="수집중단", description="내 메시지를 수집에서 제외합니다")
async def optout(interaction: discord.Interaction):
    n = db.opt_out(conn, interaction.user.id)
    await interaction.response.send_message(
        f"수집에서 제외했습니다. 이미 저장된 {n}건도 조회에서 빠집니다.", ephemeral=True
    )


@client.tree.command(name="수집재개", description="내 메시지 수집을 다시 허용합니다")
async def optin(interaction: discord.Interaction):
    ok = db.opt_in(conn, interaction.user.id)
    await interaction.response.send_message(
        "앞으로의 메시지부터 다시 수집합니다. (제외 기간의 메시지는 복구하지 않습니다)"
        if ok
        else "수집 중단 상태가 아니었습니다.",
        ephemeral=True,
    )


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(".env 에 DISCORD_TOKEN 을 넣으세요 (.env.example 참고)")
    try:
        client.run(token)
    except discord.PrivilegedIntentsRequired:
        app = os.getenv("DISCORD_APP_ID", "")
        raise SystemExit(
            "\n[설정 필요] MESSAGE CONTENT INTENT 가 꺼져 있습니다.\n"
            f"  https://discord.com/developers/applications/{app}/bot\n"
            "  위 페이지 > Privileged Gateway Intents > MESSAGE CONTENT INTENT 켜고 저장\n"
        )


if __name__ == "__main__":
    main()
