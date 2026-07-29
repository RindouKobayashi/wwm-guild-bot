import discord
import datetime
import aiosqlite
import asyncio
import json
import logging
from collections import defaultdict
from typing import Optional, List, Tuple, Dict, Any, Set
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, Section, ActionRow, Button, Modal, TextInput, Select

import settings
from settings import BASE_DIR, CLUB_ID, WWM_UID, WWM_TOKEN, WWM_API_URL, logger, GMT8_TZ
from utility.wwm import get_full_guild_info, get_bulk_hoard_data, get_bulk_players_info, _wwm_api_post, get_topics_likes, get_player_info, find_people_by_nickname


async def is_admin_or_staff(interaction: discord.Interaction) -> bool:
    """Return True if the user is an administrator OR has any staff role from settings."""
    if interaction.user.guild_permissions.administrator:
        return True
    try:
        from settings import STAFF_ROLES
        staff_role_ids = set(STAFF_ROLES.values())
    except (ImportError, AttributeError):
        return False
    member_role_ids = {r.id for r in interaction.user.roles}
    return bool(staff_role_ids & member_role_ids)


def admin_or_staff():
    """Check if the user is an administrator OR has any of the staff roles defined in settings."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not await is_admin_or_staff(interaction):
            raise app_commands.MissingPermissions(["administrator"])
        return True
    return app_commands.check(predicate)

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
                f"• Suggested by: {interaction.user.mention} (`{interaction.user}`)\n\n"
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
# Add to Watchlist Modal
# ---------------------------------------------------------------------------
class AddToWatchlistModal(Modal, title="Add Player to Watchlist"):
    """Modal to add one or more players to the market watchlist by UID or nickname."""

    identifiers = TextInput(
        label="Player UIDs or Nicknames",
        placeholder="e.g. 4036668451, 4025937269, 0032906407, TjTreacher",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, cog: "MarketCog"):
        super().__init__(timeout=300)
        self.cog = cog
        self.pending_entries: List[Tuple[str, str, str]] = []

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.identifiers.value.strip()
        if not raw:
            await interaction.response.send_message("❌ Please enter at least one UID or nickname.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        entries = [part.strip() for part in raw.split(",") if part.strip()]
        if not entries:
            await interaction.followup.send("❌ No valid entries found.", ephemeral=True)
            return

        self.pending_entries = []
        banned_skipped = []
        banned_pids = await self.cog._get_banned_pids()

        for entry in entries:
            pid = None
            nickname = None
            number_id = None
            if entry.isdigit():
                try:
                    player_data = await get_player_info(entry, fields=["base", "hoard_profiteer", "space_data"])
                    if player_data and 'result' in player_data and 'base' in player_data['result']:
                        pid = player_data['result'].get('id')
                        base = player_data['result'].get('base', {})
                        nickname = base.get('nickname')
                        number_id = base.get('number_id')
                except Exception as e:
                    logger.debug(f"Watchlist add lookup failed for number_id {entry}: {e}")
            if not pid:
                try:
                    nick_data = await find_people_by_nickname(entry)
                    if nick_data and 'result' in nick_data:
                        pid = nick_data['result'].get('id')
                        if pid:
                            base = nick_data['result'].get('base', {})
                            nickname = base.get('nickname', nickname or entry)
                            number_id = base.get('number_id', number_id)
                except Exception as e:
                    logger.debug(f"Watchlist add lookup failed for nickname {entry}: {e}")

            if pid:
                entry_data = (pid, nickname or entry, str(number_id) if number_id else "")
                if pid in banned_pids:
                    banned_skipped.append(entry_data)
                else:
                    self.pending_entries.append(entry_data)

        if not self.pending_entries and not banned_skipped:
            await interaction.followup.send("❌ Could not resolve any entries to players.", ephemeral=True)
            return

        # Confirmation view
        lines = ["# 🧾 Confirm Add to Watchlist"]
        if self.pending_entries:
            lines.append(f"**{len(self.pending_entries)} player(s) will be added:**\n")
            lines += [f"• **{n}** (`{nid}`)" for _, n, nid in self.pending_entries]
            lines.append("")
        if banned_skipped:
            lines.append(f"⚠️ **{len(banned_skipped)} player(s) are banned — skipped:**\n")
            for _, n, nid in banned_skipped:
                lines.append(f"• ~~**{n}** (`{nid}`)~~ — on banned list")
            lines.append("")
        lines.append("Please confirm to proceed.")
        container = Container(
            TextDisplay("\n".join(lines)),
            accent_color=ACCENT_BLURPLE,
        )
        confirm_view = _ConfirmAddView(cog=self.cog, entries=self.pending_entries)
        confirm_layout = LayoutView(timeout=120)
        confirm_layout.add_item(container)
        confirm_layout.add_item(confirm_view.action_row)
        await interaction.followup.send(view=confirm_layout, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"AddToWatchlistModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
        except Exception:
            pass


class _BulkPlayerLookupModal(Modal, title="Look Up Players"):
    """Modal for bulk player lookup by UID or nickname."""

    query = TextInput(
        label="Player UIDs or Nicknames",
        placeholder="e.g. 4036668451, 4025937269, 0032906407, TjTreacher",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, cog: "MarketCog"):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.query.value.strip()
        if not raw:
            await interaction.response.send_message("❌ Please enter at least one UID or nickname.", ephemeral=True)
            return
        await self.cog._bulk_player_lookup(interaction, raw)


class _BulkPlayerConfirmView:
    """Confirmation view for bulk player lookup with stats and add-to-watchlist option."""

    def __init__(self, cog: "MarketCog", players: List[Dict[str, Any]], authorized_user_id: int):
        self.cog = cog
        self.players = players
        self.authorized_user_id = authorized_user_id

        self.action_row = ActionRow()
        add_btn = Button(
            label="➕ Add All to Watchlist",
            style=discord.ButtonStyle.success,
            custom_id="bulk_player_add_watchlist",
        )
        add_btn.callback = self._on_add_all
        self.action_row.add_item(add_btn)

        close_btn = Button(
            label="❌ Close",
            style=discord.ButtonStyle.secondary,
            custom_id="bulk_player_close",
        )
        close_btn.callback = self._on_close
        self.action_row.add_item(close_btn)

    def _disable(self):
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    def build_container(self) -> Container:
        inner: list = []
        lines = [f"# 📈 Player Stats — {len(self.players)} player(s)"]
        for p in self.players:
            pct_str, _ = _pct_str(p['price_history'][0], p['price_history'][-1]) if p['price_history'] else ("N/A", 0.0)
            status = "✅ In report" if p['is_on_watchlist'] else "⚪ Not in report"
            good_label = f"{p['good_name']} (#{p['main_good']})" if p['good_name'] else f"#{p['main_good']}"

            coop_prefix = "[COOP ✅] " if p.get('mode') == 17 else ""
            player_lines = [f"• **{coop_prefix}{p['nickname']}** ({p['number_id']})  │  {status}"]
            if p['guild_name'] and p['guild_name'] != 'Unknown':
                player_lines.append(f"  **Guild:** *{p['guild_name']}*")
            player_lines.append(f"  **Main Good:** `{good_label}`")
            if p['price_history']:
                original_price = p['price_history'][0]
                current_price = p['price_history'][-1]
                player_lines.append(f"  **Price:** `{original_price}` → `{current_price}` ({pct_str})")
                player_lines.append("  **History:** `" + " → ".join(str(price) for price in p['price_history']) + "`")
            if p['likes'] > 0:
                player_lines.append(f"  **👍 Market Likes:** `{p['likes']}`")

            lines.append("\n".join(player_lines))
        inner.append(TextDisplay("\n".join(lines)))
        return Container(*inner, accent_color=ACCENT_BLURPLE)

    async def _on_add_all(self, interaction: discord.Interaction):
        if interaction.user.id != self.authorized_user_id:
            await interaction.response.send_message("❌ Only the person who used the command can use this button.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        banned_pids = await self.cog._get_banned_pids()
        added = 0
        skipped_banned = 0
        for p in self.players:
            if p['is_on_watchlist']:
                continue
            if p['pid'] in banned_pids:
                skipped_banned += 1
                continue
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "REPLACE INTO market_watchlist (pid, nickname, number_id, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                    (p['pid'], p['nickname'], p['number_id'], interaction.user.id, int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
                )
                await db.commit()
            added += 1

        self._disable()
        result_lines = [f"✅ **Watchlist Updated** by {interaction.user.mention}"]
        result_lines.append(f"**{added} player(s) added** to the market watchlist.")
        if skipped_banned:
            result_lines.append(f"⚠️ **{skipped_banned} player(s) skipped** — on banned list.")
        container = Container(
            TextDisplay("\n".join(result_lines)),
            accent_color=ACCENT_GREEN,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(container)
        await interaction.edit_original_response(view=done_view)

        logger.info(f"Market watchlist bulk add from player command: {added} players, {skipped_banned} banned skipped by {interaction.user}")
        await self.cog._refresh_dashboard()

    async def _on_close(self, interaction: discord.Interaction):
        if interaction.user.id != self.authorized_user_id:
            await interaction.response.send_message("❌ Only the person who used the command can use this button.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        self._disable()
        container = Container(
            TextDisplay("ℹ️ **Closed**\n\nNo changes were made."),
            accent_color=ACCENT_BLURPLE,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(container)
        await interaction.edit_original_response(view=done_view)


class _ConfirmAddView:
    """Holds approve/reject buttons for watchlist confirmation."""

    def __init__(self, cog: "MarketCog", entries: List[Tuple[str, str, str]], suggested_by: Optional[discord.abc.User] = None):
        self.cog = cog
        self.entries = entries
        self.suggested_by = suggested_by

        self.action_row = ActionRow()
        approve_btn = Button(
            label="✅ Confirm – Add All",
            style=discord.ButtonStyle.success,
            custom_id="watchlist_confirm_add",
        )
        approve_btn.callback = self._on_approve
        self.action_row.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="watchlist_reject_add",
        )
        reject_btn.callback = self._on_reject
        self.action_row.add_item(reject_btn)

    def _disable(self):
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    async def _on_approve(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        added = 0
        for pid, nickname, number_id in self.entries:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "REPLACE INTO market_watchlist (pid, nickname, number_id, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                    (pid, nickname, number_id, interaction.user.id, int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
                )
                await db.commit()
            added += 1

        self._disable()
        container = Container(
            TextDisplay(
                f"✅ **Watchlist Updated** by {interaction.user.mention}\n\n"
                f"**{added} player(s) added** to the market watchlist."
            ),
            accent_color=ACCENT_GREEN,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(container)
        await interaction.edit_original_response(view=done_view)

        logger.info(f"Market watchlist bulk add: {added} players by {interaction.user}")
        await self.cog._refresh_dashboard()

    async def _on_reject(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        self._disable()
        container = Container(
            TextDisplay("❌ **Watchlist Add Cancelled**\n\nNo players were added."),
            accent_color=ACCENT_RED,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(container)
        await interaction.edit_original_response(view=done_view)


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
        if not await is_admin_or_staff(interaction):
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
        if not await is_admin_or_staff(interaction):
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
        if not await is_admin_or_staff(interaction):
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
        if not await is_admin_or_staff(interaction):
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
        logger.info(f"Market player watchlist reject: {self.nickname} ({self.pid}) (by {interaction.user})")


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
        guild_name: str = "",
        mode: int = 0,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.pid = pid
        self.nickname = nickname
        self.number_id = number_id
        self.main_good = main_good
        self.price_history = price_history or []
        self.good_name = good_name or ""
        self.guild_name = guild_name or ""
        self.mode = mode

        inner: list = []

        # Stats text
        coop_prefix = "[COOP ✅] " if self.mode == 17 else ""
        lines = [f"# 📈 Market Stats — {coop_prefix}**{nickname}**"]
        if self.guild_name and self.guild_name != 'Unknown':
            lines.append(f"**Guild:** *{self.guild_name}*")
        lines.append(f"**Number ID:** `{number_id}`")
        good_label = f"{good_name} (#{main_good})" if good_name else f"#{main_good}"
        lines.append(f"**Main Good:** `{good_label}`")
        if self.price_history:
            self.original_price = price_history[0]
            self.current_price = price_history[-1]
            self.pct_str, _ = _pct_str(self.original_price, self.current_price)
            lines.append(f"**Price:** `{self.original_price}` → `{self.current_price}` ({self.pct_str})")
            lines.append("**History:** `" + " → ".join(str(p) for p in self.price_history) + "`")
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
        await interaction.response.defer(ephemeral=True)

        # Check if player is banned first
        if await self.cog._is_player_banned(self.pid):
            await interaction.edit_original_response(content=None)
            await interaction.followup.send(
                f"❌ **{self.nickname}** is on the banned list and cannot be added to the watchlist.",
                ephemeral=True
            )
            return

        # Directly add to watchlist (no admin approval needed)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "REPLACE INTO market_watchlist (pid, nickname, number_id, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                (self.pid, self.nickname, self.number_id, interaction.user.id, int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
            )
            await db.commit()

        logger.info(f"Market player watchlist add (auto): {self.nickname} ({self.pid}) by {interaction.user}")

        # Edit the original response to show success and remove the include button
        inner: list = []

        # Stats text (same as constructor)
        coop_prefix = "[COOP ✅] " if self.mode == 17 else ""
        lines = [f"# 📈 Market Stats — {coop_prefix}{self.nickname}"]
        lines.append(f"**Number ID:** `{self.number_id}`")
        good_label = f"{self.good_name} (#{self.main_good})" if self.good_name else f"#{self.main_good}"
        lines.append(f"**Main Good:** `{good_label}`")
        if self.price_history:
            lines.append(f"**Price:** `{self.original_price}` → `{self.current_price}` ({self.pct_str})")
            lines.append("**History:** `" + " → ".join(str(p) for p in self.price_history) + "`")
        lines.append(f"**💰 Price Change:** `{self.pct_str}`")
        inner.append(TextDisplay("\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Success message — no buttons
        inner.append(TextDisplay("✅ **Successfully added to market report!**"))
        inner.append(TextDisplay(f"Player `{self.nickname}` is now tracked in the daily report. Refreshing dashboard..."))

        container = Container(*inner, accent_color=ACCENT_GREEN)
        success_view = LayoutView(timeout=None)
        success_view.add_item(container)

        await interaction.edit_original_response(content=None, view=success_view)

        # Force refresh dashboard to include this player
        await self.cog._refresh_dashboard()

        await interaction.followup.send(
            f"✅ **{self.nickname}** has been added to the market report!",
            ephemeral=True
        )

    async def _on_suggest_name(self, interaction: discord.Interaction):
        modal = GoodNameModal(good_id=self.main_good, cog=self.cog)
        await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# Market Report View (Components V2) — updated with Add to Watchlist button
# ---------------------------------------------------------------------------
class MarketReportView(LayoutView):
    """Components V2 LayoutView for the daily market price report."""

    def __init__(
        self,
        cog: "MarketCog",
        grouped_data: Dict[str, List[Tuple[str, str, str, float, float, float, bool, int, str, int, int]]],
        total_players: int,
        report_ts: int,
        next_update_ts: int,
        day_number: int = 1,
        countdown_str: str = "",
        known_goods: Set[str] = None,
        good_names_map: Dict[str, str] = None,
        likes_map: Dict[str, int] = None,
        pending_report_pids: Set[str] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.grouped_data = grouped_data
        self.total_players = total_players
        self.report_ts = report_ts
        self.next_update_ts = next_update_ts
        self.day_number = day_number
        self.countdown_str = countdown_str
        self.known_goods = known_goods or set()
        self.good_names_map = good_names_map or {}
        self.likes_map = likes_map or {}
        self.pending_report_pids = pending_report_pids or set()

        inner_items: list = []

        # Title section with invite button
        inner_items.append(
            Section(
                TextDisplay(
                    f"# 📈 Market Price Report\n"
                    f"📅 **Day {day_number}**  •  ⏰ Resets {countdown_str}\n"
                    f"Tracking **{total_players}** players across **{len(grouped_data)}** goods"
                ),
                accessory=Button(
                    label="🔗 Join the Discord",
                    url="https://discord.gg/YQSV79ysGY",
                    style=discord.ButtonStyle.link,
                ),
            )
        )
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Sort goods by their ID for consistent ordering
        sorted_goods = sorted(grouped_data.keys(), key=lambda gid: int(gid))

        for idx, good_id in enumerate(sorted_goods):
            players = grouped_data[good_id]
            emoji = GOOD_EMOJIS[idx % len(GOOD_EMOJIS)]
            good_name = self.good_names_map.get(good_id, "")
            label = f"{good_name} (#{good_id})" if good_name else f"Good #{good_id}"

            # Get pending report PIDs for dashboard warnings
            pending_report_pids = self.pending_report_pids or set()

            # Build leaderboard lines
            lines = []
            for rank, (pid, nickname, number_id, original_price, current_price, pct, is_online, _hostnum, guild_name, mode, other_search) in enumerate(players[:10], 1):
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
                coop_prefix = "[COOP ✅] " if mode == 17 else ""
                reported_prefix = "[⚠️] " if pid in pending_report_pids else ""
                guild_display = f" — *{guild_name}*" if guild_name and guild_name != 'Unknown' else ""
                likes_text = f"  │  👍 {self.likes_map.get(pid, 0)}" if self.likes_map.get(pid, 0) else ""
                no_search_strike = "~~" if other_search == 0 else ""
                no_search_end = "~~" if other_search == 0 else ""
                lines.append(
                    f"{prefix} {online_icon} **{reported_prefix}{coop_prefix}{no_search_strike}{nickname}{no_search_end}** ({number_id}){guild_display}  ─  "
                    f"`{original_price:.0f}` → `{current_price:.0f}`  │  **{sign}{pct:.2f}%**"
                    f"{likes_text}"
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
            f"📊 Report generated: <t:{report_ts}:R>  •  🔄 Updates every 1 minute\n"
            f"ℹ️ Players displayed have valid prices (currently logged in or logged in after price update)\n"
            f"🤝 [COOP ✅] indicates player is in coop world — likely open to trade requests\n"
            f"⚠️ Players with [⚠️] had been reported to staff and is awaiting review\n"
            f"⛔️ ~~Strikethrough~~ indicates player has player search disabled — cannot request coop\n"
        ))

        # Add to watchlist button
        add_row = ActionRow()
        add_btn = Button(
            label="➕ Add Player to Watchlist",
            style=discord.ButtonStyle.success,
            custom_id="market_add_watchlist",
        )
        add_btn.callback = self._on_add_watchlist
        add_row.add_item(add_btn)

        report_btn = Button(
            label="🚨 Report Player",
            style=discord.ButtonStyle.danger,
            custom_id="market_report_player",
        )
        report_btn.callback = self._on_report_player
        add_row.add_item(report_btn)

        refresh_btn = Button(
            label="🔄 Refresh",
            style=discord.ButtonStyle.primary,
            custom_id="market_refresh",
        )
        refresh_btn.callback = self._on_refresh
        add_row.add_item(refresh_btn)

        # Guild watchlist button
        guild_watchlist_btn = Button(
            label="🏰 Add My Guild",
            style=discord.ButtonStyle.success,
            custom_id="market_add_my_guild",
        )
        guild_watchlist_btn.callback = self._on_add_my_guild
        add_row.add_item(guild_watchlist_btn)
        inner_items.append(add_row)

        # Filter button row
        filter_row = ActionRow()
        online_filter_btn = Button(
            label="🟢 Filter Online Only",
            style=discord.ButtonStyle.secondary,
            custom_id="market_filter_online",
        )
        online_filter_btn.callback = self._on_filter_online
        filter_row.add_item(online_filter_btn)

        guild_filter_btn = Button(
            label="🏰 Filter My Guild",
            style=discord.ButtonStyle.secondary,
            custom_id="market_filter_guild",
        )
        guild_filter_btn.callback = self._on_filter_guild
        filter_row.add_item(guild_filter_btn)
        
        inner_items.append(filter_row)

        container = Container(*inner_items, accent_color=ACCENT_GREEN)
        self.add_item(container)

    async def _on_add_watchlist(self, interaction: discord.Interaction):
        modal = AddToWatchlistModal(cog=self.cog)
        await interaction.response.send_modal(modal)

    async def _on_report_player(self, interaction: discord.Interaction):
        modal = ReportPlayerModal(cog=self.cog)
        await interaction.response.send_modal(modal)

    async def _on_refresh(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔄 Refreshing dashboard...", ephemeral=True)
        await self.cog._refresh_dashboard()

    async def _on_filter_online(self, interaction: discord.Interaction):
        """Filter the report to show only online players, sent as an ephemeral message."""
        await interaction.response.defer(ephemeral=True)

        # Filter each good group to only include online players
        filtered: Dict[str, List[Tuple[str, str, str, float, float, float, bool, int, str, int]]] = {}
        total_filtered = 0
        for good_id, players in self.grouped_data.items():
            online_players = [p for p in players if p[6]]  # is_online is index 6
            if online_players:
                filtered[good_id] = online_players
                total_filtered += len(online_players)

        if not filtered:
            await interaction.followup.send("🟢 No online players found in the current report data.", ephemeral=True)
            return

        # Build a text-only ephemeral response (simpler than a full LayoutView)
        lines = ["# 🟢 Online Players Only", f"Showing **{total_filtered}** online players across **{len(filtered)}** goods\n"]

        sorted_goods = sorted(filtered.keys(), key=lambda gid: int(gid))
        for idx, good_id in enumerate(sorted_goods):
            players = filtered[good_id]
            emoji = GOOD_EMOJIS[idx % len(GOOD_EMOJIS)]
            good_name = self.good_names_map.get(good_id, "")
            label = f"{good_name} (#{good_id})" if good_name else f"Good #{good_id}"

            lines.append(f"### {emoji} {label} — {len(players)} online")
            for rank, (pid, nickname, number_id, original_price, current_price, pct, is_online, _hostnum, guild_name, mode, _other_search) in enumerate(players[:10], 1):
                prefix = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"`{rank}.`"
                sign = "+" if pct >= 0 else ""
                coop_prefix = "[COOP ✅] " if mode == 17 else ""
                guild_display = f" — *{guild_name}*" if guild_name and guild_name != 'Unknown' else ""
                likes_text = f"  │  👍 {self.likes_map.get(pid, 0)}" if self.likes_map.get(pid, 0) else ""
                lines.append(
                    f"{prefix} 🟢 **{coop_prefix}{nickname}** ({number_id}){guild_display}  ─  "
                    f"`{original_price:.0f}` → `{current_price:.0f}`  │  **{sign}{pct:.2f}%**"
                    f"{likes_text}"
                )
            lines.append("")

        lines.append(f"📊 Filtered from current report data.")

        # Use a simple Container with TextDisplay for the ephemeral reply
        inner = [
            TextDisplay("\n".join(lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay("_This message is only visible to you._"),
        ]
        container = Container(*inner, accent_color=ACCENT_BLURPLE)
        result_view = LayoutView(timeout=60)
        result_view.add_item(container)
        await interaction.followup.send(view=result_view, ephemeral=True)

    def _make_suggest_callback(self, good_id: str):
        async def callback(interaction: discord.Interaction):
            modal = GoodNameModal(good_id=good_id, cog=self.cog)
            await interaction.response.send_modal(modal)
        return callback

    async def _on_add_my_guild(self, interaction: discord.Interaction):
        """Add the user's bound guild to the market guild watchlist."""
        await interaction.response.defer(ephemeral=True)
        
        # Get bound player info
        bound_info = await self.cog._get_bound_player_info(interaction.user.id)
        if not bound_info:
            await interaction.followup.send(
                "❌ You don't have a bound account. Please bind your account first using the verification system.",
                ephemeral=True
            )
            return
        
        if not bound_info.get('club_id'):
            await interaction.followup.send(
                "❌ Your bound player is not in a guild. Join a guild first and then try again.",
                ephemeral=True
            )
            return
        
        # Add to guild watchlist
        guild_name = await self.cog._resolve_guild_name(bound_info['club_id'], bound_info.get('hostnum', 10595))
        await self.cog._add_guild_to_watchlist(
            club_id=bound_info['club_id'],
            hostnum=bound_info.get('hostnum', 10595),
            guild_name=guild_name,
            user_id=interaction.user.id
        )
        
        logger.info(f"Market guild watchlist add (via button): {guild_name} (club {bound_info['club_id']}) by {interaction.user}")
        
        # Refresh dashboard to include new guild members
        await self.cog._refresh_dashboard()
        
        await interaction.followup.send(
            f"✅ **Guild Added to Market Watchlist**\n\n"
            f"• **Guild:** {guild_name}\n"
            f"All members of this guild will now be included in the market dashboard. "
            f"The member list updates live every refresh.",
            ephemeral=True
        )

    async def _on_filter_guild(self, interaction: discord.Interaction):
        """Filter the report to show only players from the user's bound guild, sent as an ephemeral message."""
        await interaction.response.defer(ephemeral=True)

        # Look up bound player_pid for this user
        bound_pid = None
        try:
            async with aiosqlite.connect(VERIFICATION_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT player_pid FROM verified_members WHERE user_id = ? AND player_pid IS NOT NULL",
                    (interaction.user.id,)
                )
                row = await cursor.fetchone()
                if row:
                    bound_pid = row[0]
        except Exception as e:
            logger.error(f"Market cog: failed to lookup bound player for user {interaction.user.id}: {e}")

        if not bound_pid:
            await interaction.followup.send(
                "❌ You don't have a bound account. Please bind your account first using the verification system.",
                ephemeral=True
            )
            return

        # Find the bound player's guild name in the current report data
        bound_guild_name = None
        for good_id, players in self.grouped_data.items():
            for player_tuple in players:
                # player_tuple: (pid, nickname, number_id, original_price, current_price, pct, is_online, hostnum, guild_name, mode)
                if player_tuple[0] == bound_pid:
                    bound_guild_name = player_tuple[8]
                    break
            if bound_guild_name:
                break

        if not bound_guild_name or bound_guild_name == 'Unknown':
            await interaction.followup.send(
                f"⚠️ Could not determine your guild from the current report data. "
                f"Make sure your character is online or recently active.",
                ephemeral=True
            )
            return

        # Filter each good group to only include players from the same guild
        filtered: Dict[str, List[Tuple[str, str, str, float, float, float, bool, int, str, int]]] = {}
        total_filtered = 0
        for good_id, players in self.grouped_data.items():
            guild_players = [p for p in players if p[8] == bound_guild_name]
            if guild_players:
                filtered[good_id] = guild_players
                total_filtered += len(guild_players)

        if not filtered:
            await interaction.followup.send(
                f"🏰 No other players from **{bound_guild_name}** found in the current report data.",
                ephemeral=True
            )
            return

        # Build a text-only ephemeral response
        lines = [f"# 🏰 Guild Filter — {bound_guild_name}", f"Showing **{total_filtered}** player(s) from your guild\n"]
        sorted_goods = sorted(filtered.keys(), key=lambda gid: int(gid))
        for idx, good_id in enumerate(sorted_goods):
            players = filtered[good_id]
            emoji = GOOD_EMOJIS[idx % len(GOOD_EMOJIS)]
            good_name = self.good_names_map.get(good_id, "")
            label = f"{good_name} (#{good_id})" if good_name else f"Good #{good_id}"

            lines.append(f"### {emoji} {label} — {len(players)} player(s)")
            for rank, (pid, nickname, number_id, original_price, current_price, pct, is_online, _hostnum, guild_name, mode, _other_search) in enumerate(players[:10], 1):
                prefix = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"`{rank}.`"
                sign = "+" if pct >= 0 else ""
                online_icon = "🟢" if is_online else "⚫"
                coop_prefix = "[COOP ✅] " if mode == 17 else ""
                likes_text = f"  │  👍 {self.likes_map.get(pid, 0)}" if self.likes_map.get(pid, 0) else ""
                lines.append(
                    f"{prefix} {online_icon} **{coop_prefix}{nickname}** ({number_id})  ─  "
                    f"`{original_price:.0f}` → `{current_price:.0f}`  │  **{sign}{pct:.2f}%**"
                    f"{likes_text}"
                )
            lines.append("")

        lines.append(f"📊 Filtered from current report data.")

        inner = [
            TextDisplay("\n".join(lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay("_This message is only visible to you._"),
        ]
        container = Container(*inner, accent_color=ACCENT_BLURPLE)
        result_view = LayoutView(timeout=60)
        result_view.add_item(container)
        await interaction.followup.send(view=result_view, ephemeral=True)


# ---------------------------------------------------------------------------
# New Week Market View (shown when a new week has started)
# ---------------------------------------------------------------------------
class NewWeekMarketView(LayoutView):
    """Components V2 LayoutView shown when market is in 'new week' mode.
    Displays the available goods from average_price and asks people to buy them.
    """

    def __init__(
        self,
        cog: "MarketCog",
        good_ids: List[str],
        good_names_map: Dict[str, str],
        known_goods: Set[str],
        report_ts: int,
        day_number: int = 1,
        countdown_str: str = "",
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.good_ids = good_ids
        self.good_names_map = good_names_map
        self.known_goods = known_goods

        inner_items: list = []

        # Title section with invite button
        inner_items.append(
            Section(
                TextDisplay(
                    f"# 🆕 New Market Week!\n"
                    f"📅 **Day {day_number}**  •  ⏰ Resets {countdown_str}\n"
                    f"Market prices have reset. Check out these goods to buy and prepare for stock changes!"
                ),
                accessory=Button(
                    label="🔗 Join the Discord",
                    url="https://discord.gg/YQSV79ysGY",
                    style=discord.ButtonStyle.link,
                ),
            )
        )
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Display each good
        for idx, good_id in enumerate(sorted(good_ids, key=lambda gid: int(gid))):
            emoji = GOOD_EMOJIS[idx % len(GOOD_EMOJIS)]
            good_name = good_names_map.get(good_id, "")
            label = f"{good_name} (#{good_id})" if good_name else f"Good #{good_id}"
            inner_items.append(TextDisplay(
                f"{emoji} **{label}** — Recommended to buy!"
            ))

            # Suggest Name button for goods without a name
            if good_id not in known_goods:
                name_row = ActionRow()
                suggest_btn = Button(
                    label="🏷️ Suggest Name",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"newwk_suggest_name:{good_id}",
                )
                suggest_btn.callback = self._make_suggest_callback(good_id)
                name_row.add_item(suggest_btn)
                inner_items.append(name_row)

            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Add to watchlist
        add_row = ActionRow()
        add_btn = Button(
            label="➕ Add Player to Watchlist",
            style=discord.ButtonStyle.success,
            custom_id="market_add_watchlist",
        )
        add_btn.callback = self._on_add_watchlist
        add_row.add_item(add_btn)

        refresh_btn = Button(
            label="🔄 Refresh",
            style=discord.ButtonStyle.primary,
            custom_id="newwk_market_refresh",
        )
        refresh_btn.callback = self._on_refresh
        add_row.add_item(refresh_btn)
        inner_items.append(add_row)

        # Footer
        inner_items.append(TextDisplay(
            f"📊 Generated: <t:{report_ts}:R>  •  🔄 Updates every 1 minute"
        ))

        container = Container(*inner_items, accent_color=ACCENT_GREEN)
        self.add_item(container)

    async def _on_add_watchlist(self, interaction: discord.Interaction):
        modal = AddToWatchlistModal(cog=self.cog)
        await interaction.response.send_modal(modal)

    async def _on_refresh(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔄 Refreshing dashboard...", ephemeral=True)
        await self.cog._refresh_dashboard()

    def _make_suggest_callback(self, good_id: str):
        async def callback(interaction: discord.Interaction):
            modal = GoodNameModal(good_id=good_id, cog=self.cog)
            await interaction.response.send_modal(modal)
        return callback


# ---------------------------------------------------------------------------
# Watchlist Paginated View
# ---------------------------------------------------------------------------
class WatchlistPaginatedView(LayoutView):
    """Paginated view for the market watchlist showing 10 entries per page."""

    def __init__(
        self,
        cog: "MarketCog",
        all_entries: List[Dict[str, Any]],
        guild_pids: Set[str],
        bound_pids: Set[str],
        guild_map: Dict[str, str] = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.all_entries = all_entries
        self.guild_pids = guild_pids
        self.bound_pids = bound_pids
        self.guild_map = guild_map or {}
        self.page_size = 10
        self.current_page = 0
        self.total_pages = max(1, (len(all_entries) + self.page_size - 1) // self.page_size)

        self._build()

    def _get_page_entries(self) -> Tuple[List[Dict], List[Dict]]:
        start = self.current_page * self.page_size
        end = start + self.page_size
        page = self.all_entries[start:end]
        manual = [e for e in page if e['pid'] not in self.guild_pids and e['pid'] not in self.bound_pids]
        auto = [e for e in page if e['pid'] in self.guild_pids or e['pid'] in self.bound_pids]
        return manual, auto

    def _build(self) -> None:
        # Clear previous items except the base container approach
        self.clear_items()

        manual, auto = self._get_page_entries()
        start = self.current_page * self.page_size + 1
        end = min((self.current_page + 1) * self.page_size, len(self.all_entries))

        lines: List[str] = []
        lines.append(f"# 📋 Market Watchlist\n")
        lines.append(f"**Total:** {len(self.all_entries)} entries  •  **Page {self.current_page + 1}/{self.total_pages}** (showing {start}-{end})\n")

        page_manual_start = self.current_page * self.page_size
        page_manual = [e for e in self.all_entries[page_manual_start:page_manual_start + self.page_size]
                       if e['pid'] not in self.guild_pids and e['pid'] not in self.bound_pids]
        page_auto = [e for e in self.all_entries[page_manual_start:page_manual_start + self.page_size]
                     if e['pid'] in self.guild_pids or e['pid'] in self.bound_pids]

        if page_manual:
            lines.append(f"## Manually Added ({len([e for e in self.all_entries if e['pid'] not in self.guild_pids and e['pid'] not in self.bound_pids])})\n")
            for entry in page_manual:
                guild_info = self.guild_map.get(entry['pid'])
                if guild_info:
                    lines.append(
                        f"• **{entry['nickname']}** ({entry['number_id']}) — 🏰 *{guild_info}*\n"
                        f"  └─ added <t:{entry['added_at']}:R> by <@{entry['added_by']}>"
                    )
                else:
                    lines.append(
                        f"• **{entry['nickname']}** ({entry['number_id']})\n"
                        f"  └─ added <t:{entry['added_at']}:R> by <@{entry['added_by']}>"
                    )
            lines.append("")

        if page_auto:
            lines.append(f"## Also in Guild/Bound ({len([e for e in self.all_entries if e['pid'] in self.guild_pids or e['pid'] in self.bound_pids])})\n")
            for entry in page_auto:
                guild_info = self.guild_map.get(entry['pid'])
                if guild_info:
                    lines.append(
                        f"• **{entry['nickname']}** ({entry['number_id']}) — 🏰 *{guild_info}*\n"
                        f"  └─ added <t:{entry['added_at']}:R> by <@{entry['added_by']}>"
                    )
                else:
                    lines.append(
                        f"• **{entry['nickname']}** ({entry['number_id']})\n"
                        f"  └─ added <t:{entry['added_at']}:R> by <@{entry['added_by']}>"
                    )
            lines.append("")

        if not page_manual and not page_auto:
            lines.append("No entries on this page.")

        inner: list = []
        inner.append(TextDisplay("\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Remove Select menu (only for entries on current page)
        remove_options: List[discord.SelectOption] = []
        for entry in page_manual + page_auto:
            label = f"{entry['nickname']} ({entry['number_id']})"[:90]
            remove_options.append(
                discord.SelectOption(
                    label=label,
                    description=f"Remove from watchlist",
                    value=str(entry['pid']),
                )
            )

        if remove_options:
            select_row = ActionRow()
            remove_select = Select(
                placeholder="Select a player to remove…",
                options=remove_options[:25],
                custom_id="watchlist_remove_select",
            )
            remove_select.callback = self._on_remove
            select_row.add_item(remove_select)
            inner.append(select_row)

        # Guild watchlist button
        guild_watchlist_row = ActionRow()
        guild_watchlist_btn = Button(
            label="🏰 View Guild Watchlist",
            style=discord.ButtonStyle.secondary,
            custom_id="watchlist_show_guilds",
        )
        guild_watchlist_btn.callback = self._on_show_guild_watchlist
        guild_watchlist_row.add_item(guild_watchlist_btn)
        
        banned_btn = Button(
            label="🚫 Show Banned List",
            style=discord.ButtonStyle.danger,
            custom_id="watchlist_show_banned",
        )
        banned_btn.callback = self._on_show_banned
        guild_watchlist_row.add_item(banned_btn)
        inner.append(guild_watchlist_row)

        # Navigation buttons
        nav_row = ActionRow()
        prev_btn = Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="watchlist_prev",
            disabled=self.current_page == 0,
        )
        prev_btn.callback = self._on_prev
        nav_row.add_item(prev_btn)

        page_btn = Button(
            label=f"{self.current_page + 1}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            custom_id="watchlist_page",
            disabled=True,
        )
        nav_row.add_item(page_btn)

        next_btn = Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="watchlist_next",
            disabled=self.current_page >= self.total_pages - 1,
        )
        next_btn.callback = self._on_next
        nav_row.add_item(next_btn)
        inner.append(nav_row)

        container = Container(*inner, accent_color=ACCENT_BLURPLE)
        self.add_item(container)

    async def _on_prev(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self._build()
            await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._build()
            await interaction.response.edit_message(view=self)

    async def _on_show_guild_watchlist(self, interaction: discord.Interaction):
        """Show all guilds in the guild watchlist with remove option."""
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            guild_entries = await self.cog._get_guild_watchlist()
            if not guild_entries:
                await interaction.followup.send("🏰 Guild watchlist is empty. Use `/market guild` to add your guild.")
                return
            
            # Build guild watchlist display
            lines = ["# 🏰 Guild Watchlist\n"]
            lines.append(f"**Total:** {len(guild_entries)} guild(s) being tracked\n")
            
            for idx, entry in enumerate(guild_entries, 1):
                guild_name = entry.get('guild_name', 'Unknown')
                club_id = entry.get('club_id')
                hostnum = entry.get('hostnum', 10595)
                added_by = entry.get('added_by')
                added_at = entry.get('added_at')
                
                lines.append(f"**{idx}.** {guild_name}")
                lines.append(f"   **Club ID:** `{club_id}` (hostnum: `{hostnum}`)")
                lines.append(f"   **Added by:** <@{added_by}> — <t:{added_at}:R>")
                lines.append("")
            
            lines.append("\nAll guild members are automatically included in the market dashboard on each refresh.")
            
            inner: list = []
            inner.append(TextDisplay("\n".join(lines)))
            inner.append(Separator(spacing=discord.SeparatorSpacing.small))
            
            # Add remove select dropdown if there are guilds
            if guild_entries:
                remove_options: List[discord.SelectOption] = []
                for entry in guild_entries:
                    guild_name = entry.get('guild_name', 'Unknown')
                    club_id = entry.get('club_id')
                    hostnum = entry.get('hostnum', 10595)
                    label = f"{guild_name}"[:90]
                    description = f"Club ID: {club_id}, Hostnum: {hostnum}"[:100]
                    # Use pipe separator instead of colon since club_id can contain colons
                    remove_options.append(
                        discord.SelectOption(
                            label=label,
                            description=description,
                            value=f"{club_id}|{hostnum}",
                        )
                    )
                
                if remove_options:
                    select_row = ActionRow()
                    remove_select = Select(
                        placeholder="Select a guild to remove…",
                        options=remove_options[:25],
                        custom_id="guild_watchlist_remove_select",
                    )
                    remove_select.callback = self._on_remove_guild
                    select_row.add_item(remove_select)
                    inner.append(select_row)
            
            inner.append(TextDisplay("_This message is only visible to you._"))
            
            container = Container(*inner, accent_color=ACCENT_BLURPLE)
            view = LayoutView(timeout=120)
            view.add_item(container)
            await interaction.followup.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            
        except Exception as e:
            logger.error(f"Failed to show guild watchlist: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    async def _on_remove_guild(self, interaction: discord.Interaction):
        """Remove a guild from the guild watchlist."""
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        
        value = interaction.data.get("values", [None])[0]
        if not value:
            await interaction.response.send_message("❌ No guild selected.", ephemeral=True)
            return
        
        # Parse guild selection value (format: club_id|hostnum)
        # club_id can contain colons, so we use pipe as separator
        try:
            parts = value.split("|")
            if len(parts) != 2:
                raise ValueError(f"Invalid value format: {value}")
            club_id_str, hostnum_str = parts
            
            # Validate hostnum is numeric
            if not hostnum_str.isdigit():
                raise ValueError(f"Invalid hostnum: {hostnum_str}")
            
            hostnum = int(hostnum_str)
            # club_id can be any string (numeric or alphanumeric)
            club_id = club_id_str if club_id_str else None
            
            if not club_id:
                raise ValueError(f"Invalid club_id: {club_id_str}")
        except (ValueError, AttributeError) as e:
            logger.error(f"Failed to parse guild selection value '{value}': {e}")
            await interaction.response.send_message("❌ Invalid guild selection.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Get guild name before removing for the response
        guild_entries = await self.cog._get_guild_watchlist()
        guild_name = "Unknown"
        for entry in guild_entries:
            if entry['club_id'] == club_id and entry['hostnum'] == hostnum:
                guild_name = entry.get('guild_name', 'Unknown')
                break
        
        # Remove from watchlist
        await self.cog._remove_guild_from_watchlist(club_id, hostnum)
        
        logger.info(f"Market guild watchlist remove: {guild_name} (club {club_id}) by {interaction.user}")
        
        # Refresh dashboard
        await self.cog._refresh_dashboard()
        
        await interaction.followup.send(
            f"✅ **Guild Removed from Watchlist**\n\n"
            f"• **Guild:** {guild_name}\n"
            f"• **Club ID:** `{club_id}`\n\n"
            f"Members from this guild will no longer be included in the market dashboard.",
            ephemeral=True
        )

    async def _on_show_banned(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            banned_entries = await self.cog._get_banned_list()
            if not banned_entries:
                await interaction.followup.send("🚫 Banned list is empty.")
                return
            view = BannedListPaginatedView(cog=self.cog, all_entries=banned_entries)
            await interaction.followup.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            logger.error(f"Failed to show banned list: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    async def _on_remove(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        pid = interaction.data.get("values", [None])[0]
        if not pid:
            return

        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(self.cog.db_path) as db:
            cursor = await db.execute("SELECT nickname FROM market_watchlist WHERE pid = ?", (pid,))
            row = await cursor.fetchone()
            if row:
                nickname = row[0]
                await db.execute("DELETE FROM market_watchlist WHERE pid = ?", (pid,))
                await db.commit()
                await interaction.followup.send(f"✅ Removed **{nickname}** from the watchlist.", ephemeral=True)
                # Update local entries and refresh view
                self.all_entries = [e for e in self.all_entries if e['pid'] != pid]
                self.total_pages = max(1, (len(self.all_entries) + self.page_size - 1) // self.page_size)
                if self.current_page >= self.total_pages:
                    self.current_page = max(0, self.total_pages - 1)
                self._build()
                await interaction.edit_original_response(view=self)
                await self.cog._refresh_dashboard()
            else:
                await interaction.followup.send("❌ Player not found on watchlist.", ephemeral=True)


# ---------------------------------------------------------------------------
# Report Player Modal
# ---------------------------------------------------------------------------
class ReportPlayerModal(Modal, title="Report Player(s)"):
    """Modal to report one or more players by UID or nickname with a reason."""

    identifiers = TextInput(
        label="Player UIDs or Nicknames",
        placeholder="e.g. 4036668451, 4025937269, 0032906407, TjTreacher",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    reason = TextInput(
        label="Reason for Report",
        placeholder="e.g. Deathtrap, coop request closed, etc.",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, cog: "MarketCog"):
        super().__init__(timeout=300)
        self.cog = cog
        self.resolved_players: List[Dict[str, Any]] = []
        self.banned_skipped: List[Dict[str, Any]] = []

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.identifiers.value.strip()
        reason_text = self.reason.value.strip()
        if not raw:
            await interaction.response.send_message("❌ Please enter at least one UID or nickname.", ephemeral=True)
            return
        if not reason_text:
            await interaction.response.send_message("❌ Please provide a reason for the report.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        entries = [part.strip() for part in raw.split(",") if part.strip()]
        if not entries:
            await interaction.followup.send("❌ No valid entries found.", ephemeral=True)
            return

        self.resolved_players = []
        self.banned_skipped = []
        banned_pids = await self.cog._get_banned_pids()

        for entry in entries:
            pid = None
            nickname = None
            number_id = None
            if entry.isdigit():
                try:
                    player_data = await get_player_info(entry, fields=["base", "hoard_profiteer", "space_data"])
                    if player_data and 'result' in player_data and 'base' in player_data['result']:
                        pid = player_data['result'].get('id')
                        base = player_data['result'].get('base', {})
                        nickname = base.get('nickname')
                        number_id = base.get('number_id')
                except Exception as e:
                    logger.debug(f"Report lookup failed for number_id {entry}: {e}")
            if not pid:
                try:
                    nick_data = await find_people_by_nickname(entry)
                    if nick_data and 'result' in nick_data:
                        pid = nick_data['result'].get('id')
                        if pid:
                            nickname = nick_data['result'].get('nickname', nickname or entry)
                            number_id = nick_data['result'].get('number_id', number_id)
                except Exception as e:
                    logger.debug(f"Report lookup failed for nickname {entry}: {e}")

            if pid:
                player_info = {
                    "pid": pid,
                    "nickname": nickname or entry,
                    "number_id": str(number_id) if number_id else "",
                }
                if pid in banned_pids:
                    self.banned_skipped.append(player_info)
                else:
                    self.resolved_players.append(player_info)

        if not self.resolved_players and not self.banned_skipped:
            await interaction.followup.send("❌ Could not resolve any entries to players.", ephemeral=True)
            return

        # Build confirmation view
        lines = ["# 🚨 Confirm Player Report", f"**Reason:** {reason_text}\n"]

        if self.resolved_players:
            lines.append(f"**{len(self.resolved_players)} player(s) to report:**\n")
            for p in self.resolved_players:
                lines.append(f"• **{p['nickname']}** (`{p['number_id']}`)")
            lines.append("")

        if self.banned_skipped:
            lines.append(f"⚠️ **{len(self.banned_skipped)} player(s) already banned — skipped:**\n")
            for p in self.banned_skipped:
                lines.append(f"• ~~**{p['nickname']}** (`{p['number_id']}`)~~ — already banned")
            lines.append("")

        lines.append("Please confirm to send the report to staff for review.")

        container = Container(
            TextDisplay("\n".join(lines)),
            accent_color=ACCENT_RED,
        )
        confirm_view = _ReportConfirmView(
            cog=self.cog,
            players=self.resolved_players,
            reason=reason_text,
            reporter_id=interaction.user.id,
        )
        confirm_layout = LayoutView(timeout=120)
        confirm_layout.add_item(container)
        confirm_layout.add_item(confirm_view.action_row)
        await interaction.followup.send(view=confirm_layout, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"ReportPlayerModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
        except Exception:
            pass


class _ReportConfirmView:
    """Holds confirm/cancel buttons for report submission."""

    def __init__(self, cog: "MarketCog", players: List[Dict[str, Any]], reason: str, reporter_id: int):
        self.cog = cog
        self.players = players
        self.reason = reason
        self.reporter_id = reporter_id

        self.action_row = ActionRow()
        approve_btn = Button(
            label="✅ Confirm – Send Report",
            style=discord.ButtonStyle.danger,
            custom_id="report_confirm_send",
        )
        approve_btn.callback = self._on_confirm
        self.action_row.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="report_cancel",
        )
        reject_btn.callback = self._on_cancel
        self.action_row.add_item(reject_btn)

    def _disable(self):
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    async def _on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.reporter_id:
            await interaction.response.send_message("❌ Only the person who submitted can confirm.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

        # Insert report
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO market_reports (reporter_id, reason, created_at) VALUES (?, ?, ?)",
                (self.reporter_id, self.reason, now_ts)
            )
            report_id = cursor.lastrowid

            # Insert each player
            for p in self.players:
                await db.execute(
                    "INSERT INTO market_report_players (report_id, pid, nickname, number_id, status) VALUES (?, ?, ?, ?, 'pending')",
                    (report_id, p['pid'], p['nickname'], p['number_id'])
                )
            await db.commit()

        self._disable()
        container = Container(
            TextDisplay(
                f"✅ **Report Submitted**\n\n"
                f"**{len(self.players)} player(s)** reported to staff for review.\n"
                f"**Reason:** {self.reason}"
            ),
            accent_color=ACCENT_GREEN,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(container)
        await interaction.edit_original_response(view=done_view)

        logger.info(f"Market report #{report_id} submitted by {interaction.user} for {len(self.players)} players")

        # Send to approval channel
        await self.cog._send_report_to_approval_channel(
            interaction=interaction,
            report_id=report_id,
            players=self.players,
            reason=self.reason,
            reporter_id=self.reporter_id,
        )

        # Refresh dashboard to show [REPORTED ⚠️] warnings
        await self.cog._refresh_dashboard()

    async def _on_cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.reporter_id:
            await interaction.response.send_message("❌ Only the person who submitted can cancel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        self._disable()
        container = Container(
            TextDisplay("❌ **Report Cancelled**\n\nNo report was submitted."),
            accent_color=ACCENT_RED,
        )
        done_view = LayoutView(timeout=None)
        done_view.add_item(container)
        await interaction.edit_original_response(view=done_view)


# ---------------------------------------------------------------------------
# Rejection Reason Modal
# ---------------------------------------------------------------------------
class RejectionReasonModal(Modal, title="Reject Report — Provide Reason"):
    """Modal for mod to provide a reason when rejecting a report."""

    rejection_reason = TextInput(
        label="Rejection Reason",
        placeholder="e.g. Insufficient evidence, false report...",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, cog: "MarketCog", report_player_id: int, pid: str, nickname: str, number_id: str, report_id: int, admin_msg_view: LayoutView):
        super().__init__(timeout=300)
        self.cog = cog
        self.report_player_id = report_player_id
        self.pid = pid
        self.nickname = nickname
        self.number_id = number_id
        self.report_id = report_id
        self.admin_msg_view = admin_msg_view

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.rejection_reason.value.strip()
        if not reason:
            await interaction.response.send_message("❌ Reason cannot be empty.", ephemeral=True)
            return

        await interaction.response.defer()

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE market_report_players SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, rejection_reason = ? WHERE id = ?",
                (interaction.user.id, now_ts, reason, self.report_player_id)
            )
            await db.commit()

        # Update the admin message to show rejection
        await self._update_admin_message(interaction, reason)

        logger.info(f"Report player {self.nickname} ({self.pid}) REJECTED by {interaction.user}: {reason}")
        await self.cog._refresh_dashboard()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"RejectionReasonModal error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
        except Exception:
            pass

    async def _update_admin_message(self, interaction: discord.Interaction, reason: str):
        """Update the admin approval message to show this player was rejected."""
        # We need to find and update the specific player's section in the admin message
        # The admin_msg_view contains the container with all players
        # We'll rebuild the view with updated status
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT rp.id, rp.pid, rp.nickname, rp.number_id, rp.status, rp.reviewed_by, rp.rejection_reason, "
                "mr.reason as report_reason, mr.reporter_id "
                "FROM market_report_players rp "
                "JOIN market_reports mr ON rp.report_id = mr.id "
                "WHERE rp.report_id = ? ORDER BY rp.id",
                (self.report_id,)
            )
            rows = await cursor.fetchall()

        if not rows:
            return

        players_data = []
        for row in rows:
            players_data.append({
                "id": row["id"],
                "pid": row["pid"],
                "nickname": row["nickname"],
                "number_id": row["number_id"],
                "status": row["status"],
                "reviewed_by": row["reviewed_by"],
                "rejection_reason": row["rejection_reason"],
            })

        report_reason = rows[0]["report_reason"]
        reporter_id = rows[0]["reporter_id"]

        # Build new approval view
        new_view = _ReportApprovalView(
            cog=self.cog,
            report_id=self.report_id,
            players=players_data,
            reason=report_reason,
            reporter_id=reporter_id,
        )
        await interaction.edit_original_response(view=new_view)


# ---------------------------------------------------------------------------
# Report Approval View (per-player approve/reject)
# ---------------------------------------------------------------------------
class _ReportApprovalView(LayoutView):
    """LayoutView with per-player approve/reject buttons for a report."""

    def __init__(self, cog: "MarketCog", report_id: int, players: List[Dict[str, Any]], reason: str, reporter_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.report_id = report_id
        self.players = players
        self.reason = reason
        self.reporter_id = reporter_id

        self._build()

    def _build(self) -> None:
        self.clear_items()
        inner_items: list = []

        # Header
        lines = [
            f"# 🚨 Player Report #{self.report_id}",
            f"**Reason:** {self.reason}",
            f"**Reported by:** <@{self.reporter_id}>",
            f"**Players:** {len(self.players)}\n",
        ]
        inner_items.append(TextDisplay("\n".join(lines)))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        for p in self.players:
            status_emoji = {
                "pending": "⏳",
                "approved": "✅",
                "rejected": "❌",
            }.get(p["status"], "⏳")

            player_lines = [
                f"### {status_emoji} **{p['nickname']}** (`{p['number_id']}`)",
                f"**PID:** `{p['pid']}`",
                f"**Status:** `{p['status']}`",
            ]

            if p["status"] == "rejected" and p.get("rejection_reason"):
                player_lines.append(f"**Rejection Reason:** {p['rejection_reason']}")
            if p["status"] == "approved":
                player_lines.append(f"✅ **Approved** — removed from watchlist")
            if p["status"] == "rejected":
                player_lines.append(f"❌ **Rejected** — player remains on watchlist")

            inner_items.append(TextDisplay("\n".join(player_lines)))

            # Only show action buttons for pending players
            if p["status"] == "pending":
                action_row = ActionRow()
                approve_ban_btn = Button(
                    label="✅ Approve & Ban",
                    style=discord.ButtonStyle.success,
                    custom_id=f"report_approve_ban:{self.report_id}:{p['id']}:{p['pid'][:20]}",
                )
                approve_ban_btn.callback = self._make_approve_ban_callback(p)
                action_row.add_item(approve_ban_btn)

                approve_remove_btn = Button(
                    label="✅ Approve & Remove",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"report_approve_remove:{self.report_id}:{p['id']}:{p['pid'][:20]}",
                )
                approve_remove_btn.callback = self._make_approve_remove_callback(p)
                action_row.add_item(approve_remove_btn)

                reject_btn = Button(
                    label="❌ Reject",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"report_reject:{self.report_id}:{p['id']}:{p['pid'][:20]}",
                )
                reject_btn.callback = self._make_reject_callback(p)
                action_row.add_item(reject_btn)

                inner_items.append(action_row)

            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        container = Container(*inner_items, accent_color=ACCENT_RED)
        self.add_item(container)

    def _make_approve_ban_callback(self, player: Dict[str, Any]):
        async def callback(interaction: discord.Interaction):
            if not await is_admin_or_staff(interaction):
                await interaction.response.send_message("❌ Admins only.", ephemeral=True)
                return
            await interaction.response.defer()

            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            pid = player["pid"]
            nickname = player["nickname"]
            number_id = player["number_id"]

            async with aiosqlite.connect(DB_PATH) as db:
                # Update report status
                await db.execute(
                    "UPDATE market_report_players SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                    (interaction.user.id, now_ts, player["id"])
                )
                # Remove from watchlist if present
                await db.execute("DELETE FROM market_watchlist WHERE pid = ?", (pid,))
                # Add to banned list
                await db.execute(
                    "REPLACE INTO market_banned_list (pid, nickname, number_id, banned_by, banned_at, reason) VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, nickname, number_id, interaction.user.id, now_ts, self.reason)
                )
                await db.commit()

            logger.info(f"Report player {nickname} ({pid}) APPROVED and banned by {interaction.user}")

            # Rebuild the admin message with updated status
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT rp.id, rp.pid, rp.nickname, rp.number_id, rp.status, rp.reviewed_by, rp.rejection_reason "
                    "FROM market_report_players rp WHERE rp.report_id = ? ORDER BY rp.id",
                    (self.report_id,)
                )
                rows = await cursor.fetchall()

            updated_players = [dict(row) for row in rows]
            new_view = _ReportApprovalView(
                cog=self.cog,
                report_id=self.report_id,
                players=updated_players,
                reason=self.reason,
                reporter_id=self.reporter_id,
            )
            await interaction.edit_original_response(view=new_view)

            await self.cog._refresh_dashboard()

        return callback

    def _make_approve_remove_callback(self, player: Dict[str, Any]):
        """Approve report: remove from watchlist but do NOT add to banned list."""
        async def callback(interaction: discord.Interaction):
            if not await is_admin_or_staff(interaction):
                await interaction.response.send_message("❌ Admins only.", ephemeral=True)
                return
            await interaction.response.defer()

            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            pid = player["pid"]
            nickname = player["nickname"]
            number_id = player["number_id"]

            async with aiosqlite.connect(DB_PATH) as db:
                # Update report status to 'approved'
                await db.execute(
                    "UPDATE market_report_players SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                    (interaction.user.id, now_ts, player["id"])
                )
                # Remove from watchlist if present
                await db.execute("DELETE FROM market_watchlist WHERE pid = ?", (pid,))
                # Do NOT add to banned list - just approve and remove
                await db.commit()

            logger.info(f"Report player {nickname} ({pid}) APPROVED and removed (no ban) by {interaction.user}")

            # Rebuild the admin message with updated status
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT rp.id, rp.pid, rp.nickname, rp.number_id, rp.status, rp.reviewed_by, rp.rejection_reason "
                    "FROM market_report_players rp WHERE rp.report_id = ? ORDER BY rp.id",
                    (self.report_id,)
                )
                rows = await cursor.fetchall()

            updated_players = [dict(row) for row in rows]
            new_view = _ReportApprovalView(
                cog=self.cog,
                report_id=self.report_id,
                players=updated_players,
                reason=self.reason,
                reporter_id=self.reporter_id,
            )
            await interaction.edit_original_response(view=new_view)

            await self.cog._refresh_dashboard()

        return callback

    def _make_reject_callback(self, player: Dict[str, Any]):
        async def callback(interaction: discord.Interaction):
            if not await is_admin_or_staff(interaction):
                await interaction.response.send_message("❌ Admins only.", ephemeral=True)
                return

            # Open rejection reason modal
            modal = RejectionReasonModal(
                cog=self.cog,
                report_player_id=player["id"],
                pid=player["pid"],
                nickname=player["nickname"],
                number_id=player["number_id"],
                report_id=self.report_id,
                admin_msg_view=self,
            )
            await interaction.response.send_modal(modal)

        return callback


# ---------------------------------------------------------------------------
# Banned List Paginated View
# ---------------------------------------------------------------------------
class BannedListPaginatedView(LayoutView):
    """Paginated view for the market banned list showing 10 entries per page."""

    def __init__(
        self,
        cog: "MarketCog",
        all_entries: List[Dict[str, Any]],
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.all_entries = all_entries
        self.page_size = 10
        self.current_page = 0
        self.total_pages = max(1, (len(all_entries) + self.page_size - 1) // self.page_size)

        self._build()

    def _build(self) -> None:
        self.clear_items()

        start = self.current_page * self.page_size
        end = min((self.current_page + 1) * self.page_size, len(self.all_entries))
        page = self.all_entries[start:end]

        lines: List[str] = []
        lines.append(f"# 🚫 Market Banned List\n")
        lines.append(f"**Total:** {len(self.all_entries)} entries  •  **Page {self.current_page + 1}/{self.total_pages}** (showing {start + 1}-{end})\n")

        if not page:
            lines.append("No entries on this page.")
        else:
            for entry in page:
                lines.append(
                    f"• **{entry['nickname']}** ({entry['number_id']})  —  "
                    f"banned <t:{entry['banned_at']}:R> by <@{entry['banned_by']}>"
                )
                if entry.get('reason'):
                    lines.append(f"  **Reason:** {entry['reason']}")
                lines.append("")

        inner: list = []
        inner.append(TextDisplay("\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Remove button (only for entries on current page)
        remove_options: List[discord.SelectOption] = []
        for entry in page:
            label = f"{entry['nickname']} ({entry['number_id']})"[:90]
            remove_options.append(
                discord.SelectOption(
                    label=label,
                    description=f"Remove from banned list",
                    value=str(entry['pid']),
                )
            )

        if remove_options:
            remove_row = ActionRow()
            remove_select = Select(
                placeholder="Select a player to remove from banned list…",
                options=remove_options[:25],
                custom_id="banned_remove_select",
            )
            remove_select.callback = self._on_remove
            remove_row.add_item(remove_select)
            inner.append(remove_row)

        # Navigation buttons
        nav_row = ActionRow()
        prev_btn = Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="banned_prev",
            disabled=self.current_page == 0,
        )
        prev_btn.callback = self._on_prev
        nav_row.add_item(prev_btn)

        page_btn = Button(
            label=f"{self.current_page + 1}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            custom_id="banned_page",
            disabled=True,
        )
        nav_row.add_item(page_btn)

        next_btn = Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="banned_next",
            disabled=self.current_page >= self.total_pages - 1,
        )
        next_btn.callback = self._on_next
        nav_row.add_item(next_btn)
        inner.append(nav_row)

        container = Container(*inner, accent_color=ACCENT_RED)
        self.add_item(container)

    async def _on_remove(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        pid = interaction.data.get("values", [None])[0]
        if not pid:
            return

        async with aiosqlite.connect(self.cog.db_path) as db:
            cursor = await db.execute("SELECT nickname FROM market_banned_list WHERE pid = ?", (pid,))
            row = await cursor.fetchone()
            if row:
                nickname = row[0]
                await db.execute("DELETE FROM market_banned_list WHERE pid = ?", (pid,))
                await db.commit()
                await interaction.followup.send(f"✅ Removed **{nickname}** from the banned list.", ephemeral=True)
                # Update local entries and refresh view
                self.all_entries = [e for e in self.all_entries if e['pid'] != pid]
                self.total_pages = max(1, (len(self.all_entries) + self.page_size - 1) // self.page_size)
                if self.current_page >= self.total_pages:
                    self.current_page = max(0, self.total_pages - 1)
                self._build()
                await interaction.edit_original_response(view=self)
                await self.cog._refresh_dashboard()
            else:
                await interaction.followup.send("❌ Player not found on banned list.", ephemeral=True)

    async def _on_prev(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self._build()
            await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._build()
            await interaction.response.edit_message(view=self)


# ---------------------------------------------------------------------------
# Goods Paginated View
# ---------------------------------------------------------------------------
class GoodsPaginatedView(LayoutView):
    """Paginated view for mapped goods, 10 per page."""

    def __init__(self, rows: List[aiosqlite.Row]):
        super().__init__(timeout=180)
        self.rows = rows
        self.page_size = 10
        self.current_page = 0
        self.total_pages = max(1, (len(rows) + self.page_size - 1) // self.page_size)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.rows))
        page = self.rows[start:end]

        lines: List[str] = []
        lines.append(f"# 📦 Mapped Goods ({len(self.rows)} total)")
        lines.append(f"**Page {self.current_page + 1}/{self.total_pages}** (showing {start + 1}–{end})\n")

        for row in page:
            lines.append(f"• **{row['name']}** (`{row['good_id']}`)")

        inner: list = []
        inner.append(TextDisplay("\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Navigation
        nav_row = ActionRow()
        prev_btn = Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page == 0,
        )
        prev_btn.callback = self._on_prev
        nav_row.add_item(prev_btn)

        page_btn = Button(
            label=f"{self.current_page + 1}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        nav_row.add_item(page_btn)

        next_btn = Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page >= self.total_pages - 1,
        )
        next_btn.callback = self._on_next
        nav_row.add_item(next_btn)
        inner.append(nav_row)

        container = Container(*inner, accent_color=ACCENT_BLURPLE)
        self.add_item(container)

    async def _on_prev(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self._build()
            await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._build()
            await interaction.response.edit_message(view=self)


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
                CREATE TABLE IF NOT EXISTS market_watchlist (
                    pid TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    number_id TEXT NOT NULL DEFAULT '',
                    added_by INTEGER,
                    added_at INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_cache (
                    club_id INTEGER NOT NULL,
                    hostnum INTEGER NOT NULL DEFAULT 10595,
                    guild_name TEXT NOT NULL,
                    last_updated INTEGER NOT NULL,
                    PRIMARY KEY (club_id, hostnum)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_report_players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    pid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    number_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_by INTEGER,
                    reviewed_at INTEGER,
                    rejection_reason TEXT,
                    FOREIGN KEY (report_id) REFERENCES market_reports(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_banned_list (
                    pid TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    number_id TEXT NOT NULL DEFAULT '',
                    banned_by INTEGER NOT NULL,
                    banned_at INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS market_guild_watchlist (
                    club_id INTEGER NOT NULL,
                    hostnum INTEGER NOT NULL DEFAULT 10595,
                    guild_name TEXT NOT NULL DEFAULT 'Unknown',
                    added_by INTEGER NOT NULL,
                    added_at INTEGER NOT NULL,
                    PRIMARY KEY (club_id, hostnum)
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

    # -- Report / Ban helpers ---------------------------------------------
    async def _get_banned_pids(self) -> Set[str]:
        """Return the set of PIDs on the banned list."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT pid FROM market_banned_list")
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def _is_player_banned(self, pid: str) -> bool:
        """Check if a player is on the banned list."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT 1 FROM market_banned_list WHERE pid = ?", (pid,))
            return await cursor.fetchone() is not None

    async def _get_pending_report_pids(self) -> Set[str]:
        """Return the set of PIDs that have pending reports."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT DISTINCT pid FROM market_report_players WHERE status = 'pending'"
            )
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def _get_banned_list(self) -> List[Dict[str, Any]]:
        """Return all entries from the banned list, ordered by banned_at DESC."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT pid, nickname, number_id, banned_by, banned_at, reason FROM market_banned_list ORDER BY banned_at DESC"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # -- Guild Watchlist helpers ------------------------------------------
    async def _get_guild_watchlist(self) -> List[Dict[str, Any]]:
        """Return all guilds from the market_guild_watchlist table."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT club_id, hostnum, guild_name, added_by, added_at FROM market_guild_watchlist ORDER BY added_at DESC"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _add_guild_to_watchlist(self, club_id: int, hostnum: int, guild_name: str, user_id: int):
        """Add or update a guild in the market_guild_watchlist."""
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "REPLACE INTO market_guild_watchlist (club_id, hostnum, guild_name, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                (club_id, hostnum, guild_name, user_id, now_ts)
            )
            await db.commit()

    async def _remove_guild_from_watchlist(self, club_id: int, hostnum: int):
        """Remove a guild from the market_guild_watchlist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM market_guild_watchlist WHERE club_id = ? AND hostnum = ?",
                (club_id, hostnum)
            )
            await db.commit()

    async def _get_bound_player_info(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get the bound player's PID and guild info from verification DB."""
        try:
            async with aiosqlite.connect(VERIFICATION_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT player_pid, character_uid FROM verified_members WHERE user_id = ? AND player_pid IS NOT NULL",
                    (user_id,)
                )
                row = await cursor.fetchone()
                if not row:
                    logger.debug(f"No bound player found for user {user_id}")
                    return None
                
                player_pid = row[0]
                character_uid = row[1]
                logger.debug(f"Found bound player PID {player_pid} (UID: {character_uid}) for user {user_id}")
                
                # Use get_player_info with explicit credentials (same pattern as guild_verification_cog)
                try:
                    player_data = await get_player_info(
                        character_uid,
                        uid=WWM_UID,
                        token=WWM_TOKEN,
                        api_url=WWM_API_URL,
                        fields=["base", "club"]
                    )
                    if player_data and 'result' in player_data:
                        result = player_data['result']
                        base = result.get('base', {})
                        club_info = result.get('club', {})
                        
                        return {
                            'pid': player_pid,
                            'nickname': base.get('nickname'),
                            'club_id': club_info.get('club_id', 0),
                            'hostnum': club_info.get('hostnum', 10595)
                        }
                    else:
                        logger.warning(f"Failed to fetch player data for UID {character_uid}: no result")
                        return None
                except Exception as e:
                    logger.error(f"Failed to fetch player info for bound player {character_uid}: {e}", exc_info=True)
                    return None
        except Exception as e:
            logger.error(f"Failed to lookup bound player for user {user_id}: {e}", exc_info=True)
            return None

    # -- Cog lifecycle ----------------------------------------------------
    async def cog_load(self):
        await self._init_database()
        await self._load_config()
        if not self.daily_market_report.is_running():
            self.daily_market_report.start()
        if not self.refresh_guild_names.is_running():
            self.refresh_guild_names.start()

    async def cog_unload(self):
        if self.daily_market_report.is_running():
            self.daily_market_report.cancel()
        if self.refresh_guild_names.is_running():
            self.refresh_guild_names.cancel()

    # -- Force refresh ----------------------------------------------------
    async def _refresh_dashboard(self):
        """Force refresh the market report dashboard message (if it exists)."""
        try:
            await self._build_and_send_report()
        except Exception as e:
            logger.error(f"Market cog: dashboard refresh failed: {e}", exc_info=True)

    # -- Guild name cache -------------------------------------------------
    async def _resolve_guild_name(self, club_id: int, hostnum: int = 10595) -> str:
        """Look up guild name from cache; fetch and store if not cached."""
        if not club_id:
            return 'Unknown'
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT guild_name FROM guild_cache WHERE club_id = ? AND hostnum = ?",
                (club_id, hostnum)
            )
            row = await cursor.fetchone()
            if row:
                return row[0]
        # Not in cache — fetch from API and store
        guild_name = 'Unknown'
        try:
            guild_full_data = await get_full_guild_info(club_id, hostnum=hostnum, fields={'base': []})
            if guild_full_data and 'result' in guild_full_data:
                guild_name = guild_full_data['result'].get('base', {}).get('name', 'Unknown')
        except Exception as e:
            logger.debug(f"Market cog: failed to fetch guild info for club {club_id}: {e}")
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "REPLACE INTO guild_cache (club_id, hostnum, guild_name, last_updated) VALUES (?, ?, ?, ?)",
                (club_id, hostnum, guild_name, now_ts)
            )
            await db.commit()
        logger.debug(f"Market cog: cached guild name '{guild_name}' for club {club_id} (hostnum {hostnum})")
        return guild_name

    async def _refresh_all_guild_names(self):
        """Re-fetch all cached guild names to detect renames. Run once daily."""
        logger.info("Market cog: starting daily guild name refresh")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT club_id, hostnum, guild_name FROM guild_cache")
            rows = await cursor.fetchall()

        if not rows:
            logger.info("Market cog: no cached guild names to refresh")
            return

        from utility.wwm import get_bulk_guild_names
        targets = [(club_id, hostnum) for club_id, hostnum, _ in rows]
        old_names_map = {(club_id, hostnum): old_name for club_id, hostnum, old_name in rows}

        # Concurrently fetch all guild names
        names_map = await get_bulk_guild_names(targets, max_concurrency=10)

        updated = 0
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            for (cid, hnum), new_name in names_map.items():
                old_name = old_names_map.get((cid, hnum), "Unknown")
                if new_name != old_name:
                    await db.execute(
                        "REPLACE INTO guild_cache (club_id, hostnum, guild_name, last_updated) VALUES (?, ?, ?, ?)",
                        (cid, hnum, new_name, now_ts)
                    )
                    logger.info(f"Market cog: guild renamed — club {cid}: '{old_name}' → '{new_name}'")
                    updated += 1
            await db.commit()
        logger.info(f"Market cog: daily guild name refresh complete — {len(rows)} checked, {updated} updated")

    # -- Core data fetching ------------------------------------------------
    async def _get_all_player_pids(self) -> List[str]:
        """Collect PIDs from guild watchlist members + bound players + watchlist, deduplicated."""
        guild_pids = set()
        
        # Fetch all guilds from guild watchlist
        guild_watchlist = await self._get_guild_watchlist()
        if guild_watchlist:
            logger.debug(f"Market cog: fetching members from {len(guild_watchlist)} guild(s) in watchlist")
            # Fetch members from each guild in the watchlist concurrently
            guild_fetch_tasks = []
            for g in guild_watchlist:
                guild_fetch_tasks.append(self._fetch_guild_members(g['club_id'], g['hostnum']))
            
            guild_member_sets = await asyncio.gather(*guild_fetch_tasks, return_exceptions=True)
            for member_set in guild_member_sets:
                if isinstance(member_set, set):
                    guild_pids.update(member_set)
                elif isinstance(member_set, Exception):
                    logger.error(f"Market cog: failed to fetch guild members: {member_set}")
        else:
            # Fallback: if no guilds in watchlist, use the hardcoded CLUB_ID
            logger.debug("Market cog: no guilds in watchlist, falling back to CLUB_ID")
            try:
                guild_data = await get_full_guild_info(CLUB_ID)
                if guild_data and 'result' in guild_data:
                    members = guild_data['result'].get('members', {}).get('members', {})
                    guild_pids.update(members.keys())
            except Exception as e:
                logger.error(f"Market cog: failed to fetch fallback guild members: {e}")

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

    async def _fetch_guild_members(self, club_id: int, hostnum: int = 10595) -> Set[str]:
        """Fetch member PIDs from a specific guild."""
        member_pids = set()
        try:
            guild_data = await get_full_guild_info(club_id, hostnum=hostnum)
            if guild_data and 'result' in guild_data:
                members = guild_data['result'].get('members', {}).get('members', {})
                member_pids.update(members.keys())
                logger.debug(f"Market cog: fetched {len(member_pids)} members from guild {club_id} (hostnum {hostnum})")
        except Exception as e:
            logger.error(f"Market cog: failed to fetch guild members for club {club_id} (hostnum {hostnum}): {e}")
        return member_pids

    @staticmethod
    def _get_day_start_ts() -> int:
        """Return the UNIX timestamp of today's 6am GMT+8 (or yesterday's 6am if before 6am)."""
        GMT8_OFFSET = 8 * 3600
        now_utc_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        gmt8_now_ts = now_utc_ts + GMT8_OFFSET
        gmt8_dt = datetime.datetime.fromtimestamp(gmt8_now_ts, tz=datetime.timezone.utc)
        # If current GMT+8 hour < 6, use yesterday's 6am; else today's 6am
        if gmt8_dt.hour < 6:
            day_start = (gmt8_dt - datetime.timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        else:
            day_start = gmt8_dt.replace(hour=6, minute=0, second=0, microsecond=0)
        return int(day_start.timestamp() - GMT8_OFFSET)

    async def _fetch_and_process(self, day_number: int = 1) -> Optional[Dict[str, Any]]:
        """Fetch hoard data and determine market state.

        Logic:
          - Filter qualifying players: online AND/OR logged out within today (since 6am GMT+8).
          - If day_number == 1 AND any qualifying player has only 1 data point in price_change_history,
            it's a new week: return {"mode": "new_week", "good_ids": [...]} with
            goods from that player's average_price keys.
          - Otherwise: return {"mode": "active", "groups": {good_id: [(player_data...), ...]}}
            Each player tuple: (pid, nickname, number_id, original_price, current_price, pct, is_online, hostnum, guild_name, mode, other_search)

        Returns None if no qualifying player data.
        """
        all_pids = await self._get_all_player_pids()
        if not all_pids:
            logger.warning("Market cog: no player PIDs to fetch")
            return None

        raw_data = await get_bulk_hoard_data(all_pids)
        if not raw_data or 'result' not in raw_data:
            logger.warning("Market cog: bulk hoard fetch returned no data")
            return None

        players_data = raw_data['result']
        if not players_data:
            logger.warning("Market cog: empty player data from bulk hoard fetch")
            return None

        day_start_ts = self._get_day_start_ts()

        # First pass: collect all unique (club_id, hostnum) pairs and
        # identify which ones are *not* already cached so we can fetch
        # them in one concurrent batch.
        uncached_targets: List[Tuple[int, int]] = []
        seen_clubs: Set[Tuple[int, int]] = set()
        for pid, player_entry in players_data.items():
            club_info = player_entry.get('club', {}) if isinstance(player_entry, dict) else {}
            club_id = club_info.get('club_id', 0)
            club_hostnum = club_info.get('hostnum', 10595)
            if club_id != 0 and (club_id, club_hostnum) not in seen_clubs:
                seen_clubs.add((club_id, club_hostnum))
                # Check cache first
                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute(
                        "SELECT 1 FROM guild_cache WHERE club_id = ? AND hostnum = ?",
                        (club_id, club_hostnum)
                    )
                    row = await cursor.fetchone()
                    if not row:
                        uncached_targets.append((club_id, club_hostnum))

        # Concurrently fetch all uncached guild names in one batch
        if uncached_targets:
            from utility.wwm import get_bulk_guild_names
            names_map = await get_bulk_guild_names(uncached_targets, max_concurrency=10)
            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            async with aiosqlite.connect(self.db_path) as db:
                for (cid, hnum), name in names_map.items():
                    await db.execute(
                        "REPLACE INTO guild_cache (club_id, hostnum, guild_name, last_updated) VALUES (?, ?, ?, ?)",
                        (cid, hnum, name, now_ts)
                    )
                await db.commit()

        # Second pass: build qualifying players list (cache hit, no API calls)
        qualifying_players: List[Dict] = []
        for pid, player_entry in players_data.items():
            base = player_entry.get('base', {}) if isinstance(player_entry, dict) else {}
            # hostnum is in space_data
            player_hostnum = player_entry.get("space_data").get("space_hostnum", 10595)
            club_info = player_entry.get('club', {}) if isinstance(player_entry, dict) else {}
            club_id = club_info.get('club_id', 0)
            club_hostnum = club_info.get('hostnum', 10595)
            guild_name = 'Unknown'
            if club_id != 0:
                guild_name = await self._resolve_guild_name(club_id, club_hostnum)
            is_online = base.get('is_online', 0) == 1
            logout_time = base.get('logout_time', 0) or base.get('last_online_ts', 0)
            if not is_online and logout_time < day_start_ts:
                continue  # not online and not logged out within today
            qualifying_players.append({
                'pid': pid,
                'player_entry': player_entry,
                'base': base,
                'is_online': is_online,
                'guild_name': guild_name,
                'hostnum': player_hostnum,
                'mode': base.get('mode', 0)
            })

        if not qualifying_players:
            logger.warning("Market cog: no qualifying players found (online or logged out today)")
            return None

        # Only check for new week on Day 1 (Saturday)
        # After Day 1, always show active mode with player rankings
        new_week_good_ids: List[str] = []
        if day_number == 1:
            # Check for new week: first try to find an ONLINE player with 1 data point
            # and 3 average_price goods. If none, fallback to a player logged out within today.
            # Try online players first
            for qp in qualifying_players:
                if not qp['is_online']:
                    continue
                hoard = qp['player_entry'].get('hoard_profiteer', {}) if isinstance(qp['player_entry'], dict) else {}
                price_history = hoard.get('price_change_history', [])
                if len(price_history) == 1:
                    average_price = hoard.get('average_price', {})
                    if len(average_price) == 3:
                        new_week_good_ids = sorted(
                            (str(gid) for gid in average_price.keys()),
                            key=lambda x: int(x)
                        )[:3]
                        logger.debug(
                            f"Market cog: NEW WEEK detected via ONLINE player {qp['pid']} — "
                            f"goods: {new_week_good_ids}"
                        )
                        break
            # Fallback: any qualifying player with 1 data point and non-empty average_price
            if not new_week_good_ids:
                for qp in qualifying_players:
                    hoard = qp['player_entry'].get('hoard_profiteer', {}) if isinstance(qp['player_entry'], dict) else {}
                    price_history = hoard.get('price_change_history', [])
                    if len(price_history) == 1:
                        average_price = hoard.get('average_price', {})
                        if len(average_price) == 3:
                            new_week_good_ids = sorted(
                                (str(gid) for gid in average_price.keys()),
                                key=lambda x: int(x)
                            )[:3]
                            logger.debug(
                                f"Market cog: NEW WEEK detected via player {qp['pid']} — "
                                f"goods: {new_week_good_ids}"
                            )
                            break

        if new_week_good_ids:
            return {
                "mode": "new_week",
                "good_ids": new_week_good_ids,
                "qualifying_players": qualifying_players
            }
        else:
            # Active mode – build ranking groups for qualifying players with >=2 data points
            good_groups: Dict[str, List[Tuple[str, str, str, float, float, float, bool, int, str]]] = defaultdict(list)
            skipped_none = 0
            skipped_short_history = 0

            for qp in qualifying_players:
                pid = qp['pid']
                base = qp['base']
                nickname = base.get('nickname', 'Unknown')
                number_id = str(base.get('number_id', '')) if base.get('number_id') else ''
                hostnum = qp['hostnum']
                is_online = qp['is_online']

                hoard = qp['player_entry'].get('hoard_profiteer', {}) if isinstance(qp['player_entry'], dict) else {}
                if not hoard:
                    skipped_none += 1
                    continue

                main_good = str(hoard.get('main_good', ''))
                if not main_good:
                    skipped_none += 1
                    continue

                price_history = hoard.get('price_change_history', [])
                if len(price_history) < 2:
                    skipped_short_history += 1
                    continue

                original_price = float(price_history[0])
                current_price = float(price_history[-1])

                if original_price == 0:
                    skipped_none += 1
                    continue

                pct = ((current_price - original_price) / original_price) * 100.0

                other_search = base.get('other_search', 1)
                good_groups[main_good].append((
                    pid, nickname, number_id,
                    original_price, current_price, pct,
                    is_online, hostnum, qp['guild_name'],
                    qp['mode'], other_search
                ))

            logger.debug(
                f"Market cog: processed {len(qualifying_players)} qualifying players — "
                f"{skipped_none} no hoard data, {skipped_short_history} short history, "
                f"{sum(len(v) for v in good_groups.values())} included"
            )

            if not good_groups:
                logger.warning("Market cog: no players with valid hoard data found in active mode")
                return None

            for good_id in good_groups:
                good_groups[good_id].sort(key=lambda x: x[5], reverse=True)

            unique_goods = list(good_groups.keys())
            logger.debug(f"Market cog: found {len(unique_goods)} unique goods: {unique_goods}")
            if len(unique_goods) > 3:
                logger.warning(f"Market cog: expected at most 3 unique goods but found {len(unique_goods)}")

            return {
                "mode": "active",
                "groups": dict(good_groups),
                "qualifying_players": qualifying_players
            }

    # -- Build and send report --------------------------------------------
    async def _build_and_send_report(self):
        """Fetch data, determine mode, build appropriate view, send/edit to channel."""
        if not self.market_channel:
            logger.warning("Market cog: no channel configured, cannot send report")
            return

        # Get day number FIRST before fetching data
        day_number = self._get_market_day_number()
        
        result = await self._fetch_and_process(day_number=day_number)
        if not result:
            return

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        next_update_ts = self._get_next_update_ts()
        countdown_str = self._get_countdown_timestamp(next_update_ts)
        
        # day_number already calculated above
        
        known_goods = await self._get_known_goods()
        good_names_map = {}
        for gid in known_goods:
            name = await self._get_good_name(gid)
            if name:
                good_names_map[gid] = name

        if result['mode'] == 'new_week':
            good_ids = result['good_ids']
            view = NewWeekMarketView(
                cog=self,
                good_ids=good_ids,
                good_names_map=good_names_map,
                known_goods=known_goods,
                report_ts=now_ts,
                day_number=day_number,
                countdown_str=countdown_str,
            )
        else:
            grouped = result['groups']
            total_players = sum(len(v) for v in grouped.values())
            # Concurrently fetch market likes for all players
            likes_map = await self._fetch_all_market_likes(grouped)
            pending_report_pids = await self._get_pending_report_pids()
            view = MarketReportView(
                cog=self,
                grouped_data=grouped,
                total_players=total_players,
                report_ts=now_ts,
                next_update_ts=next_update_ts,
                day_number=day_number,
                countdown_str=countdown_str,
                known_goods=known_goods,
                good_names_map=good_names_map,
                likes_map=likes_map,
                pending_report_pids=pending_report_pids,
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

    @staticmethod
    def _get_market_day_number() -> int:
        """Calculate market day number based on calendar schedule.
        
        Market week starts on Saturday 6am GMT+8.
        - Saturday 6am GMT+8 = Day 1
        - Sunday 6am GMT+8 = Day 2
        - Monday 6am GMT+8 = Day 3
        - etc.
        
        Returns the current market day number (1-indexed).
        """
        GMT8_OFFSET = 8 * 3600
        now_utc_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        gmt8_now_ts = now_utc_ts + GMT8_OFFSET
        gmt8_dt = datetime.datetime.fromtimestamp(gmt8_now_ts, tz=datetime.timezone.utc)
        
        # Find the most recent Saturday 6am GMT+8
        # weekday(): Monday=0, Sunday=6, Saturday=5
        days_since_saturday = (gmt8_dt.weekday() - 5) % 7  # 0=Saturday, 1=Sunday, ..., 6=Friday
        
        if days_since_saturday == 0:
            # Today is Saturday
            if gmt8_dt.hour >= 6:
                # Already past 6am today = Day 1
                week_start = gmt8_dt.replace(hour=6, minute=0, second=0, microsecond=0)
            else:
                # Before 6am Saturday = still Friday's week, use last Saturday
                week_start = (gmt8_dt - datetime.timedelta(days=7)).replace(hour=6, minute=0, second=0, microsecond=0)
        else:
            # Not Saturday, use the most recent Saturday
            week_start = (gmt8_dt - datetime.timedelta(days=days_since_saturday)).replace(hour=6, minute=0, second=0, microsecond=0)
        
        # Calculate days since week start (each day = 1 market day)
        days_elapsed = (gmt8_dt - week_start).total_seconds() / 86400.0
        market_day = int(days_elapsed) + 1  # Day 1 on the first day
        
        return max(1, market_day)

    @staticmethod
    def _get_countdown_timestamp(next_update_ts: int) -> str:
        """Return Discord relative timestamp for countdown.
        
        Returns: "<t:1234567890:R>" which displays as "in 14 hours" / "in 23 minutes"
        """
        return f"<t:{next_update_ts}:R>"

    async def _fetch_market_likes(self, pid: str, hostnum: int = 10595) -> int:
        """Fetch market likes (topic 129) for a single player. Returns n_likes."""
        try:
            result = await get_topics_likes(pid, hostnum)
            if result and 'result' in result:
                likes_info = result['result']
                if isinstance(likes_info, dict):
                    # Handle both int and string keys from the API
                    topic_129 = None
                    for key in (129, "129"):
                        if key in likes_info:
                            topic_129 = likes_info[key]
                            break
                    if isinstance(topic_129, dict):
                        return topic_129.get('n_likes', 0)
        except Exception as e:
            logger.debug(f"Market cog: failed to fetch likes for PID {pid}: {e}")
        return 0

    async def _fetch_all_market_likes(self, grouped: Dict[str, List]) -> Dict[str, int]:
        """Fetch market likes for all players in every good group.
        Uses ``get_bulk_topics_likes`` to fire all requests concurrently.
        Returns dict mapping pid -> n_likes."""
        from utility.wwm import get_bulk_topics_likes

        # Collect unique (pid, hostnum) from ALL players (not just top N),
        # so the online-filter view shows likes for everyone correctly.
        seen: Dict[str, int] = {}
        for good_id, players in grouped.items():
            for player in players:
                pid = player[0]
                hostnum = player[7] if len(player) > 7 else 10595
                if pid not in seen:
                    seen[pid] = hostnum

        if not seen:
            return {}

        targets = list(seen.items())  # [(pid, hostnum), ...]
        logger.debug(f"Market cog: fetching likes for {len(targets)} players")
        likes_map = await get_bulk_topics_likes(targets, max_concurrency=10)
        logger.debug(f"Market cog: likes fetched — {sum(1 for v in likes_map.values() if v > 0)} players with likes, {sum(1 for v in likes_map.values() if v == 0)} with 0")
        return likes_map

    # -- Scheduled task ---------------------------------------------------
    @tasks.loop(minutes=1)
    async def daily_market_report(self):
        """Market report refreshes every 1 minute."""
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

    # -- Daily guild name refresh -----------------------------------------
    @tasks.loop(hours=24)
    async def refresh_guild_names(self):
        """Once a day, re-fetch all cached guild names to detect renames."""
        try:
            await self._refresh_all_guild_names()
        except Exception as e:
            logger.error(f"Market cog: daily guild name refresh failed: {e}", exc_info=True)

    @refresh_guild_names.before_loop
    async def before_refresh_guild_names(self):
        await self.bot.wait_until_ready()

    # -- Send report to approval channel ----------------------------------
    async def _send_report_to_approval_channel(
        self,
        interaction: discord.Interaction,
        report_id: int,
        players: List[Dict[str, Any]],
        reason: str,
        reporter_id: int,
    ):
        """Send a report to the admin approval channel for mod review."""
        admin_channel_id = getattr(self, '_admin_channel_id', None) or ADMIN_AVATAR_CHANNEL_ID
        admin_channel = interaction.guild.get_channel(admin_channel_id)
        if not admin_channel:
            logger.warning(f"Market cog: admin channel {admin_channel_id} not found for report approval")
            return

        approval_view = _ReportApprovalView(
            cog=self,
            report_id=report_id,
            players=[{
                "id": i + 1,
                "pid": p["pid"],
                "nickname": p["nickname"],
                "number_id": p["number_id"],
                "status": "pending",
                "reviewed_by": None,
                "rejection_reason": None,
            } for i, p in enumerate(players)],
            reason=reason,
            reporter_id=reporter_id,
        )

        await admin_channel.send(view=approval_view)
        logger.info(f"Market cog: report #{report_id} sent to approval channel {admin_channel_id}")

    # -- Slash commands ---------------------------------------------------
    @market_group.command(name="report", description="Force-trigger a fresh market price report")
    @admin_or_staff()
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
    @admin_or_staff()
    async def market_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Configure the channel where daily reports are posted."""
        self.market_channel = channel
        await self._save_config()
        await interaction.response.send_message(f"✅ Market report channel set to {channel.mention}", ephemeral=True)
        logger.info(f"Market cog: channel set to {channel.id} by {interaction.user}")

    @market_group.command(name="set-admin-channel", description="Set the admin channel for market approvals")
    @admin_or_staff()
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
            value="Every 1 minute" if self.daily_market_report.is_running() else "❌ Stopped",
            inline=True
        )
        embed.add_field(
            name="Message",
            value=f"[View latest]({self.last_report_message.jump_url})" if self.last_report_message else "None",
            inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @market_group.command(name="goods", description="Show all mapped goods with their IDs and names")
    async def market_goods(self, interaction: discord.Interaction):
        """Show all goods that have been mapped with their IDs and names (paginated, 10 per page)."""
        await interaction.response.defer()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT good_id, name, approved_by, approved_at FROM good_names ORDER BY CAST(good_id AS INTEGER)"
                )
                rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send("📦 No goods have been mapped yet.")
                return

            # Build paginated view
            view = GoodsPaginatedView(rows=rows)
            await interaction.followup.send(view=view)

        except Exception as e:
            logger.error(f"Market cog: goods command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    @market_group.command(name="watchlist", description="Show/remove manually added players on the market watchlist")
    @admin_or_staff()
    async def market_watchlist(self, interaction: discord.Interaction):
        """Show all manually-added players on the watchlist, with pagination (10 per page) and removal Select."""
        await interaction.response.defer()
        try:
            # Get guild + bound PIDs to filter out
            guild_pids = set()
            bound_pids = set()
            try:
                guild_data = await get_full_guild_info(CLUB_ID)
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

            all_entries: List[Dict[str, Any]] = []
            for row in rows:
                all_entries.append({
                    'pid': row['pid'],
                    'nickname': row['nickname'],
                    'number_id': row['number_id'],
                    'added_by': row['added_by'],
                    'added_at': row['added_at'],
                })

            # Fetch guild information for all watchlist entries
            logger.debug(f"Fetching guild info for {len(all_entries)} watchlist entries")
            pids_to_lookup = [entry['pid'] for entry in all_entries]
            guild_map: Dict[str, str] = {}
            
            try:
                # Use get_bulk_players_info to fetch all player data at once
                bulk_data = await get_bulk_players_info(pids_to_lookup, fields=["club"])
                if bulk_data and 'result' in bulk_data:
                    for pid, player_data in bulk_data['result'].items():
                        club_info = player_data.get('club', {})
                        club_id = club_info.get('club_id', 0)
                        hostnum = club_info.get('hostnum', 10595)
                        if club_id != 0:
                            guild_name = await self._resolve_guild_name(club_id, hostnum)
                            guild_map[pid] = guild_name
                        else:
                            guild_map[pid] = None
            except Exception as e:
                logger.warning(f"Failed to fetch bulk guild info for watchlist: {e}")

            view = WatchlistPaginatedView(
                cog=self,
                all_entries=all_entries,
                guild_pids=guild_pids,
                bound_pids=bound_pids,
                guild_map=guild_map,
            )
            await interaction.followup.send(view=view)

        except Exception as e:
            logger.error(f"Market cog: watchlist command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    @market_group.command(name="guild", description="Add your bound guild to the market guild watchlist")
    async def market_guild(self, interaction: discord.Interaction):
        """Add the guild of your bound player to the guild watchlist for live market tracking."""
        await interaction.response.defer(ephemeral=False)
        
        # Get bound player info
        bound_info = await self._get_bound_player_info(interaction.user.id)
        if not bound_info:
            await interaction.followup.send(
                "❌ You don't have a bound account. Please bind your account first using the verification system.",
                ephemeral=False
            )
            return
        
        if not bound_info.get('club_id'):
            await interaction.followup.send(
                "❌ Your bound player is not in a guild. Join a guild first and then try again.",
                ephemeral=False
            )
            return
        
        # Add to guild watchlist
        guild_name = await self._resolve_guild_name(bound_info['club_id'], bound_info.get('hostnum', 10595))
        await self._add_guild_to_watchlist(
            club_id=bound_info['club_id'],
            hostnum=bound_info.get('hostnum', 10595),
            guild_name=guild_name,
            user_id=interaction.user.id
        )
        
        logger.info(f"Market guild watchlist add: {guild_name} (club {bound_info['club_id']}) by {interaction.user}")
        
        # Refresh dashboard to include new guild members
        await self._refresh_dashboard()
        
        await interaction.followup.send(
            f"✅ **Guild Added to Market Watchlist**\n\n"
            f"• **Guild:** {guild_name}\n"
            f"All members of this guild will now be included in the market dashboard. "
            f"The member list updates live every refresh.",
            ephemeral=False
        )

    @market_group.command(name="player", description="Look up specific players' market stats")
    @app_commands.describe(
        query="One or more player UIDs or nicknames, comma-separated (optional if using the modal)",
    )
    async def market_player(self, interaction: discord.Interaction, query: str = ""):
        """Show market data for one or more players."""
        if query:
            # Direct lookup from slash command argument
            await self._bulk_player_lookup(interaction, query)
        else:
            # Open modal for bulk input
            modal = _BulkPlayerLookupModal(cog=self)
            await interaction.response.send_modal(modal)

    async def _bulk_player_lookup(self, interaction: discord.Interaction, raw: str):
        entries = [part.strip() for part in raw.split(",") if part.strip()]
        if not entries:
            await interaction.response.send_message("❌ Please enter at least one UID or nickname.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        resolved: List[Dict[str, Any]] = []

        # Resolve each entry
        for entry in entries:
            pid = None
            nickname = None
            number_id = None
            hostnum = 10595

            if entry.isdigit():
                try:
                    player_data = await get_player_info(entry, fields=["base", "hoard_profiteer", "space_data"])
                    if player_data and 'result' in player_data and 'base' in player_data['result']:
                        pid = player_data['result'].get('id')
                        base = player_data['result'].get('base', {})
                        nickname = base.get('nickname')
                        number_id = base.get('number_id')
                        hostnum = player_data['result'].get("space_data", {}).get("space_hostnum", 10595)
                except Exception as e:
                    logger.debug(f"Player lookup failed for number_id {entry}: {e}")

            if not pid:
                try:
                    nick_data = await find_people_by_nickname(entry)
                    if nick_data and 'result' in nick_data:
                        pid = nick_data['result'].get('id')
                        if pid:
                            nickname = nick_data['result'].get('nickname', nickname or entry)
                            number_id = nick_data['result'].get('number_id', number_id)
                            hostnum = nick_data["result"].get("hostnum", 10595)
                except Exception as e:
                    logger.debug(f"Player lookup failed for nickname {entry}: {e}")

            if pid:
                resolved.append({
                    "pid": pid,
                    "nickname": nickname or entry,
                    "number_id": str(number_id) if number_id else "",
                    "hostnum": hostnum,
                })

        if not resolved:
            await interaction.followup.send("❌ Could not resolve any entries to players.", ephemeral=True)
            return

        # Fetch hoard data for all resolved PIDs concurrently
        from utility.wwm import get_bulk_hoard_data
        pids = [r["pid"] for r in resolved]
        try:
            raw_data = await get_bulk_hoard_data(pids)
        except Exception as e:
            logger.error(f"Market cog: bulk hoard fetch failed: {e}")
            await interaction.followup.send("❌ Failed to fetch market data for resolved players.", ephemeral=True)
            return

        players_data = raw_data.get('result', {}) if raw_data else {}

        # Build final list with stats
        final_players: List[Dict[str, Any]] = []
        for r in resolved:
            pid = r["pid"]
            entry = players_data.get(pid, {})
            base = entry.get('base', {}) if isinstance(entry, dict) else {}
            hoard = entry.get('hoard_profiteer', {}) if isinstance(entry, dict) else {}
            if not hoard:
                continue

            nickname = base.get('nickname', r['nickname'])
            number_id = r['number_id'] or str(base.get('number_id', ''))
            main_good = str(hoard.get('main_good', '?'))
            price_history = hoard.get('price_change_history', [])
            total_profit = hoard.get('total_profit', 0)

            # Check if already on watchlist
            watchlist_pids = await self._get_watchlist_pids()
            is_on_watchlist = pid in watchlist_pids

            # Good name check
            known_goods = await self._get_known_goods()
            good_has_name = main_good in known_goods
            good_name = await self._get_good_name(main_good) if good_has_name else ""
            likes = await self._fetch_market_likes(pid, r["hostnum"])
            guild_name = ''
            club_info = entry.get('club_info', {}) if isinstance(entry, dict) else {}
            club_id = club_info.get('club_id', 0)
            if club_id:
                guild_name = await self._resolve_guild_name(club_id, r["hostnum"])

            final_players.append({
                "pid": pid,
                "nickname": nickname,
                "number_id": number_id,
                "main_good": main_good,
                "price_history": price_history,
                "total_profit": total_profit,
                "is_on_watchlist": is_on_watchlist,
                "good_has_name": good_has_name,
                "good_name": good_name or "",
                "likes": likes,
                "guild_name": guild_name,
                "mode": base.get('mode', 0),
            })

        if not final_players:
            await interaction.followup.send("⚠️ No players with market data found in the resolved list.", ephemeral=True)
            return

        # Confirmation view
        confirm_view = _BulkPlayerConfirmView(cog=self, players=final_players, authorized_user_id=interaction.user.id)
        container = confirm_view.build_container()
        confirm_layout = LayoutView(timeout=180)
        confirm_layout.add_item(container)
        confirm_layout.add_item(confirm_view.action_row)
        await interaction.followup.send(view=confirm_layout, ephemeral=True)


# ---------------------------------------------------------------------------
# View registration
# ---------------------------------------------------------------------------
from cogs.view_registry import register
register(MarketReportView, cog=None, grouped_data={}, total_players=0, report_ts=0, next_update_ts=0, day_number=1, countdown_str="", known_goods=set())
register(NewWeekMarketView, cog=None, good_ids=[], good_names_map={}, known_goods=set(), report_ts=0, day_number=1, countdown_str="")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(MarketCog(bot))