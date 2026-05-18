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

# -----------------------------------------------------------------------------
# Database path
# -----------------------------------------------------------------------------
DB_PATH = BASE_DIR / "data" / "event_log.db"

# -----------------------------------------------------------------------------
# Event type constants (shared with test script)
# -----------------------------------------------------------------------------
BOSS_NAMES = {
    1: "The Void King", 2: "Ye Wanshan", 3: "Lucky Seventeen",
    4: "Heartseeker", 5: "Snaker Doctor", 6: "Puppeteer",
    7: "Earth Fiend Deity", 8: "Yi Dao", 9: "Dao Lord",
    10: "Lion Dance", 11: "*BLANK*", 12: "*BLANK*",
    13: "Coffin Master", 14: "Zheng E", 15: "Drunk Martial Artist",
    16: "Ghost Master", 17: "Nameless General", 18: "Wolf Maiden",
    19: "*BLANK*", 20: "Grand Protector of Anxi", 21: "Moongazing Maiden",
    22: "Everdeer", 23: "*BLANK*", 24: "*BLANK*",
    25: "Sentinel Howlion", 26: "Pocketrupt Circus", 27: "Snowplum Requiem",
}

ROLE_NAMES = {
    1: "Guild Leader", 2: "Vice Leader", 5: "Command",
    7: "Half Time Performer", 10000: "䨻䨻䨻䨻䨻", 10001: "䨻䨻䨻䨻",
    10002: "䨻䨻䨻", 10003: "䨻䨻", 10004: "Construction", 10005: "Absent",
}

RANK_ORDER = [1, 2, 5, 7, 10000, 10001, 10002, 10003, 10004, 10005]
LADDER_RANKS = {10000, 10001, 10002, 10003}
ASSIGNMENT_RANKS = {1, 2, 5, 7, 10004, 10005}

GMT8 = timezone(timedelta(hours=8))


def format_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=GMT8).strftime("%Y-%m-%d %H:%M:%S")


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


def decode_extra(cat: int, ev: int, extra: list) -> str:
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
# Helper: fetch club event log from API using the configured full guild URL
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
    """
    Fetch a player's nickname by PID using the correct hostnum.
    The 16-char IDs are PIDs that go into hostnum2pids with the player's hostnum.
    """
    try:
        payload = {
            "fields": ["base"],
            "hostnum2pids": {
                hostnum: [pid]
            },
        }
        data = _wwm_api_post(PLAYER_INFO_URL, payload)
        if data and 'result' in data and pid in data['result']:
            base = data['result'][pid].get('base', {})
            name = base.get('nickname')
            if name:
                return name
            level = base.get('level', '?')
            return f"{truncate_pid(pid)}:Lv{level}"
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS classified_events (
                timestamp INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                classification TEXT NOT NULL,
                classified_by INTEGER,
                classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (timestamp, category_id, event_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_classifications (
                message_id INTEGER PRIMARY KEY,
                event_timestamp INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL
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


async def is_event_classified(ts: int, cat: int, ev: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT classification FROM classified_events WHERE timestamp = ? AND category_id = ? AND event_id = ?",
            (ts, cat, ev),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def save_classification(ts: int, cat: int, ev: int, classification: str, user_id: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO classified_events (timestamp, category_id, event_id, classification, classified_by) VALUES (?, ?, ?, ?, ?)",
            (ts, cat, ev, classification, user_id),
        )
        await db.commit()


async def save_pending(msg_id: int, ts: int, cat: int, ev: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_classifications (message_id, event_timestamp, category_id, event_id) VALUES (?, ?, ?, ?)",
            (msg_id, ts, cat, ev),
        )
        await db.commit()


async def get_pending_event_key(msg_id: int) -> Optional[Tuple[int, int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT event_timestamp, category_id, event_id FROM pending_classifications WHERE message_id = ?",
            (msg_id,),
        )
        row = await cursor.fetchone()
        return row if row else None


async def remove_pending(msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_classifications WHERE message_id = ?", (msg_id,))
        await db.commit()


# -----------------------------------------------------------------------------
# Persistent Views for Classification
# -----------------------------------------------------------------------------
class EventConfirmView(discord.ui.View):
    """Confirmation dialog shown after selecting a classification."""

    def __init__(self, cog, original_msg_id: int, event_ts: int, event_cat: int, event_ev: int, classification: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.original_msg_id = original_msg_id
        self.event_ts = event_ts
        self.event_cat = event_cat
        self.event_ev = event_ev
        self.classification = classification

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green, custom_id="ev_confirm_yes")
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin only", ephemeral=True)
            return
        await save_classification(self.event_ts, self.event_cat, self.event_ev, self.classification, interaction.user.id)
        # Update original message
        try:
            msg = await interaction.channel.fetch_message(self.original_msg_id)
            embed = msg.embeds[0]
            embed.add_field(name="✅ Classified", value=f"**{self.classification}**", inline=False)
            embed.color = discord.Color.green()
            await msg.edit(embed=embed, view=None)
        except Exception as e:
            logger.warning(f"Could not update classification message: {e}")
        await interaction.response.send_message(f"✅ Saved as: **{self.classification}**", ephemeral=True)
        await remove_pending(self.original_msg_id)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red, custom_id="ev_confirm_no")
    async def confirm_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin only", ephemeral=True)
            return
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


class EventClassificationView(discord.ui.View):
    """Persistent View for classifying unknown player interaction events."""

    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="✅ Approved Join", style=ButtonStyle.primary, emoji="✅", custom_id="evclass_approve")
    async def btn_approve_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_choice(interaction, "Approved Join (approve application)")

    @discord.ui.button(label="📥 Invited + Joined", style=ButtonStyle.primary, emoji="📥", custom_id="evclass_invite")
    async def btn_invite_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_choice(interaction, "Invited + Joined (invited and they accepted)")

    @discord.ui.button(label="🚫 Kicked", style=ButtonStyle.danger, emoji="🚫", custom_id="evclass_kick")
    async def btn_kicked(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_choice(interaction, "Kicked (removed from guild)")

    async def _handle_choice(self, interaction: discord.Interaction, classification: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin only", ephemeral=True)
            return
        event_key = await get_pending_event_key(interaction.message.id)
        if not event_key:
            await interaction.response.send_message("❌ This event is no longer pending classification.", ephemeral=True)
            return
        ts, cat, ev = event_key
        view2 = EventConfirmView(self.cog, interaction.message.id, ts, cat, ev, classification)
        await interaction.response.send_message(f"Classify as **{classification}**?", view=view2, ephemeral=True)


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

        for event in new_events:
            try:
                await self._post_event(channel, event)
            except Exception as e:
                logger.error(f"Failed to post event {event}: {e}")

        max_ts = max(e["timestamp"] for e in new_events)
        await set_last_timestamp(max_ts)
        return len(new_events)

    async def _post_event(self, channel: discord.TextChannel, event: Dict):
        cat, ev, ts, extra = event["category_id"], event["event_id"], event["timestamp"], event["extra"]
        time_str = format_timestamp(ts)

        if cat == 1:
            await self._post_player_event(channel, event)
            return

        if cat == 2:
            await self._post_rank_event(channel, event)
            return

        # For category 4 events (schedule changes, party time, showdown), resolve the actor name
        if cat == 4 and extra and len(extra) >= 2 and isinstance(extra[0], str):
            actor_pid = extra[0]
            actor_hostnum = extra[1]
            resolved = await self._resolve_name(actor_pid, actor_hostnum)
            if resolved:
                description = f"👤 **{resolved}** changed "
                if ev == 13:
                    hour = extra[2][0] if len(extra) > 2 and isinstance(extra[2], list) and len(extra[2]) >= 1 else "?"
                    description += f"🎉 Guild Party time to **{hour}:00**"
                elif ev == 14:
                    desc_parts = []
                    for i in range(2, len(extra)):
                        item = extra[i]
                        if isinstance(item, list) and len(item) == 3:
                            desc_parts.append(f"📅 {format_schedule_day(item)}")
                    description += f"🎪 Showdown to " + " and ".join(desc_parts)
                elif ev == 15:
                    desc_parts = []
                    for i in range(2, len(extra) - 1, 2):
                        bid = extra[i]
                        sched = extra[i + 1]
                        if isinstance(bid, int) and isinstance(sched, list) and len(sched) == 3:
                            desc_parts.append(f"{get_boss_name(bid)} ({format_schedule_day(sched)})")
                    description += " | ".join(desc_parts)
                else:
                    description = decode_extra(cat, ev, extra)
            else:
                description = decode_extra(cat, ev, extra)
        else:
            description = decode_extra(cat, ev, extra) if extra else "(no data)"

        embed = discord.Embed(
            title=f"📋 {decode_event_type(cat, ev)}",
            description=description,
            color=discord.Color.blue(),
            timestamp=datetime.fromtimestamp(ts, tz=GMT8),
        )
        embed.set_footer(text=f"UTC+8 • {time_str}")
        await channel.send(embed=embed)

    async def _post_player_event(self, channel: discord.TextChannel, event: Dict):
        cat, ev, ts, extra = event["category_id"], event["event_id"], event["timestamp"], event["extra"]
        time_str = format_timestamp(ts)

        if ev == 2 and len(extra) >= 2:
            pid, hostnum = extra[0], extra[1]
            name = await self._resolve_name(pid, hostnum)
            embed = discord.Embed(
                title="👋 Player Left Guild",
                description=f"**{name}**",
                color=discord.Color.orange(),
                timestamp=datetime.fromtimestamp(ts, tz=GMT8),
            )
            embed.set_footer(text=f"UTC+8 • {time_str}")
            await channel.send(embed=embed)
            return

        if ev in (3, 4, 5) and len(extra) >= 4:
            actor, actor_hostnum, target, target_hostnum = extra[0], extra[1], extra[2], extra[3]
            existing = await is_event_classified(ts, cat, ev)
            actor_name = await self._resolve_name(actor, actor_hostnum)
            target_name = await self._resolve_name(target, target_hostnum)

            if existing:
                embed = discord.Embed(
                    title=f"👥 {existing}",
                    description=f"**{actor_name}** → **{target_name}**",
                    color=discord.Color.green(),
                    timestamp=datetime.fromtimestamp(ts, tz=GMT8),
                )
                embed.set_footer(text=f"UTC+8 • {time_str}")
                embed.add_field(name="✅ Classified", value=existing, inline=False)
                await channel.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="❓ Unknown Player Interaction",
                    description=f"**{actor_name}**\n→\n**{target_name}**",
                    color=discord.Color.yellow(),
                    timestamp=datetime.fromtimestamp(ts, tz=GMT8),
                )
                embed.set_footer(text=f"UTC+8 • {time_str}")
                embed.add_field(
                    name="What happened?",
                    value="An admin needs to classify this event using the buttons below.",
                    inline=False,
                )
                view = EventClassificationView(self)
                msg = await channel.send(embed=embed, view=view)
                await save_pending(msg.id, ts, cat, ev)
            return

        embed = discord.Embed(
            title="👤 Player Event",
            description=f"event_id={ev}, extra={extra}",
            color=discord.Color.light_gray(),
            timestamp=datetime.fromtimestamp(ts, tz=GMT8),
        )
        embed.set_footer(text=f"UTC+8 • {time_str}")
        await channel.send(embed=embed)

    async def _post_rank_event(self, channel: discord.TextChannel, event: Dict):
        ev, ts, extra = event["event_id"], event["timestamp"], event["extra"]
        time_str = format_timestamp(ts)

        if ev == 8 and len(extra) >= 4:
            from_pid, from_hostnum, to_pid, to_hostnum = extra[0], extra[1], extra[2], extra[3]
            from_name = await self._resolve_name(from_pid, from_hostnum)
            to_name = await self._resolve_name(to_pid, to_hostnum)
            if from_name and to_name:
                desc = f"**{from_name}** → **{to_name}**"
            else:
                desc = f"**{from_name or truncate_pid(from_pid)}** → **{to_name or truncate_pid(to_pid)}**"
            embed = discord.Embed(
                title="🔄 Guild Transfer",
                description=desc,
                color=discord.Color.purple(),
                timestamp=datetime.fromtimestamp(ts, tz=GMT8),
            )
            embed.set_footer(text=f"UTC+8 • {time_str}")
            await channel.send(embed=embed)
            return

        if ev in (6, 7) and len(extra) >= 5:
            actor, actor_hostnum, target, target_hostnum, rank_code = extra[0], extra[1], extra[2], extra[3], extra[4]
            role_name = get_role_name(rank_code)
            actor_name, target_name = await self._resolve_name(actor, actor_hostnum), await self._resolve_name(target, target_hostnum)

            if rank_code in LADDER_RANKS:
                title, desc = ("⬆️ Promotion", f"**{actor_name}** promoted **{target_name}** → 🏅 {role_name}") if ev == 6 else ("⬇️ Demotion", f"**{actor_name}** demoted **{target_name}** (was 🏅 {role_name})")
            elif rank_code in ASSIGNMENT_RANKS:
                title, desc = ("📝 Role Mark", f"**{actor_name}** marked **{target_name}** as 🏅 {role_name}") if ev == 6 else ("🗑️ Role Unmark", f"**{actor_name}** unmarked **{target_name}** (was 🏅 {role_name})")
            else:
                title, desc = "🎖️ Rank Change", f"**{actor_name}** changed **{target_name}** → 🏅 {role_name}"

            embed = discord.Embed(title=title, description=desc, color=discord.Color.gold(), timestamp=datetime.fromtimestamp(ts, tz=GMT8))
            embed.set_footer(text=f"UTC+8 • {time_str}")
            await channel.send(embed=embed)
            return

        embed = discord.Embed(title=f"🎖️ {decode_event_type(2, ev)}", description=str(extra), color=discord.Color.blue(), timestamp=datetime.fromtimestamp(ts, tz=GMT8))
        embed.set_footer(text=f"UTC+8 • {time_str}")
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