import discord
import datetime
import aiosqlite
import json
import logging
from collections import defaultdict
from typing import Optional, List, Tuple, Dict, Any, Set
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow, Button, Modal, TextInput, Select

import settings
from settings import BASE_DIR, CLUB_ID, WWM_UID, logger, GMT8_TZ
from utility.wwm import get_full_guild_info, get_bulk_hoard_data, get_bulk_players_info, _wwm_api_post, get_topics_likes

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = BASE_DIR / "data" / "market.db"
VERIFICATION_DB_PATH = BASE_DIR / "data" / "guild_verification.db"
ADMIN_AVATAR_CHANNEL_ID = 1500005539256602774  # Existing admin channel for avatar approvals
ACCENT_GREEN = 0x2ECC71
ACCENT_BLURPLE = 0x5865F2
ACCENT_RED = 0xE74C3C
ACCENT_ORANGE = 0xE67E22

GOOD_EMOJIS = ["🟢", "🔵", "🟣", "🟡", "🔴", "🟠", "💠", "⭐"]

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _pct_str(original: float, current: float) -> Tuple[str, float]:
    """Return (formatted percentage string, raw percentage)."""
    if original == 0:
        return "N/A", 0.0
    pct = ((current - original) / original) * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%", pct


# ---------------------------------------------------------------------------
# Good Name Suggestion Modal
# ---------------------------------------------------------------------------
class GoodNameModal(Modal, title="Suggest a Good Name"):
    """Modal that lets a user suggest a name for a good ID."""

    name_input = TextInput(
        label="Good Name",
        placeholder="e.g. Silk, Vinegar, Charcoal...",
        required=True,
        min_length=1,
        max_length=100,
    )

    def __init__(self, good_id: str, cog: "MarketCog"):
        super().__init__(timeout=300)
        self.good_id = good_id
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        suggested_name = self.name_input.value.strip()
        if not suggested_name:
            await interaction.response.send_message("❌ Name cannot be empty.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Send to admin channel for approval
        admin_channel_id = getattr(self.cog, '_admin_channel_id', None) or ADMIN_AVATAR_CHANNEL_ID
        admin_channel = interaction.guild.get_channel(admin_channel_id)
        if not admin_channel:
            await interaction.followup.send("❌ Admin channel not configured.", ephemeral=True)
            return

        view = GoodNameAdminConfirmView(
            cog=self.cog,
            good_id=self.good_id,
            suggested_name=suggested_name,
            suggested_by=interaction.user,
        )

        container = Container(
            TextDisplay(
                f"# 🏷️ Good Name Suggestion\n\n"
                f"• **Good ID:** `{self.good_id}`\n"
                f"• **Suggested Name:** `{suggested_name}`\n"
                f"• **Suggested by:** {interaction.user.mention} (`{interaction.user}`)\n\n"
                f"Approve to save this mapping and refresh the dashboard."
            ),
            Separator(spacing=discord.SeparatorSpacing.small),
            accent_color=ACCENT_ORANGE,
        )
        msg_view = LayoutView(timeout=None)
        msg_view.add_item(container)
        msg_view.add_item(view.action_row)

        await admin_channel.send(view=msg_view)
        await interaction.followup.send(
            f"✅ Name suggestion `{suggested_name}` for Good `{self.good_id}` sent to admins for approval.",
            ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"GoodNameModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Good Name Admin Confirm View
# ---------------------------------------------------------------------------
class GoodNameAdminConfirmView:
    """Not a real LayoutView — just holds the action_row for admin approve/reject.
    We attach this row to the admin message's LayoutView."""

    def __init__(self, cog: "MarketCog", good_id: str, suggested_name: str, suggested_by: discord.abc.User):
        self.cog = cog
        self.good_id = good_id
        self.suggested_name = suggested_name
        self.suggested_by = suggested_by

        self.action_row = ActionRow()
        approve_btn = Button(
            label="✅ Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"goodname_approve:{good_id}:{suggested_name[:50]}",
        )
        approve_btn.callback = self._on_approve
        self.action_row.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"goodname_reject:{good_id}",
        )
        reject_btn.callback = self._on_reject
        self.action_row.add_item(reject_btn)

    def _disable(self):
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    async def _on_approve(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer()

        # Save to good_names table
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "REPLACE INTO good_names (good_id, name, approved_by, approved_at) VALUES (?, ?, ?, ?)",
                (self.good_id, self.suggested_name, interaction.user.id, int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
            )
            await db.commit()

        self._disable()
        summary = Container(
            TextDisplay(
                f"✅ **Good Name Approved** by {interaction.user.mention}\n\n"
                f"• `{self.good_id}` → **{self.suggested_name}**\n"
                f"• Suggested by: {self.suggested_by.mention}"
            ),
            accent_color=ACCENT_GREEN,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(summary)
        await interaction.edit_original_response(view=done_view)

        logger.info(f"Good name approved: {self.good_id} -> {self.suggested_name} (by {interaction.user})")

        # Force refresh the dashboard to show the new name
        await self.cog._refresh_dashboard()

    async def _on_reject(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer()

        self._disable()
        summary = Container(
            TextDisplay(
                f"❌ **Good Name Rejected** by {interaction.user.mention}\n\n"
                f"• `{self.good_id}` → ~~{self.suggested_name}~~\n"
                f"• Suggested by: {self.suggested_by.mention}"
            ),
            accent_color=ACCENT_RED,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(summary)
        await interaction.edit_original_response(view=done_view)
        logger.info(f"Good name rejected: {self.good_id} -> {self.suggested_name} (by {interaction.user})")


# ---------------------------------------------------------------------------
# Market Player Admin Confirm View
# ---------------------------------------------------------------------------
class MarketPlayerAdminConfirmView:
    """Holds the action_row for admin approve/reject of adding a player to the watchlist."""

    def __init__(self, cog: "MarketCog", pid: str, nickname: str, number_id: str, suggested_by: discord.abc.User):
        self.cog = cog
        self.pid = pid
        self.nickname = nickname
        self.number_id = number_id
        self.suggested_by = suggested_by

        self.action_row = ActionRow()
        approve_btn = Button(
            label="✅ Approve – Add to Report",
            style=discord.ButtonStyle.success,
            custom_id=f"marketplayer_approve:{pid[:20]}",
        )
        approve_btn.callback = self._on_approve
        self.action_row.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"marketplayer_reject:{pid[:20]}",
        )
        reject_btn.callback = self._on_reject
        self.action_row.add_item(reject_btn)

    def _disable(self):
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    async def _on_approve(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer()

        # Add to watchlist
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "REPLACE INTO market_watchlist (pid, nickname, number_id, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                (self.pid, self.nickname, self.number_id, interaction.user.id, int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
            )
            await db.commit()

        self._disable()
        summary = Container(
            TextDisplay(
                f"✅ **Player Added to Market Report** by {interaction.user.mention}\n\n"
                f"• **{self.nickname}** ({self.number_id})\n"
                f"• PID: `{self.pid}`\n"
                f"• Suggested by: {self.suggested_by.mention}\n\n"
                f"Refreshing dashboard..."
            ),
            accent_color=ACCENT_GREEN,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(summary)
        await interaction.edit_original_response(view=done_view)

        logger.info(f"Market player watchlist add: {self.nickname} ({self.pid}) by {interaction.user}")

        # Force refresh dashboard to potentially include this player
        await self.cog._refresh_dashboard()

    async def _on_reject(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer()

        self._disable()
        summary = Container(
            TextDisplay(
                f"❌ **Player Rejected from Market Report** by {interaction.user.mention}\n\n"
                f"• **{self.nickname}** ({self.number_id})\n"
                f"• Suggested by: {self.suggested_by.mention}"
            ),
            accent_color=ACCENT_RED,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(summary)
        await interaction.edit_original_response(view=done_view)
        logger.info(f"Market player watchlist reject: {self.nickname} ({self.pid}) by {interaction.user}")


# ---------------------------------------------------------------------------
# Market Player View (wraps player stats with buttons)
# ---------------------------------------------------------------------------
class MarketPlayerView(LayoutView):
    """Components V2 LayoutView that shows player market stats + action buttons."""

    def __init__(
        self,
        cog: "MarketCog",
        pid: str,
        nickname: str,
        number_id: str,
        main_good: str,
        price_history: List[int],
        total_profit: int,
        is_on_watchlist: bool = False,
        good_has_name: bool = False,
        good_name: str = "",
        likes: int = 0,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.pid = pid
        self.nickname = nickname
        self.number_id = number_id
        self.main_good = main_good

        inner: list = []

        # Stats text
        lines = [f"# 📈 Market Stats — {nickname}"]
        lines.append(f"**Number ID:** `{number_id}`")
        good_label = f"{good_name} (#{main_good})" if good_name else f"#{main_good}"
        lines.append(f"**Main Good:** `{good_label}`")
        if price_history:
            original = price_history[0]
            current = price_history[-1]
            pct_str, _ = _pct_str(original, current)
            lines.append(f"**Price:** `{original}` → `{current}` ({pct_str})")
            lines.append("**History:** `" + " → ".join(str(p) for p in price_history) + "`")
        lines.append(f"**💰 Total Profit:** `{total_profit:,}`")
        if likes > 0:
            lines.append(f"**👍 Market Likes:** `{likes}`")
        inner.append(TextDisplay("\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Action buttons row
        action_row = ActionRow()

        include_btn = Button(
            label="📋 Include in Report" if not is_on_watchlist else "✅ Already in Report",
            style=discord.ButtonStyle.primary if not is_on_watchlist else discord.ButtonStyle.secondary,
            custom_id=f"market_include_player:{pid[:20]}",
            disabled=is_on_watchlist,
        )
        include_btn.callback = self._on_include
        action_row.add_item(include_btn)

        if main_good and main_good != '?' and not good_has_name:
            suggest_name_btn = Button(
                label="🏷️ Suggest Name",
                style=discord.ButtonStyle.success,
                custom_id=f"market_suggest_name:{main_good}",
            )
            suggest_name_btn.callback = self._on_suggest_name
            action_row.add_item(suggest_name_btn)

        inner.append(action_row)
        container = Container(*inner, accent_color=ACCENT_GREEN)
        self.add_item(container)

    async def _on_include(self, interaction: discord.Interaction):
        # Send approval request to admin channel
        admin_channel_id = getattr(self.cog, '_admin_channel_id', None) or ADMIN_AVATAR_CHANNEL_ID
        admin_channel = interaction.guild.get_channel(admin_channel_id)
        if not admin_channel:
            await interaction.response.send_message("❌ Admin channel not configured.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        view = MarketPlayerAdminConfirmView(
            cog=self.cog,
            pid=self.pid,
            nickname=self.nickname,
            number_id=self.number_id,
            suggested_by=interaction.user,
        )

        # Format price info
        good_label = f"{self.good_name} (#{self.main_good})" if self.good_name else f"Good #{self.main_good}"
        price_line = f"`{self.original_price:.0f}` → `{self.current_price:.0f}`"
        sign = "+" if self.pct >= 0 else ""
        history_line = " → ".join(str(p) for p in self.price_history) if self.price_history else ""

        container = Container(
            TextDisplay(
                f"# 📋 Add Player to Market Report\n\n"
                f"• **{self.nickname}** (`{self.number_id}`)\n"
                f"• **{good_label}**\n"
                f"• Price: {price_line}  ({sign}{self.pct:.2f}%)\n"
                + (f"• History: `{history_line}`\n" if history_line else "")
                + f"\n• Requested by: {interaction.user.mention}\n\n"
                f"Approve to include this player in the daily market report dashboard."
            ),
            Separator(spacing=discord.SeparatorSpacing.small),
            accent_color=ACCENT_ORANGE,
        )
        msg_view = LayoutView(timeout=None)
        msg_view.add_item(container)
        msg_view.add_item(view.action_row)

        await admin_channel.send(view=msg_view)
        await interaction.followup.send(
            f"✅ Approval request sent to admins for including **{self.nickname}** in the market report.",
            ephemeral=True
        )

    async def _on_suggest_name(self, interaction: discord.Interaction):
        modal = GoodNameModal(good_id=self.main_good, cog=self.cog)
        await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# Market Report View (Components V2) — updated with Suggest Name buttons
# ---------------------------------------------------------------------------
class MarketReportView(LayoutView):
    """Components V2 LayoutView for the daily market price report."""

    def __init__(
        self,
        cog: "MarketCog",
        grouped_data: Dict[str, List[Tuple[str, str, str, float, float, float, bool, int]]],
        total_players: int,
        report_ts: int,
        next_update_ts: int,
        known_goods: Set[str] = None,
        good_names_map: Dict[str, str] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.grouped_data = grouped_data
        self.total_players = total_players
        self.report_ts = report_ts
        self.next_update_ts = next_update_ts
        self.known_goods = known_goods or set()
        self.good_names_map = good_names_map or {}

        inner_items: list = []

        # Title
        inner_items.append(TextDisplay(
            f"# 📈 Market Price Report\n"
            f"Tracking **{total_players}** players across **{len(grouped_data)}** goods"
        ))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Sort goods by their ID for consistent ordering
        sorted_goods = sorted(grouped_data.keys(), key=lambda gid: int(gid))

        for idx, good_id in enumerate(sorted_goods):
            players = grouped_data[good_id]
            emoji = GOOD_EMOJIS[idx % len(GOOD_EMOJIS)]
            good_name = self.good_names_map.get(good_id, "")
            label = f"{good_name} (#{good_id})" if good_name else f"Good #{good_id}"

            # Build leaderboard lines
            lines = []
            for rank, (pid, nickname, number_id, original_price, current_price, pct, is_online, _hostnum) in enumerate(players[:10], 1):
                if rank == 1:
                    prefix = "🥇"
                elif rank == 2:
                    prefix = "🥈"
                elif rank == 3:
                    prefix = "🥉"
                else:
                    prefix = f"`{rank}.`"

                sign = "+" if pct >= 0 else ""
                online_icon = "🟢" if is_online else "⚫"
                lines.append(
                    f"{prefix} {online_icon} **{nickname}** ({number_id})  ─  "
                    f"`{original_price:.0f}` → `{current_price:.0f}`  │  **{sign}{pct:.2f}%**"
                )

            body = "\n".join(lines) if lines else "*No data available*"
            header_text = f"### {emoji} {label} — Top {min(len(players), 10)}"
            inner_items.append(TextDisplay(f"{header_text}\n{body}"))

            # Suggest Name button row for goods without a name
            if good_id not in self.known_goods:
                name_row = ActionRow()
                suggest_btn = Button(
                    label="🏷️ Suggest Name",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"market_report_suggest_name:{good_id}",
                )
                suggest_btn.callback = self._make_suggest_callback(good_id)
                name_row.add_item(suggest_btn)
                inner_items.append(name_row)

            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Footer
        inner_items.append(TextDisplay(
            f"📊 Report generated: <t:{report_ts}:R>  •  🔄 Updates every 10 minutes"
        ))

        container = Container(*inner_items, accent_color=ACCENT_GREEN)
        self.add_item(container)

    def _make_suggest_callback(self, good_id: str):
        async def callback(interaction: discord.Interaction):
            modal = GoodNameModal(good_id=good_id, cog=self.cog)
            await interaction.response.send_modal(modal)
        return callback


# ---------------------------------------------------------------------------
# Market Cog
# ---------------------------------------------------------------------------
class MarketCog(commands.Cog):
    """Tracks market price changes and posts daily rankings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH
        self.market_channel = None
        self.last_report_message = None
        self._admin_channel_id = ADMIN_AVATAR_CHANNEL_ID

    # -- Command groups ----------------------------------------------------
    market_group = app_commands.Group(
        name="market",
        description="Market price report commands"
    )

    # -- Database ----------------------------------------------------------
    async def _init_database(self):
        (BASE_DIR / "data").mkdir(exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_history (
                    ts INTEGER PRIMARY KEY,
                    report_json TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS good_names (
                    good_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    approved_by INTEGER,
                    approved_at INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS good_name_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    good_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    suggested_by INTEGER,
                    status TEXT DEFAULT 'pending',
                    reviewed_by INTEGER,
                    created_at INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_watchlist (
                    pid TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    number_id TEXT NOT NULL DEFAULT '',
                    added_by INTEGER,
                    added_at INTEGER
                )
            """)
            await db.commit()

    async def _load_config(self):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT key, value FROM market_config")
            rows = await cursor.fetchall()
            config = {row[0]: row[1] for row in rows}

            if 'channel_id' in config:
                channel = self.bot.get_channel(int(config['channel_id']))
                self.market_channel = channel
            if 'message_id' in config and self.market_channel:
                try:
                    self.last_report_message = await self.market_channel.fetch_message(int(config['message_id']))
                except Exception:
                    self.last_report_message = None
            if 'admin_channel_id' in config:
                self._admin_channel_id = int(config['admin_channel_id'])

    async def _save_config(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "REPLACE INTO market_config VALUES ('channel_id', ?)",
                (str(self.market_channel.id) if self.market_channel else None,)
            )
            await db.execute(
                "REPLACE INTO market_config VALUES ('message_id', ?)",
                (str(self.last_report_message.id) if self.last_report_message else None,)
            )
            await db.execute(
                "REPLACE INTO market_config VALUES ('admin_channel_id', ?)",
                (str(self._admin_channel_id),)
            )
            await db.commit()

    async def _get_known_goods(self) -> Set[str]:
        """Return the set of good_ids that have approved names."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT good_id FROM good_names")
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def _get_watchlist_pids(self) -> Set[str]:
        """Return the set of PIDs on the watchlist."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT pid FROM market_watchlist")
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def _get_good_name(self, good_id: str) -> Optional[str]:
        """Return the approved name for a good ID, or None."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT name FROM good_names WHERE good_id = ?", (good_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    def _good_label_sync(self, good_id: str, known_goods: Set[str]) -> str:
        """Synchronous version — just checks a pre-fetched set."""
        if good_id in known_goods:
            return f"#{good_id}"  # Known goods already have approved names, show just the ID
        return f"Good #{good_id}"

    async def _good_label(self, good_id: str) -> str:
        """Async version — checks the DB."""
        name = await self._get_good_name(good_id)
        if name:
            return f"{name} (#{good_id})"
        return f"Good #{good_id}"

    # -- Cog lifecycle ----------------------------------------------------
    async def cog_load(self):
        await self._init_database()
        await self._load_config()
        if not self.daily_market_report.is_running():
            self.daily_market_report.start()

    async def cog_unload(self):
        if self.daily_market_report.is_running():
            self.daily_market_report.cancel()

    # -- Force refresh ----------------------------------------------------
    async def _refresh_dashboard(self):
        """Force refresh the market report dashboard message (if it exists)."""
        try:
            await self._build_and_send_report()
        except Exception as e:
            logger.error(f"Market cog: dashboard refresh failed: {e}", exc_info=True)

    # -- Core data fetching ------------------------------------------------
    async def _get_all_player_pids(self) -> List[str]:
        """Collect PIDs from guild members + bound players + watchlist, deduplicated."""
        guild_pids = set()
        try:
            guild_data = get_full_guild_info(CLUB_ID)
            if guild_data and 'result' in guild_data:
                members = guild_data['result'].get('members', {}).get('members', {})
                guild_pids.update(members.keys())
        except Exception as e:
            logger.error(f"Market cog: failed to fetch guild members: {e}")

        bound_pids = set()
        try:
            async with aiosqlite.connect(VERIFICATION_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT player_pid FROM verified_members WHERE player_pid IS NOT NULL"
                )
                rows = await cursor.fetchall()
                bound_pids.update(row[0] for row in rows)
        except Exception as e:
            logger.error(f"Market cog: failed to fetch bound players: {e}")

        watchlist_pids = await self._get_watchlist_pids()

        all_pids = list(guild_pids | bound_pids | watchlist_pids)
        logger.debug(f"Market cog: collected {len(guild_pids)} guild + {len(bound_pids)} bound + {len(watchlist_pids)} watchlist = {len(all_pids)} unique PIDs")
        return all_pids

    @staticmethod
    def _get_week_start_ts() -> int:
        """Return the UNIX timestamp of the current schedule week start (Monday 5am GMT+8)."""
        GMT8_OFFSET = 8 * 3600
        now_utc_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        gmt8_now_ts = now_utc_ts + GMT8_OFFSET
        gmt8_dt = datetime.datetime.fromtimestamp(gmt8_now_ts, tz=datetime.timezone.utc)
        adjusted_dt = gmt8_dt - datetime.timedelta(hours=5)
        monday_dt = adjusted_dt - datetime.timedelta(days=adjusted_dt.weekday())
        week_start = monday_dt.replace(hour=5, minute=0, second=0, microsecond=0)
        return int(week_start.timestamp() - GMT8_OFFSET)

    async def _fetch_and_process(self) -> Optional[Dict[str, Any]]:
        """Fetch hoard data, group by main_good, calculate percentages.
        
        Only includes players whose data is fresh (online this week),
        OR who are on the watchlist, to avoid stale data >3 goods.
        
        Returns dict: { good_id: [(pid, nickname, number_id, original, current, pct), ...] }
        Or None if no data.
        """
        all_pids = await self._get_all_player_pids()
        if not all_pids:
            logger.warning("Market cog: no player PIDs to fetch")
            return None

        watchlist = await self._get_watchlist_pids()

        raw_data = get_bulk_hoard_data(all_pids)
        if not raw_data or 'result' not in raw_data:
            logger.warning("Market cog: bulk hoard fetch returned no data")
            return None

        players_data = raw_data['result']
        if not players_data:
            logger.warning("Market cog: empty player data from bulk hoard fetch")
            return None

        week_start_ts = self._get_week_start_ts()

        good_groups: Dict[str, List[Tuple[str, str, str, float, float, float, bool, int]]] = defaultdict(list)
        skipped_none = 0
        skipped_stale = 0

        for pid, player_entry in players_data.items():
            base = player_entry.get('base', {}) if isinstance(player_entry, dict) else {}
            nickname = base.get('nickname', 'Unknown')
            number_id = str(base.get('number_id', '')) if base.get('number_id') else ''
            hostnum = int(base.get('hostnum', 10595)) if base.get('hostnum') else 10595

            # Freshness check: skip stale data, UNLESS on watchlist
            is_online = base.get('is_online', 0) == 1
            logout_time = base.get('logout_time', 0) or base.get('last_online_ts', 0)
            if not is_online and logout_time < week_start_ts and pid not in watchlist:
                skipped_stale += 1
                continue

            hoard = player_entry.get('hoard_profiteer', {}) if isinstance(player_entry, dict) else {}
            if not hoard:
                skipped_none += 1
                continue

            main_good = str(hoard.get('main_good', ''))
            if not main_good:
                skipped_none += 1
                continue

            price_history = hoard.get('price_change_history', [])
            if len(price_history) < 2:
                skipped_none += 1
                continue

            original_price = float(price_history[0])
            current_price = float(price_history[-1])

            if original_price == 0:
                skipped_none += 1
                continue

            pct = ((current_price - original_price) / original_price) * 100.0

            good_groups[main_good].append((
                pid, nickname, number_id,
                original_price, current_price, pct,
                is_online, hostnum
            ))

        logger.debug(
            f"Market cog: processed {len(players_data)} players — "
            f"{skipped_none} no hoard data, {skipped_stale} stale (not seen this week), "
            f"{sum(len(v) for v in good_groups.values())} included"
        )

        if not good_groups:
            logger.warning("Market cog: no players with valid hoard data found")
            return None

        for good_id in good_groups:
            good_groups[good_id].sort(key=lambda x: x[5], reverse=True)

        unique_goods = list(good_groups.keys())
        logger.debug(f"Market cog: found {len(unique_goods)} unique goods: {unique_goods}")
        if len(unique_goods) > 3:
            logger.warning(f"Market cog: expected at most 3 unique goods but found {len(unique_goods)}")

        return dict(good_groups)

    # -- Build and send report --------------------------------------------
    async def _build_and_send_report(self):
        """Fetch data, build view, send/edit to channel."""
        if not self.market_channel:
            logger.warning("Market cog: no channel configured, cannot send report")
            return

        grouped = await self._fetch_and_process()
        if not grouped:
            await self.market_channel.send("❌ No market data available to report.")
            return

        known_goods = await self._get_known_goods()
        # Build a map of good_id -> approved name for display
        good_names_map = {}
        for gid in known_goods:
            name = await self._get_good_name(gid)
            if name:
                good_names_map[gid] = name

        total_players = sum(len(v) for v in grouped.values())
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        next_update_ts = self._get_next_update_ts()

        view = MarketReportView(
            cog=self,
            grouped_data=grouped,
            total_players=total_players,
            report_ts=now_ts,
            next_update_ts=next_update_ts,
            known_goods=known_goods,
            good_names_map=good_names_map,
        )

        try:
            if self.last_report_message:
                await self.last_report_message.edit(content=None, embeds=[], attachments=[], view=view)
            else:
                self.last_report_message = await self.market_channel.send(view=view)
        except Exception:
            self.last_report_message = await self.market_channel.send(view=view)

        await self._save_config()

    @staticmethod
    def _get_next_update_ts() -> int:
        """Return the UNIX timestamp of the next 6am GMT+8."""
        now = datetime.datetime.now(GMT8_TZ)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        return int(target.timestamp())

    async def _fetch_market_likes(self, pid: str, hostnum: int = 10595) -> int:
        """Fetch market likes (topic 129) for a single player. Returns n_likes."""
        try:
            result = get_topics_likes(pid, hostnum)
            if result and 'result' in result:
                likes_info = result['result']
                if isinstance(likes_info, dict):
                    topic_129 = likes_info.get(129)
                    if isinstance(topic_129, dict):
                        return topic_129.get('n_likes', 0)
        except Exception as e:
            logger.debug(f"Market cog: failed to fetch likes for PID {pid}: {e}")
        return 0

    async def _fetch_all_market_likes(self, grouped: Dict[str, List], max_per_good: int = 10) -> Dict[str, int]:
        """Fetch market likes for the top players in each good group.
        Returns dict mapping pid -> n_likes."""
        likes_map: Dict[str, int] = {}
        # Collect unique (pid, hostnum) from top players only
        seen: Set[Tuple[str, int]] = set()
        for good_id, players in grouped.items():
            for player in players[:max_per_good]:
                pid = player[0]
                hostnum = player[7] if len(player) > 7 else 10595
                if pid not in {s[0] for s in seen}:
                    seen.add((pid, hostnum))
        # Fetch likes for all top players with correct hostnum
        for pid, hostnum in seen:
            likes_map[pid] = await self._fetch_market_likes(pid, hostnum)
        return likes_map

    # -- Scheduled task ---------------------------------------------------
    @tasks.loop(minutes=10)
    async def daily_market_report(self):
        """Market report refreshes every 10 minutes."""
        logger.debug("Market cog: running report refresh")
        try:
            await self._build_and_send_report()
        except Exception as e:
            logger.error(f"Market cog: report refresh failed: {e}", exc_info=True)

    @daily_market_report.before_loop
    async def before_daily_market_report(self):
        await self.bot.wait_until_ready()

        if self.last_report_message:
            return

        if self.market_channel:
            try:
                await self._build_and_send_report()
            except Exception as e:
                logger.error(f"Market cog: initial report build failed: {e}")

    # -- Slash commands ---------------------------------------------------
    @market_group.command(name="report", description="Force-trigger a fresh market price report")
    @app_commands.checks.has_permissions(administrator=True)
    async def market_report(self, interaction: discord.Interaction):
        """Manually trigger a market report."""
        await interaction.response.defer()
        try:
            await self._build_and_send_report()
            await interaction.followup.send("✅ Market report generated and posted.", ephemeral=True)
        except Exception as e:
            logger.error(f"Market cog: manual report failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to generate report: `{e}`", ephemeral=True)

    @market_group.command(name="set-channel", description="Set the channel for daily market reports")
    @app_commands.checks.has_permissions(administrator=True)
    async def market_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Configure the channel where daily reports are posted."""
        self.market_channel = channel
        await self._save_config()
        await interaction.response.send_message(f"✅ Market report channel set to {channel.mention}", ephemeral=True)
        logger.info(f"Market cog: channel set to {channel.id} by {interaction.user}")

    @market_group.command(name="set-admin-channel", description="Set the admin channel for market approvals")
    @app_commands.checks.has_permissions(administrator=True)
    async def market_set_admin_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Configure the admin channel for market approval requests."""
        self._admin_channel_id = channel.id
        await self._save_config()
        await interaction.response.send_message(f"✅ Market admin channel set to {channel.mention}", ephemeral=True)
        logger.info(f"Market cog: admin channel set to {channel.id} by {interaction.user}")

    @market_group.command(name="status", description="Check market report configuration")
    async def market_status(self, interaction: discord.Interaction):
        """Show current market report config."""
        embed = discord.Embed(title="📈 Market Report Status", color=ACCENT_BLURPLE)
        embed.add_field(
            name="Channel",
            value=self.market_channel.mention if self.market_channel else "❌ Not set",
            inline=True
        )
        embed.add_field(
            name="Admin Channel",
            value=f"<#{self._admin_channel_id}>" if self._admin_channel_id else "❌ Not set",
            inline=True
        )
        embed.add_field(
            name="Schedule",
            value="Every 10 minutes" if self.daily_market_report.is_running() else "❌ Stopped",
            inline=True
        )
        embed.add_field(
            name="Message",
            value=f"[View latest]({self.last_report_message.jump_url})" if self.last_report_message else "None",
            inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @market_group.command(name="watchlist", description="Show/remove manually added players on the market watchlist")
    @app_commands.checks.has_permissions(administrator=True)
    async def market_watchlist(self, interaction: discord.Interaction):
        """Show all manually-added players on the watchlist, with a Select to remove entries."""
        await interaction.response.defer()
        try:
            # Get guild + bound PIDs to filter out
            guild_pids = set()
            bound_pids = set()
            try:
                guild_data = get_full_guild_info(CLUB_ID)
                if guild_data and 'result' in guild_data:
                    members = guild_data['result'].get('members', {}).get('members', {})
                    guild_pids.update(members.keys())
            except Exception:
                pass
            try:
                async with aiosqlite.connect(VERIFICATION_DB_PATH) as conn:
                    cursor = await conn.execute(
                        "SELECT player_pid FROM verified_members WHERE player_pid IS NOT NULL"
                    )
                    bound_pids.update(row[0] for row in await cursor.fetchall())
            except Exception:
                pass

            # Get full watchlist with metadata
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT pid, nickname, number_id, added_by, added_at FROM market_watchlist ORDER BY added_at DESC"
                )
                rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send("📋 Watchlist is empty — no manually added players.")
                return

            # Build lists
            manual_only = []
            also_auto = []
            for row in rows:
                entry = {
                    'pid': row['pid'],
                    'nickname': row['nickname'],
                    'number_id': row['number_id'],
                    'added_by': row['added_by'],
                    'added_at': row['added_at'],
                }
                if row['pid'] in guild_pids or row['pid'] in bound_pids:
                    also_auto.append(entry)
                else:
                    manual_only.append(entry)

            lines = []
            lines.append(f"# 📋 Market Watchlist\n")
            lines.append(f"**Total:** {len(rows)} entries\n")

            if manual_only:
                lines.append(f"## Manually Added ({len(manual_only)})\n")
                for entry in manual_only:
                    lines.append(
                        f"• **{entry['nickname']}** ({entry['number_id']}) "
                        f"— added <t:{entry['added_at']}:R> by <@{entry['added_by']}>"
                    )
                lines.append("")

            if also_auto:
                lines.append(f"## Also in Guild/Bound ({len(also_auto)})\n")
                for entry in also_auto:
                    lines.append(
                        f"• **{entry['nickname']}** ({entry['number_id']}) "
                        f"— added <t:{entry['added_at']}:R> by <@{entry['added_by']}>"
                    )
                lines.append("")

            if not manual_only and also_auto:
                lines.append("\nAll watchlist entries are already covered by guild/bound membership.")
            elif not manual_only and not also_auto:
                lines.append("No entries.")

            # Build a Select menu for removal
            remove_options = []
            for row in rows:
                label = f"{row['nickname']} ({row['number_id']})"[:90]
                remove_options.append(
                    discord.SelectOption(
                        label=label,
                        description=f"Remove from watchlist",
                        value=str(row['pid']),
                    )
                )

            inner = [
                TextDisplay("\n".join(lines)),
                Separator(spacing=discord.SeparatorSpacing.small),
            ]

            if remove_options:
                select_row = ActionRow()
                remove_select = Select(
                    placeholder="Select a player to remove from watchlist…",
                    options=remove_options[:25],  # Discord max 25 options per Select
                    custom_id="watchlist_remove_select",
                )
                remove_select.callback = self._watchlist_remove_callback
                select_row.add_item(remove_select)
                inner.append(select_row)

            container = Container(*inner, accent_color=ACCENT_BLURPLE)
            view = LayoutView(timeout=120)
            view.add_item(container)
            await interaction.followup.send(view=view)

        except Exception as e:
            logger.error(f"Market cog: watchlist command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    async def _watchlist_remove_callback(self, interaction: discord.Interaction):
        """Remove a player from the watchlist via Select menu."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        pid = interaction.data.get("values", [None])[0]
        if not pid:
            return

        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT nickname FROM market_watchlist WHERE pid = ?", (pid,))
            row = await cursor.fetchone()
            if row:
                nickname = row[0]
                await db.execute("DELETE FROM market_watchlist WHERE pid = ?", (pid,))
                await db.commit()
                await interaction.followup.send(f"✅ Removed **{nickname}** from the watchlist.", ephemeral=True)
                # Force refresh dashboard since this player is no longer force-included
                await self._refresh_dashboard()
            else:
                await interaction.followup.send("❌ Player not found on watchlist.", ephemeral=True)

    @market_group.command(name="player", description="Look up a specific player's market stats")
    @app_commands.describe(
        number_id="The player's 10-digit Number ID",
        nickname="The player's in-game nickname"
    )
    async def market_player(self, interaction: discord.Interaction, number_id: str = None, nickname: str = None):
        """Show market data for a specific player."""
        if not number_id and not nickname:
            await interaction.response.send_message("❌ Please provide either a Number ID or nickname.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        try:
            from utility.wwm import get_player_info, find_people_by_nickname, fetch_player_data_by_pid
            from settings import WWM_REDIS_PLAYER_URL

            player_entry = None
            resolved_number_id = number_id
            pid = None

            if number_id:
                player_data = get_player_info(number_id, fields=["base", "hoard_profiteer"])
                if player_data and 'result' in player_data and 'base' in player_data['result']:
                    player_entry = player_data['result']
                    pid = player_entry.get('id')
                    player_hostnum = player_entry.get("hostnum", 10595)
                else:
                    await interaction.followup.send("❌ Player not found with that Number ID.", ephemeral=True)
                    return
            elif nickname:
                nick_data = find_people_by_nickname(nickname)
                if not nick_data or 'result' not in nick_data:
                    await interaction.followup.send("❌ Player not found with that nickname.", ephemeral=True)
                    return
                pid = nick_data['result'].get('id')
                player_hostnum = nick_data["result"].get("hostnum", 10595)
                if not pid:
                    await interaction.followup.send("❌ Could not resolve nickname to a player ID.", ephemeral=True)
                    return
                raw = _wwm_api_post(
                    WWM_REDIS_PLAYER_URL,
                    {
                        "fields": ["base", "hoard_profiteer"],
                        "hostnum2pids": {10595: [pid]},
                        "uid": WWM_UID
                    }
                )
                if raw and 'result' in raw:
                    first_pid = next(iter(raw['result'].keys()))
                    player_entry = raw['result'][first_pid]
                if not player_entry:
                    await interaction.followup.send("❌ Failed to fetch player data.", ephemeral=True)
                    return
                resolved_number_id = player_entry.get('base', {}).get('number_id', '')

            base = player_entry.get('base', {}) if isinstance(player_entry, dict) else {}
            hoard = player_entry.get('hoard_profiteer', {}) if isinstance(player_entry, dict) else {}

            if not hoard:
                await interaction.followup.send("⚠️ This player has no market data.", ephemeral=True)
                return

            nickname_val = base.get('nickname', nickname or 'Unknown')
            number_id_val = str(resolved_number_id or base.get('number_id', ''))
            main_good = str(hoard.get('main_good', '?'))
            price_history = hoard.get('price_change_history', [])
            total_profit = hoard.get('total_profit', 0)

            # Check if already on watchlist
            watchlist_pids = await self._get_watchlist_pids()
            is_on_watchlist = pid in watchlist_pids if pid else False

            # Check if good has an approved name
            known_goods = await self._get_known_goods()
            good_has_name = main_good in known_goods if main_good else False
            good_name = await self._get_good_name(main_good) if good_has_name else ""

            # Fetch market likes for this player (hostnum is at root level, not in base)
            likes = await self._fetch_market_likes(pid, player_hostnum) if pid else 0
            logger.debug(f"PID {pid} HOSTNUM {player_hostnum} has {likes} likes")

            view = MarketPlayerView(
                cog=self,
                pid=pid or '',
                nickname=nickname_val,
                number_id=number_id_val,
                main_good=main_good,
                price_history=price_history,
                total_profit=total_profit,
                is_on_watchlist=is_on_watchlist,
                good_has_name=good_has_name,
                good_name=good_name or "",
                likes=likes,
            )

            # Edit the deferred response with the view
            await interaction.edit_original_response(content=None, view=view)

        except Exception as e:
            logger.error(f"Market cog: player lookup failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)


# ---------------------------------------------------------------------------
# View registration
# ---------------------------------------------------------------------------
from cogs.view_registry import register
register(MarketReportView, cog=None, grouped_data={}, total_players=0, report_ts=0, next_update_ts=0, known_goods=set())


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(MarketCog(bot))