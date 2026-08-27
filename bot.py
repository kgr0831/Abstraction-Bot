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
@app_commands.describe(채널="비우면 현재 채널", 일수="기본 90일")
@manager
async def backfill(
    interaction: discord.Interaction, 채널: discord.TextChannel = None, 일수: int = 90
):
    ch = 채널 or interaction.channel
    if not db.is_collected(conn, ch.id):
        await interaction.response.send_message(
            f"{ch.mention} 는 수집 채널이 아닙니다. `/수집시작` 먼저 실행하세요.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    after = datetime.now(timezone.utc) - timedelta(days=일수)
    n = 0
    async for msg in ch.history(limit=None, after=after, oldest_first=True):
        if msg.author.bot or db.is_opted_out(conn, msg.author.id):
            continue
        db.upsert_message(conn, msg)
        n += 1
    log.info("backfill %s: %d건", ch.name, n)
    await interaction.followup.send(f"{ch.mention} 최근 {일수}일 {n}건 적재", ephemeral=True)


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
