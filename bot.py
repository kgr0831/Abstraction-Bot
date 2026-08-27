"""디스코드 수집 봇.

하는 일은 하나뿐이다 — 화이트리스트 채널의 메시지를 SQLite에 넣는다.
요약·클러스터링·아이디어 추출은 MCP 서버 너머의 에이전트가 한다.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

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


# --- 운영 커맨드 ------------------------------------------------------------

manager = app_commands.checks.has_permissions(manage_guild=True)


@client.tree.command(name="수집시작", description="이 채널의 대화 수집을 시작합니다")
@app_commands.describe(채널="비우면 현재 채널")
@manager
async def start(interaction: discord.Interaction, 채널: discord.TextChannel = None):
    ch = 채널 or interaction.channel
    db.add_channel(conn, ch.id, interaction.guild_id, ch.name)
    await interaction.response.send_message(f"{ch.mention} 수집 시작", ephemeral=True)
    await ch.send(NOTICE)


@client.tree.command(name="수집중지", description="채널의 대화 수집을 중지합니다")
@manager
async def stop(interaction: discord.Interaction, 채널: discord.TextChannel = None):
    ch = 채널 or interaction.channel
    ok = db.remove_channel(conn, ch.id)
    await interaction.response.send_message(
        f"{ch.mention} 수집 중지" if ok else f"{ch.mention} 는 수집 중이 아닙니다",
        ephemeral=True,
    )


@client.tree.command(name="backfill", description="과거 대화를 일회성으로 가져옵니다")
@app_commands.describe(
    채널="비우면 현재 채널",
    일수="최근 N일. 기본 90일",
    시작="YYYY-MM-DD. 주면 일수 대신 이 날짜부터",
    끝="YYYY-MM-DD. 비우면 오늘까지",
)
@manager
async def backfill(
    interaction: discord.Interaction,
    채널: discord.TextChannel = None,
    일수: int = 90,
    시작: str = "",
    끝: str = "",
):
    ch = 채널 or interaction.channel
    if not db.is_collected(conn, ch.id):
        await interaction.response.send_message(
            f"{ch.mention} 는 수집 채널이 아닙니다. `/수집시작` 먼저 실행하세요.",
            ephemeral=True,
        )
        return
    for label, d in (("시작", 시작), ("끝", 끝)):
        if d and not db.valid_date(d):
            await interaction.response.send_message(
                f"{label} 날짜 형식이 잘못됐습니다: `{d}` (YYYY-MM-DD)", ephemeral=True
            )
            return
    if 시작 and 끝 and 끝 < 시작:
        await interaction.response.send_message(
            f"끝(`{끝}`)이 시작(`{시작}`)보다 앞섭니다.", ephemeral=True
        )
        return

    if 시작:
        after = datetime.fromisoformat(db.kst_day_range(시작)[0])
        before = datetime.fromisoformat(db.kst_day_range(끝)[1]) if 끝 else None
        span = f"{시작}~{끝 or '오늘'}"
    else:
        after, before = datetime.now(timezone.utc) - timedelta(days=일수), None
        span = f"최근 {일수}일"

    await interaction.response.defer(ephemeral=True)
    n = 0
    async for msg in ch.history(limit=None, after=after, before=before, oldest_first=True):
        if msg.author.bot or db.is_opted_out(conn, msg.author.id):
            continue
        db.upsert_message(conn, msg)
        n += 1
    log.info("backfill %s (%s): %d건", ch.name, span, n)
    await interaction.followup.send(f"{ch.mention} {span} {n}건 적재", ephemeral=True)


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


@client.tree.command(name="상태", description="수집 현황을 봅니다")
async def status(interaction: discord.Interaction):
    s = db.stats(conn)
    lines = [
        f"**수집 채널 {s['channels']}개 · 메시지 {s['messages']}건 · 수집중단 {s['optouts']}명**"
    ]
    for c in db.list_channels(conn):
        last = (c["last_message_at"] or "-")[:16].replace("T", " ")
        lines.append(f"· #{c['channel_name']} — {c['message_count']}건, 최근 {last} UTC")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# --- CLI 연동 (사용 전 필수) ------------------------------------------------

LAUNCHER_URL = os.getenv("LAUNCHER_URL", "http://127.0.0.1:8787")


@client.tree.command(name="시작", description="내 세팅 콘솔을 엽니다 (에이전트·MCP 연결)")
async def setup(interaction: discord.Interaction):
    existing = db.get_link(conn, interaction.user.id)
    code, ttl = db.new_pair_code(conn, interaction.user.id, interaction.user.display_name)
    head = (
        f"현재 연동: **{existing['identity']}** ({existing['agent']})\n\n"
        if existing
        else ""
    )
    await interaction.response.send_message(
        head
        + f"**세팅 콘솔 열기 → {LAUNCHER_URL}/?code={code}**\n"
        f"(연동 코드 `{code}` · {ttl}분 유효)\n\n"
        "콘솔에서 다음을 전부 할 수 있습니다.\n"
        "· Claude / Codex 계정 연결 — 브라우저 로그인만\n"
        "· 디스코드 연동 — 코드는 자동으로 채워집니다\n"
        "· Notion · Figma 등 MCP 연결\n\n"
        "링크가 안 열리면 본인 PC에서 `start.bat` 을 먼저 실행하세요.\n"
        "**반드시 본인 CLI 계정으로만 동작합니다.**",
        ephemeral=True,
    )


@client.tree.command(name="리더보드", description="발언 집계를 봅니다")
async def leaderboard_cmd(interaction: discord.Interaction):
    top = db.leaderboard(conn, None)[:5]
    lines = [f"**리더보드 → {LAUNCHER_URL}/leaderboard**", ""]
    if top:
        lines += [
            f"{i}. {r['author']} — {r['messages']}건 / {r['days']}일"
            for i, r in enumerate(top, 1)
        ]
    else:
        lines.append("아직 수집된 대화가 없습니다. `/수집시작` 부터 실행하세요.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@client.tree.command(name="연동상태", description="내 CLI 연동 상태를 봅니다")
async def link_status(interaction: discord.Interaction):
    row = db.get_link(conn, interaction.user.id)
    await interaction.response.send_message(
        f"연동됨 — **{row['identity']}** ({row['agent']})"
        f", {row['linked_at'][:16].replace('T', ' ')} UTC"
        if row
        else "연동되어 있지 않습니다. `/시작` 으로 본인 CLI를 연결하세요.",
        ephemeral=True,
    )


@client.tree.command(name="연동해제", description="내 CLI 연동을 해제합니다")
async def link_remove(interaction: discord.Interaction):
    ok = db.unlink(conn, interaction.user.id)
    await interaction.response.send_message(
        "연동을 해제했습니다. 다시 쓰려면 `/시작` 하세요."
        if ok
        else "연동되어 있지 않았습니다.",
        ephemeral=True,
    )


@start.error
@stop.error
@backfill.error
async def perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
    else:
        raise error


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
