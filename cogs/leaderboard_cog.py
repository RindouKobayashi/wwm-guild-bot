"""
Leaderboard Cog — Components V2 leaderboard with auto-refresh.
Supports MULTIPLE simultaneous leaderboards of different types.

Supports leaderboard types:
  - elegance: fashion score (from "fashion" API field)
  - martial_mastery: XIUWEI_KUNGFU (from "attr" API field)
  - exploration_mastery: XIUWEI_EXPLORE (from "attr" API field)

Architecture:
  - Admin posts a leaderboard to a channel with /leaderboard command
  - JSON file stores a list [{channel_id, message_id, type, guild_id}, ...]
  - A single background task refreshes ALL active leaderboards every 60 seconds
  - A "Check My Rank" button lets users see their position even if off-screen
"""
import discord
import json
import os
import aiosqlite
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow
from typing import Optional, List, Tuple, Dict, Any

import settings
from settings import logger, BASE_DIR, WWM_UID, WWM_REDIS_PLAYER_URL
from utility.wwm import _wwm_api_post
from cogs.view_registry import register

DB_PATH = BASE_DIR / "data" / "guild_verification.db"
CONFIG_PATH = BASE_DIR / "data" / "leaderboard_config.json"

LEADERBOARD_COLORS = {
    "elegance": 0xFF69B4,
    "martial_mastery": 0xE74C3C,
    "exploration_mastery": 0x2ECC71,
}

LEADERBOARD_EMOJIS = {
    "elegance": "💃",
    "martial_mastery": "⚔️",
    "exploration_mastery": "🗺️",
}

LB_API_FIELDS = {
    "elegance":         (["fashion", "base"], 10403),
    "martial_mastery":   (["attr", "base"], 10595),
    "exploration_mastery": (["attr", "base"], 10595),
}


def _extract_score(lb_type: str, player_data: dict) -> float:
    """Pull the correct score value from player_data for a given leaderboard type."""
    if lb_type == "elegance":
        fashion = player_data.get("fashion", {})
        if isinstance(fashion, dict):
            return fashion.get("score", 0) or 0
        return float(fashion) if isinstance(fashion, (int, float)) else 0

    attr_map = {
        "martial_mastery": "XIUWEI_KUNGFU",
        "exploration_mastery": "XIUWEI_EXPLORE",
    }
    key = attr_map.get(lb_type)
    if key:
        attr = player_data.get("attr", {})
        return round(float(attr.get(key, 0)), 1)
    return 0


# ---------------------------------------------------------------------------
# Persistent Components V2 LayoutView — the leaderboard message itself
# ---------------------------------------------------------------------------
class LeaderboardView(LayoutView):
    """The auto-refreshing leaderboard rendered with Components V2 containers."""

    def __init__(self, cog: "LeaderboardCog", lb_type: str,
                 entries: list, total_players: int, timestamp: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.lb_type = lb_type
        self.entries = entries
        self.total_players = total_players
        self.timestamp = timestamp
        self._build()

    def _build(self):
        inner: list = []
        emoji = LEADERBOARD_EMOJIS.get(self.lb_type, "🏆")
        display_name = self.lb_type.replace("_", " ").title()

        inner.append(TextDisplay(f"# {emoji} {display_name} Leaderboard\nTop players who have bound their accounts"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        lines = []
        for i, e in enumerate(self.entries[:15], 1):
            prefix = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            score_str = f"{e['score']:,}" if isinstance(e['score'], int) else str(e['score'])
            lines.append(f"{prefix} **{e['nickname']}** — `{score_str}`")
        rankings = "\n".join(lines) if lines else "*No data yet*"
        inner.append(TextDisplay(rankings))

        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        inner.append(TextDisplay(
            f"🏆 **{self.total_players}** players tracked  •  ⏱️ <t:{self.timestamp}:R>"
        ))

        row = ActionRow()
        btn = discord.ui.Button(
            label="🔍 Check My Rank",
            style=discord.ButtonStyle.primary,
            custom_id="leaderboard_check_rank",
        )
        btn.callback = self._on_check_rank
        row.add_item(btn)
        inner.append(row)

        color = LEADERBOARD_COLORS.get(self.lb_type, 0x5865F2)
        self.add_item(Container(*inner, accent_color=color))

    async def _on_check_rank(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT player_pid FROM verified_members WHERE user_id = ?",
                (interaction.user.id,)
            )
            row = await cur.fetchone()

        if not row or not row[0]:
            await interaction.followup.send(
                "❌ You haven't bound your account yet.\n"
                "Bind it in the verification channel first to see your rank!",
                ephemeral=True
            )
            return

        user_pid = row[0]

        matched = [e for e in self.entries if e["pid"] == user_pid]
        entry = matched[0] if matched else None

        if entry:
            rank = next(i for i, e in enumerate(self.entries, 1) if e["pid"] == user_pid)
            await self._send_rank_result(interaction, entry, rank)
            return

        try:
            fields, hostnum = LB_API_FIELDS.get(self.lb_type, (["attr", "base"], 10595))
            resp = _wwm_api_post(
                WWM_REDIS_PLAYER_URL,
                {
                    "fields": fields,
                    "hostnum2pids": {hostnum: [user_pid]},
                }
            )
            player_data = (resp or {}).get("result", {}).get(user_pid, {})
            if not player_data:
                await interaction.followup.send(
                    "❌ Could not fetch your character data. Try again later.",
                    ephemeral=True
                )
                return

            score = _extract_score(self.lb_type, player_data)
            nickname = player_data.get("base", {}).get("nickname", "Unknown")

            rank = 1
            for e in self.entries:
                if score >= e["score"]:
                    break
                rank += 1

            user_entry = {"pid": user_pid, "nickname": nickname, "score": score}
            await self._send_rank_result(interaction, user_entry, rank, self.total_players)

        except Exception as e:
            logger.error(f"Check-rank fetch failed: {e}")
            await interaction.followup.send(
                "❌ An error occurred. Please try again later.", ephemeral=True
            )

    async def _send_rank_result(self, interaction: discord.Interaction,
                                entry: dict, rank: int,
                                total: Optional[int] = None):
        total = total or self.total_players
        color = LEADERBOARD_COLORS.get(self.lb_type, 0x5865F2)
        emoji = LEADERBOARD_EMOJIS.get(self.lb_type, "🏆")
        display_name = self.lb_type.replace("_", " ").title()

        embed = discord.Embed(title=f"{emoji} Your {display_name} Ranking", color=color)
        score_str = f"{entry['score']:,}" if isinstance(entry['score'], int) else str(entry['score'])
        embed.add_field(name="Player", value=f"**{entry['nickname']}**", inline=True)
        embed.add_field(name="Score",  value=f"`{score_str}`", inline=True)
        embed.add_field(name="Rank",   value=f"**#{rank} / {total}**", inline=True)

        idx = rank - 1
        start = max(0, idx - 5)
        end   = min(len(self.entries), idx + 6)

        lines = []
        for i in range(start, end):
            e = self.entries[i]
            s = f"{e['score']:,}" if isinstance(e['score'], int) else str(e['score'])
            if e["pid"] == entry["pid"]:
                lines.append(f"**→ #{i+1} {e['nickname']} — {s}**")
            else:
                lines.append(f"#{i+1} {e['nickname']} — {s}")

        if lines:
            embed.add_field(name="Players Around You", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Internal model for a single leaderboard instance
# ---------------------------------------------------------------------------
class _LeaderboardInstance:
    """Tracks one active leaderboard message + channel + type."""
    def __init__(self, config: dict, cog: "LeaderboardCog"):
        self.config = config
        self.cog = cog
        self.guild_id: int = int(config["guild_id"])
        self.channel_id: int = int(config["channel_id"])
        self.lb_type: str = config.get("leaderboard_type", "elegance")
        self.message_id: Optional[int] = int(config["message_id"]) if config.get("message_id") else None
        self.channel: Optional[discord.TextChannel] = None
        self.message: Optional[discord.Message] = None

    async def resolve(self, bot: commands.Bot):
        """Resolve channel & message objects from IDs."""
        guild = bot.get_guild(self.guild_id)
        if not guild:
            return False
        self.channel = guild.get_channel(self.channel_id)
        if not self.channel:
            return False
        if self.message_id:
            try:
                self.message = await self.channel.fetch_message(self.message_id)
            except Exception:
                self.message = None
        return True


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------
class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.instances: List[_LeaderboardInstance] = []
        os.makedirs(BASE_DIR / "data", exist_ok=True)

    async def cog_load(self):
        self._load_config()
        if not self.instances:
            return

        # Resolve all saved instances
        valid = []
        for inst in self.instances:
            ok = await inst.resolve(self.bot)
            if ok:
                valid.append(inst)
            else:
                logger.warning(f"Leaderboard: discarding orphan config {inst.config}")
        self.instances = valid

        if self.instances:
            self.refresh_task.start()

    def cog_unload(self):
        if self.refresh_task.is_running():
            self.refresh_task.cancel()

    def _to_config_list(self) -> list:
        return [inst.config for inst in self.instances]

    def _load_config(self):
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH) as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    self.instances = [_LeaderboardInstance(c, self) for c in raw]
                elif isinstance(raw, dict) and raw:
                    # migrate old single-instance format
                    self.instances = [_LeaderboardInstance(raw, self)]
                else:
                    self.instances = []
                logger.info(f"Leaderboard config loaded: {len(self.instances)} instance(s)")
        except Exception as e:
            logger.error(f"Failed to load leaderboard config: {e}")
            self.instances = []

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self._to_config_list(), f, indent=2)
            logger.info(f"Leaderboard config saved: {len(self.instances)} instance(s)")
        except Exception as e:
            logger.error(f"Failed to save leaderboard config: {e}")

    def _find_entry(self, config: dict) -> Tuple[Optional[_LeaderboardInstance], int]:
        """Return (instance, index) for a matching config."""
        for idx, inst in enumerate(self.instances):
            if (inst.channel_id == int(config["channel_id"]) and
                    inst.lb_type == config["leaderboard_type"]):
                return inst, idx
        return None, -1

    # ── data fetching ──────────────────────────────────────────────────
    async def _fetch_data(self, lb_type: str) -> Tuple[List[dict], int]:
        fields, hostnum = LB_API_FIELDS.get(lb_type, (["attr", "base"], 10595))

        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT player_pid FROM verified_members WHERE player_pid IS NOT NULL"
            )
            rows = await cur.fetchall()

        all_pids = [r[0] for r in rows]
        if not all_pids:
            return [], 0

        entries_raw: List[dict] = []

        BATCH = 50
        for i in range(0, len(all_pids), BATCH):
            batch = all_pids[i:i + BATCH]
            try:
                resp = _wwm_api_post(
                    WWM_REDIS_PLAYER_URL,
                    {"fields": fields, "hostnum2pids": {hostnum: batch}},
                )
                if not resp or "result" not in resp:
                    continue

                for pid, pd in resp["result"].items():
                    if not pd:
                        continue
                    score = _extract_score(lb_type, pd)
                    if score == 0:
                        continue
                    base = pd.get("base", {})
                    entries_raw.append({
                        "pid": pid,
                        "nickname": base.get("nickname", "Unknown"),
                        "score": score,
                    })

            except Exception as e:
                logger.error(f"Batch fetch failed at offset {i}: {e}")

        entries_raw.sort(key=lambda x: x["score"], reverse=True)
        return entries_raw, len(all_pids)

    # ── publish / refresh a single instance ────────────────────────────
    async def _publish_one(self, inst: _LeaderboardInstance):
        entries, total = await self._fetch_data(inst.lb_type)
        now_ts = int(discord.utils.utcnow().timestamp())

        view = LeaderboardView(self, inst.lb_type, entries, total, now_ts)

        if inst.message:
            try:
                await inst.message.edit(content=None, embeds=[], attachments=[], view=view)
                return
            except discord.NotFound:
                inst.message = None
            except Exception as e:
                logger.error(f"Failed to edit leaderboard message: {e}")
                return

        # No existing message → send new one
        try:
            inst.message = await inst.channel.send(content=None, embeds=[], view=view)
            inst.config["message_id"] = str(inst.message.id)
            self._save_config()
        except Exception as e:
            logger.error(f"Failed to send leaderboard message: {e}")

    # ── background refresh loop ────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def refresh_task(self):
        for inst in self.instances:
            try:
                await self._publish_one(inst)
            except Exception as e:
                logger.error(f"Leaderboard refresh failed for {inst.lb_type}: {e}")
        logger.debug(f"Leaderboard refreshed ({len(self.instances)} instances)")

    @refresh_task.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # ── admin command ──────────────────────────────────────────────────
    @app_commands.command(
        name="leaderboard",
        description="Setup an auto-refreshing leaderboard in a channel"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def cmd_leaderboard(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📊 Leaderboard Setup",
            description="Select the type of leaderboard to display:",
            color=0x5865F2,
        )
        view = _TypeSelectView(self)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)


# ---------------------------------------------------------------------------
# Setup wizard views
# ---------------------------------------------------------------------------
class _TypeSelectView(discord.ui.View):
    def __init__(self, cog: LeaderboardCog):
        super().__init__(timeout=120)
        self.cog = cog
        self.config: dict = {}
        self.add_item(_TypeSelect())
        self.add_item(_CancelButton())

    async def on_type_chosen(self, interaction: discord.Interaction, lb_type: str):
        self.config["leaderboard_type"] = lb_type
        embed = discord.Embed(
            title="📊 Step 2/2 — Select Channel",
            description="Choose the channel where the leaderboard will appear:",
            color=0x5865F2,
        )
        await interaction.response.edit_message(
            embed=embed, view=_ChannelSelectView(self.cog, self.config, interaction.guild)
        )


class _TypeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select leaderboard type…",
            options=[
                discord.SelectOption(label="Elegance", description="Fashion score leaderboard",
                                     value="elegance", emoji="💃"),
                discord.SelectOption(label="Martial Mastery", description="XIUWEI_KUNGFU",
                                     value="martial_mastery", emoji="⚔️"),
                discord.SelectOption(label="Exploration Mastery", description="XIUWEI_EXPLORE",
                                     value="exploration_mastery", emoji="🗺️"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view: _TypeSelectView = self.view
        await view.on_type_chosen(interaction, self.values[0])


class _CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Setup cancelled.", embed=None, view=None)


class _ChannelSelectView(discord.ui.View):
    def __init__(self, cog: LeaderboardCog, config: dict,
                 guild: discord.Guild, page: int = 0):
        super().__init__(timeout=120)
        self.cog = cog
        self.config = config
        self.guild = guild
        self.page = page

        channels = guild.text_channels
        start = page * 25
        end = min(start + 25, len(channels))
        page_ch = channels[start:end]

        if page_ch:
            sel = discord.ui.Select(
                placeholder=f"Channel (page {page+1}/{(len(channels)-1)//25+1})…",
                options=[discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id))
                         for c in page_ch],
            )
            sel.callback = self._on_select
            self.add_item(sel)

        if page > 0:
            b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary)
            b.callback = self._prev
            self.add_item(b)

        if end < len(channels):
            b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
            b.callback = self._next
            self.add_item(b)

    async def _prev(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=_ChannelSelectView(self.cog, self.config, self.guild, self.page - 1))

    async def _next(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=_ChannelSelectView(self.cog, self.config, self.guild, self.page + 1))

    async def _on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

        channel_id = int(interaction.data["values"][0])
        guild_id = str(self.guild.id)

        # Check if an instance with same (channel, type) already exists
        for inst in self.cog.instances:
            if inst.channel_id == channel_id and inst.lb_type == self.config["leaderboard_type"]:
                await interaction.edit_original_response(
                    content="⚠️ A leaderboard of this type already exists in that channel!",
                    embed=None, view=None)
                return

        new_config = {
            "channel_id": str(channel_id),
            "guild_id": guild_id,
            "leaderboard_type": self.config["leaderboard_type"],
        }

        # Create the new instance
        inst = _LeaderboardInstance(new_config, self.cog)
        inst.channel = self.guild.get_channel(channel_id)
        ok = await inst.resolve(self.cog.bot)
        if not ok:
            await interaction.edit_original_response(
                content="❌ Could not resolve the selected channel.", embed=None, view=None)
            return

        # Start the refresh loop if not already running
        if not self.cog.refresh_task.is_running():
            self.cog.refresh_task.start()

        # Publish the first message
        await self.cog._publish_one(inst)

        # Add to instances list and save
        self.cog.instances.append(inst)
        self.cog._save_config()

        embed = discord.Embed(
            title="✅ Leaderboard Added!",
            description=(
                f"**Type:** {inst.lb_type.replace('_', ' ').title()}\n"
                f"**Channel:** <#{channel_id}>\n\n"
                f"Active leaderboards: **{len(self.cog.instances)}**\n"
                "Auto-refreshes every 60 seconds."
            ),
            color=discord.Color.green(),
        )
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        logger.info(f"Leaderboard added by {interaction.user}: {new_config}")


# ---------------------------------------------------------------------------
# Self-register persistent view for restart recovery
# ---------------------------------------------------------------------------
register(LeaderboardView, cog=None, lb_type="elegance",
         entries=[], total_players=0, timestamp=0)


# ---------------------------------------------------------------------------
# Cog entry point
# ---------------------------------------------------------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))