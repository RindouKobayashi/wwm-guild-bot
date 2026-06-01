import discord
from discord import app_commands, ButtonStyle
from discord.ext import commands, tasks
from discord.ui import ChannelSelect
import json
import time
import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Set, Tuple
from collections import defaultdict
from settings import logger, BASE_DIR, WWM_UID, WWM_FULL_GUILD_URL, CLUB_ID, WWM_REDIS_PLAYER_URL
from utility.wwm import _wwm_api_post
from utility.api_constants import BOSS_NAMES

# -----------------------------------------------------------------------------
# Database path
# -----------------------------------------------------------------------------
DB_PATH = BASE_DIR / "data" / "event_log.db"

# -----------------------------------------------------------------------------
# Event type constants
# -----------------------------------------------------------------------------


ROLE_NAMES = {
    1: "Guild Leader", 2: "Vice Leader", 5: "Command",
    7: "Half Time Performer", 10000: "䨻䨻䨻䨻䨻", 10001: "䨻䨻䨻䨻",
    10002: "䨻䨻䨻", 10003: "䨻䨻", 10004: "Construction", 10005: "Absent",
}

RANK_ORDER = [1, 2, 5, 7, 10000, 10001, 10002, 10003, 10004, 10005]
LADDER_RANKS = {10000, 10001, 10002, 10003}
ASSIGNMENT_RANKS = {1, 2, 5, 7, 10004, 10005}

GMT8 = timezone(timedelta(hours=8))


def get_boss_name(bid: int) -> str:
    return BOSS_NAMES.get(bid, f"Boss#{bid}")


def get_role_name(rid: int) -> str:
    return ROLE_NAMES.get(rid, f"Role#{rid}")


def format_schedule_day(dhm: list) -> str:
    if len(dhm) >= 3:
        return f"Day{dhm[0]} {dhm[1]:02d}:{dhm[2]:02d}"
    return str(dhm)


def truncate_pid(pid: str) -> str:
    if isinstance(pid, str) and len(pid) >= 8:
        return pid[:8] + "..."
    return str(pid)


def decode_event_type(cat: int, ev: int) -> str:
    names = {1: "Player Event", 2: "Rank Management", 3: "Player Left",
             4: "Stats Update", 5: "Guild Schedule", 6: "Role Changed",
             7: "Message Received", 8: "Transfer", 13: "System Event", 15: "Guild Stats"}
    overrides = {(2, 8): "Transfer", (4, 13): "Guild Party Time",
                 (4, 14): "Showdown Change", (4, 15): "Schedule Change",
                 (5, 20): "Notification", (5, 21): "Schedule",
                 (5, 22): "Raid Timer", (5, 23): "Objective Update"}
    return overrides.get((cat, ev), names.get(cat, f"Type {cat}"))


def timestamp_line(ts: int) -> str:
    """Discord timestamp formatting: full datetime + relative."""
    return f"\n<t:{ts}:F> (<t:{ts}:R>)"


def decode_extra(cat: int, ev: int, extra: list) -> str:
    """Decode extra data into human-readable text (fallback for unknown events)."""
    if not extra:
        return ""
    if cat == 4 and ev == 15 and len(extra) >= 4:
        actor = extra[0]; actor_hostnum = extra[1]
        parts = []
        for i in range(2, len(extra) - 1, 2):
            bid = extra[i]; sched = extra[i + 1]
            if isinstance(bid, int) and isinstance(sched, list) and len(sched) == 3:
                parts.append(f"{get_boss_name(bid)} ({format_schedule_day(sched)})")
        if parts:
            return f"👤 {truncate_pid(actor)} @{actor_hostnum} | " + " | ".join(parts)
        return str(extra)
    if cat == 4 and ev == 13 and len(extra) >= 3:
        actor, actor_hostnum, vl = extra[0], extra[1], extra[2]
        if isinstance(vl, list) and len(vl) >= 1:
            return f"👤 {truncate_pid(actor)} @{actor_hostnum} | 🎉 Guild Party time changed to {vl[0]}:00"
        return str(extra)
    if cat == 4 and ev == 14 and len(extra) >= 4:
        actor, actor_hostnum = extra[0], extra[1]
        sparts = [f"📅 {format_schedule_day(item)}" for i in range(2, len(extra)) if isinstance((item := extra[i]), list) and len(item) == 3]
        if sparts:
            return f"👤 {truncate_pid(actor)} @{actor_hostnum} | 🎪 Showdown changed to " + " and ".join(sparts)
        return str(extra)
    if cat == 5 and len(extra) >= 2 and isinstance(extra[0], int) and isinstance(extra[1], int):
        return f"🐉 {get_boss_name(extra[0])} | Day {extra[1]}"
    parts = []
    for item in extra:
        if isinstance(item, list) and len(item) == 3:
            parts.append(f"📅 {format_schedule_day(item)}")
        elif isinstance(item, list) and len(item) == 2:
            parts.append(f"⚔️ Lv:{item[0]}+Rarity:{item[1]}")
        elif isinstance(item, int):
            parts.append(get_boss_name(item) if item in BOSS_NAMES else
                         get_role_name(item) if item in ROLE_NAMES else
                         f"📦 Item:{item}" if 10000 <= item <= 19999 else
                         f"🏅 Rank:{item}" if 1 <= item <= 7 else str(item))
        elif isinstance(item, str) and len(item) == 16:
            parts.append(f"👤 {truncate_pid(item)}")
        else:
            parts.append(str(item))
    return " | ".join(parts)


# -----------------------------------------------------------------------------
# Helper: fetch club event log from API
# -----------------------------------------------------------------------------
def fetch_event_log_raw() -> Optional[Dict[str, Any]]:
    """Call the game API and return the full response dict, or None on failure."""
    payload = {
        "club_id": CLUB_ID,
        "uid": WWM_UID,
        "field_info": {"event_log": []},
        "hostnum": 10103,
    }
    return _wwm_api_post(WWM_FULL_GUILD_URL, payload, timeout=15)


PLAYER_INFO_URL = WWM_REDIS_PLAYER_URL


def resolve_player_name_sync(pid: str, hostnum: int = 10403) -> Optional[str]:
    """Fetch a player's nickname by PID using the correct hostnum."""
    try:
        payload = {
            "fields": ["base"],
            "hostnum2pids": {hostnum: [pid]},
        }
        data = _wwm_api_post(PLAYER_INFO_URL, payload)
        if data and 'result' in data and pid in data['result']:
            base = data['result'][pid].get('base', {})
            name = base.get('nickname')
            if name:
                return name
    except Exception as e:
        logger.debug(f"Name resolution failed for {truncate_pid(pid)}@{hostnum}: {e}")
    return None


# -----------------------------------------------------------------------------
# Database helpers
# -----------------------------------------------------------------------------
async def init_db():
    (BASE_DIR / "data").mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()


async def get_config(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_config(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def get_channel_id() -> Optional[int]:
    val = await get_config("channel_id", "0")
    return int(val) if val and int(val) > 0 else None


async def get_last_timestamp() -> int:
    val = await get_config("last_timestamp", "0")
    return int(val)


async def set_last_timestamp(ts: int):
    await set_config("last_timestamp", str(ts))


# -----------------------------------------------------------------------------
# Main Cog
# -----------------------------------------------------------------------------
class EventLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._name_cache: Dict[str, str] = {}

    # --- Setup / Teardown ---

    async def cog_load(self):
        await init_db()
        self.poll_event_log.start()
        logger.info("EventLogCog loaded, polling started (60s interval)")

    async def cog_unload(self):
        self.poll_event_log.cancel()
        logger.info("EventLogCog unloaded")

    # --- Tasks ---

    @tasks.loop(seconds=60.0)
    async def poll_event_log(self):
        try:
            await self._poll()
        except Exception as e:
            logger.error(f"Event poll error: {e}", exc_info=True)

    @poll_event_log.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # --- Commands ---

    @app_commands.command(name="event_log_setup", description="Set the channel for event log posts")
    @app_commands.default_permissions(administrator=True)
    async def event_log_setup(self, interaction: discord.Interaction):
        view = EventLogChannelSelect(self)
        await interaction.response.send_message(
            "📋 Select the channel where event log messages should be posted:",
            view=view, ephemeral=True,
        )

    @app_commands.command(name="event_log_poll", description="Manually trigger an event poll now")
    @app_commands.default_permissions(administrator=True)
    async def event_log_poll(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            new_count = await self._poll()
            await interaction.followup.send(f"✅ Polled. {new_count} new event(s) found.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Poll error: {e}", ephemeral=True)

    # --- Core Polling Logic ---

    async def _poll(self) -> int:
        channel_id = await get_channel_id()
        if not channel_id:
            return 0

        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning(f"Event log channel {channel_id} not found")
            return 0

        raw = await self.bot.loop.run_in_executor(None, fetch_event_log_raw)
        if not raw or 'result' not in raw:
            return 0

        event_log = raw['result'].get('event_log', {}).get('event_logs', {})
        if not event_log:
            return 0

        # Flatten ALL events
        all_events: List[Dict] = []
        for cat_int, raw_events in event_log.items():
            for raw_ev in raw_events:
                ev_id = raw_ev[0]
                ts = raw_ev[1]
                extra = raw_ev[2] if len(raw_ev) > 2 else []
                all_events.append({
                    "timestamp": ts, "category_id": cat_int,
                    "event_id": ev_id, "extra": extra,
                })

        if not all_events:
            return 0

        all_events.sort(key=lambda e: e["timestamp"])
        last_ts = await get_last_timestamp()

        # First run: only last 24 hours
        if last_ts == 0:
            last_ts = int(time.time()) - 86400

        new_events = [e for e in all_events if e["timestamp"] > last_ts]
        if not new_events:
            return 0

        # Process events in chronological order
        for event in new_events:
            try:
                await self._post_event(channel, event)
            except Exception as e:
                logger.error(f"Failed to post event {event}: {e}")

        max_ts = max(e["timestamp"] for e in new_events)
        await set_last_timestamp(max_ts)
        return len(new_events)

    async def _post_event(self, channel: discord.TextChannel, event: Dict):
        """Route event to the appropriate handler based on category."""
        cat, ev, ts = event["category_id"], event["event_id"], event["timestamp"]

        if cat == 1:
            await self._post_player_event(channel, event)
        elif cat == 2:
            await self._post_rank_event(channel, event)
        else:
            await self._post_other_event(channel, event)

    # --- Category 1: Player Events (join, leave, kick, approve, invite) ---

    async def _post_player_event(self, channel: discord.TextChannel, event: Dict):
        cat, ev, ts, extra = event["category_id"], event["event_id"], event["timestamp"], event["extra"]
        ts_str = timestamp_line(ts)

        # Player left (ev=2): [pid, hostnum]
        if ev == 2 and len(extra) >= 2:
            pid, hostnum = extra[0], extra[1]
            name = await self._resolve_name(pid, hostnum)
            embed = discord.Embed(
                title="👋 Player Left Guild",
                description=f"**{name}**{ts_str}",
                color=discord.Color.orange(),
            )
            await channel.send(embed=embed)
            return

        # Two-player interactions: [actor_pid, actor_hostnum, target_pid, target_hostnum]
        if ev in (3, 4, 5) and len(extra) >= 4:
            actor, actor_hn, target, target_hn = extra[0], extra[1], extra[2], extra[3]
            actor_name = await self._resolve_name(actor, actor_hn)
            target_name = await self._resolve_name(target, target_hn)

            if ev == 3:
                title = "🚫 Player Kicked"
                desc = f"**{actor_name}** kicked **{target_name}**{ts_str}"
                color = discord.Color.red()
            elif ev == 4:
                title = "✅ Join Approved"
                desc = f"**{actor_name}** approved **{target_name}**'s application{ts_str}"
                color = discord.Color.green()
            else:  # ev == 5
                title = "📥 Invited + Joined"
                desc = f"**{actor_name}** invited **{target_name}** (they joined){ts_str}"
                color = discord.Color.blue()

            embed = discord.Embed(title=title, description=desc, color=color)
            await channel.send(embed=embed)
            return

        # Fallback for unknown category 1
        embed = discord.Embed(
            title="👤 Player Event",
            description=f"event_id={ev}, extra={extra}{ts_str}",
            color=discord.Color.light_gray(),
        )
        await channel.send(embed=embed)

    # --- Category 2: Rank Management ---

    async def _post_rank_event(self, channel: discord.TextChannel, event: Dict):
        ev, ts, extra = event["event_id"], event["timestamp"], event["extra"]
        ts_str = timestamp_line(ts)

        # Transfer (ev=8): [from_pid, from_hn, to_pid, to_hn]
        if ev == 8 and len(extra) >= 4:
            from_pid, from_hn, to_pid, to_hn = extra[0], extra[1], extra[2], extra[3]
            from_name = await self._resolve_name(from_pid, from_hn)
            to_name = await self._resolve_name(to_pid, to_hn)
            desc = f"**{from_name}** → **{to_name}**{ts_str}"
            embed = discord.Embed(title="🔄 Guild Transfer", description=desc, color=discord.Color.purple())
            await channel.send(embed=embed)
            return

        # Promote (ev=6) / Demote (ev=7): [actor_pid, actor_hn, target_pid, target_hn, rank_code]
        if ev in (6, 7) and len(extra) >= 5:
            actor, actor_hn, target, target_hn, rank_code = extra[0], extra[1], extra[2], extra[3], extra[4]
            role_name = get_role_name(rank_code)
            actor_name = await self._resolve_name(actor, actor_hn)
            target_name = await self._resolve_name(target, target_hn)

            if rank_code in LADDER_RANKS:
                if ev == 6:
                    title, desc = "⬆️ Promotion", f"**{actor_name}** promoted **{target_name}** → 🏅 {role_name}"
                else:
                    title, desc = "⬇️ Demotion", f"**{actor_name}** demoted **{target_name}** (was 🏅 {role_name})"
            elif rank_code in ASSIGNMENT_RANKS:
                if ev == 6:
                    title, desc = "📝 Role Mark", f"**{actor_name}** marked **{target_name}** as 🏅 {role_name}"
                else:
                    title, desc = "🗑️ Role Unmark", f"**{actor_name}** unmarked **{target_name}** (was 🏅 {role_name})"
            else:
                title, desc = "🎖️ Rank Change", f"**{actor_name}** changed **{target_name}** → 🏅 {role_name}"

            desc += ts_str
            embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
            await channel.send(embed=embed)
            return

        # Fallback for unknown rank events
        embed = discord.Embed(
            title=f"🎖️ {decode_event_type(2, ev)}",
            description=f"{str(extra)}{ts_str}",
            color=discord.Color.blue(),
        )
        await channel.send(embed=embed)

    # --- Other Categories (3, 4, 5, etc.) ---

    async def _post_other_event(self, channel: discord.TextChannel, event: Dict):
        cat, ev, ts, extra = event["category_id"], event["event_id"], event["timestamp"], event["extra"]
        ts_str = timestamp_line(ts)

        # Category 4 events (schedule/party/showdown changes) — resolve actor name
        if cat == 4 and extra and len(extra) >= 2 and isinstance(extra[0], str):
            actor_pid, actor_hn = extra[0], extra[1]
            resolved = await self._resolve_name(actor_pid, actor_hn)
            if resolved:
                if ev == 13:
                    hour = extra[2][0] if len(extra) > 2 and isinstance(extra[2], list) and len(extra[2]) >= 1 else "?"
                    desc = f"👤 **{resolved}** changed 🎉 Guild Party time to **{hour}:00**"
                elif ev == 14:
                    parts = [f"📅 {format_schedule_day(extra[i])}" for i in range(2, len(extra)) if isinstance(extra[i], list) and len(extra[i]) == 3]
                    desc = f"👤 **{resolved}** changed 🎪 Showdown to " + " and ".join(parts)
                elif ev == 15:
                    parts = []
                    for i in range(2, len(extra) - 1, 2):
                        bid, sched = extra[i], extra[i + 1]
                        if isinstance(bid, int) and isinstance(sched, list) and len(sched) == 3:
                            parts.append(f"{get_boss_name(bid)} ({format_schedule_day(sched)})")
                    desc = f"👤 **{resolved}** changed | " + " | ".join(parts)
                else:
                    desc = decode_extra(cat, ev, extra)
            else:
                desc = decode_extra(cat, ev, extra)
            desc += ts_str
        else:
            desc = (decode_extra(cat, ev, extra) if extra else "(no data)") + ts_str

        embed = discord.Embed(
            title=f"📋 {decode_event_type(cat, ev)}",
            description=desc,
            color=discord.Color.blue(),
        )
        await channel.send(embed=embed)

    # --- Name Resolution ---

    async def _resolve_name(self, pid: str, hostnum: int = 10403) -> str:
        """Get player nickname from PID using the correct hostnum."""
        if not isinstance(pid, str) or len(pid) != 16:
            return truncate_pid(pid)
        cache_key = f"{pid}@{hostnum}"
        if cache_key in self._name_cache:
            return self._name_cache[cache_key]
        name = await self.bot.loop.run_in_executor(None, resolve_player_name_sync, pid, hostnum)
        if name:
            self._name_cache[cache_key] = name
            return name
        self._name_cache[cache_key] = truncate_pid(pid)
        return truncate_pid(pid)


# -----------------------------------------------------------------------------
# Channel Selector View
# -----------------------------------------------------------------------------
class EventLogChannelSelect(discord.ui.View):
    def __init__(self, cog: EventLogCog):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.select(
        cls=ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Select a text channel...",
    )
    async def channel_select(self, interaction: discord.Interaction, select: ChannelSelect):
        channel = select.values[0]
        await set_config("channel_id", str(channel.id))
        await interaction.response.send_message(f"✅ Event log channel set to {channel.mention}", ephemeral=True)
        self.stop()


# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------
async def setup(bot: commands.Bot):
    cog = EventLogCog(bot)
    await bot.add_cog(cog)