import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow
import aiosqlite
import datetime
import os
import random
from typing import Optional, List, Tuple
from datetime import timezone, timedelta

from settings import logger, BASE_DIR

# Paths
DB_PATH = "data/giveaways.db"
VERIFICATION_DB_PATH = BASE_DIR / "data" / "guild_verification.db"

# ─── MODULE-LEVEL SHARED HELPERS ─────────────────────────────────────────────

async def _is_user_bound(user_id: int) -> bool:
    """Check if a Discord user is in the verified_members table."""
    if not os.path.exists(VERIFICATION_DB_PATH):
        return False
    try:
        async with aiosqlite.connect(VERIFICATION_DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM verified_members WHERE user_id = ?", (user_id,)
            ) as cursor:
                return await cursor.fetchone() is not None
    except Exception:
        return False


async def _get_participant_count(giveaway_id: int) -> int:
    """Helper: count entries for a giveaway."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM entries WHERE giveaway_id = ?", (giveaway_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def _get_participant_pids(giveaway_id: int) -> List[int]:
    """Get all participant user IDs for a giveaway."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM entries WHERE giveaway_id = ?", (giveaway_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def _get_game_id_by_user(user_id: int) -> Optional[str]:
    """Get the in-game character UID (game ID) for a Discord user from the verification database."""
    if not os.path.exists(VERIFICATION_DB_PATH):
        return None
    try:
        async with aiosqlite.connect(VERIFICATION_DB_PATH) as db:
            async with db.execute(
                "SELECT character_uid FROM verified_members WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _parse_db_datetime(db_str: str) -> datetime.datetime:
    """Parse a datetime string and return an aware UTC datetime."""
    try:
        dt = datetime.datetime.fromisoformat(db_str)
    except (ValueError, TypeError):
        try:
            dt = datetime.datetime.strptime(db_str, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return datetime.datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _serialize_db_datetime(dt: datetime.datetime) -> str:
    """Serialize an aware datetime to a UTC ISO string for storage."""
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.isoformat()


def _unix_timestamp(dt: datetime.datetime) -> int:
    """Safely get a Unix timestamp from a datetime."""
    if dt.tzinfo is None:
        epoch = datetime.datetime(1970, 1, 1, tzinfo=timezone.utc)
        return int((dt.replace(tzinfo=timezone.utc) - epoch).total_seconds())
    return int(dt.timestamp())


def _weighted_sample_without_replacement(
    population: list, weights: list, k: int
) -> list:
    """
    Sample k items from population using weighted probabilities WITHOUT replacement.
    Uses cumulative weight algorithm: pick an item, remove it, re-normalize, repeat.
    """
    if k >= len(population):
        return list(population)

    remaining_pop = list(population)
    remaining_weights = list(weights)
    result = []

    for _ in range(k):
        total_weight = sum(remaining_weights)
        if total_weight <= 0:
            pick = random.choice(remaining_pop)
        else:
            r = random.uniform(0, total_weight)
            cumulative = 0.0
            for i, w in enumerate(remaining_weights):
                cumulative += w
                if r <= cumulative:
                    pick = remaining_pop[i]
                    break
            else:
                pick = remaining_pop[-1]

        idx = remaining_pop.index(pick)
        remaining_pop.pop(idx)
        remaining_weights.pop(idx)
        result.append(pick)

    return result


async def _select_winners(
    participants: list,
    winners_count: int,
    multiplier: Optional[float],
    chance_role_id: Optional[int],
    chance_bound_only: bool,
    channel: discord.TextChannel,
    require_bound: bool = False,
    deduplicate: bool = False,
) -> Tuple[list, str, list]:
    """
    Select winners using weighted random selection WITHOUT replacement.
    Returns (winners_list, winner_mentions_text, winner_ids).

    If require_bound is True, participants who have since unbounded
    their account are filtered out before selecting winners.
    
    If deduplicate is True, multiple Discord users bound to the same game account
    are merged so each unique game account only counts once. The first Discord user
    encountered for each game account is used as the representative.
    """
    total = len(participants)
    if total == 0:
        return [], "No one entered this time. 😢", []

    # Filter out unbound participants if the giveaway is bound-only
    if require_bound:
        filtered_participants = []
        for uid in participants:
            if await _is_user_bound(uid):
                filtered_participants.append(uid)
        participants = filtered_participants
        total = len(participants)
        if total == 0:
            return [], "No eligible participants (no bound accounts). 😢", []

    # Deduplicate by game account if requested
    if deduplicate:
        seen_game_ids = {}
        unique_participants = []
        for uid in participants:
            game_id = await _get_game_id_by_user(uid)
            if game_id:
                if game_id not in seen_game_ids:
                    seen_game_ids[game_id] = uid
                    unique_participants.append(uid)
            else:
                # Unbound users are kept as-is (they have no game ID to deduplicate)
                unique_participants.append(uid)
        participants = unique_participants
        total = len(participants)
        if total == 0:
            return [], "No eligible participants after deduplication. 😢", []

    has_multiplier = multiplier is not None and multiplier > 1.0
    weights = []
    for uid in participants:
        weight = 1.0
        if has_multiplier and chance_role_id:
            member = channel.guild.get_member(uid)
            if member and any(r.id == chance_role_id for r in member.roles):
                if chance_bound_only:
                    if await _is_user_bound(uid):
                        weight = multiplier
                else:
                    weight = multiplier
        weights.append(weight)

    k = min(total, winners_count)
    winners_list = _weighted_sample_without_replacement(participants, weights, k)

    # Build mentions with Game Character UID for each winner
    enriched = []
    for uid in winners_list:
        game_id = await _get_game_id_by_user(uid)
        if game_id:
            enriched.append(f"<@{uid}> (Game ID: {game_id})")
        else:
            enriched.append(f"<@{uid}>")
    mentions = ", ".join(enriched)
    return winners_list, mentions, winners_list


# ─── DATABASE INIT ──────────────────────────────────────────────────────────

async def init_db():
    """Initialize the giveaways database with the full schema."""
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '🎁 NEW GIVEAWAY! 🎁',
                channel_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                winners INTEGER NOT NULL DEFAULT 1,
                sponsor_id INTEGER NOT NULL,
                end_time DATETIME NOT NULL,
                allowed_roles TEXT,
                require_bound INTEGER DEFAULT 0,
                chance_multiplier REAL DEFAULT 1.0,
                chance_role_id INTEGER,
                chance_for_bound_only INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                message_id INTEGER,
                guild_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                giveaway_id INTEGER,
                user_id INTEGER,
                entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (giveaway_id, user_id)
            )
        """)
        for col, col_type in [
            ("title", "TEXT NOT NULL DEFAULT '🎁 NEW GIVEAWAY! 🎁'"),
            ("require_bound", "INTEGER DEFAULT 0"),
            ("chance_multiplier", "REAL DEFAULT 1.0"),
            ("chance_role_id", "INTEGER"),
            ("chance_for_bound_only", "INTEGER DEFAULT 0"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("message_id", "INTEGER"),
            ("guild_id", "INTEGER"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("ended_at", "TIMESTAMP"),
        ]:
            try:
                await db.execute(f"ALTER TABLE giveaways ADD COLUMN {col} {col_type}")
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE entries ADD COLUMN entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        except Exception:
            pass
        await db.commit()


async def _cleanup_old_giveaways(days: int = 365):
    """Delete giveaways older than `days` that are ended or cancelled."""
    cutoff = datetime.datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = _serialize_db_datetime(cutoff)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM entries WHERE giveaway_id IN (
                SELECT id FROM giveaways WHERE ended_at IS NOT NULL AND ended_at <= ?
            )
        """, (cutoff_str,))
        await db.execute("""
            DELETE FROM giveaways WHERE status IN ('ended', 'cancelled')
            AND ended_at IS NOT NULL AND ended_at <= ?
        """, (cutoff_str,))
        await db.commit()
        logger.info(f"Cleaned up giveaways older than {days} days")


# ─── ACTIVE GIVEAWAY VIEW (Components V2 Layout) ───────────────────────────

class _ActiveGiveawayContent:
    """Helper to build the content text displays for an active giveaway."""

    @staticmethod
    def build_text_displays(gw: dict, participant_count: int, guild: Optional[discord.Guild]) -> list:
        """Return a list of TextDisplay/Separator components for the active giveaway layout."""
        inner = []

        inner.append(TextDisplay(f"# {gw['title']}"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay(f"🎁 **Prize:** {gw['prize']}"))
        inner.append(TextDisplay(
            f"👑 **Winners:** {gw['winners']}  ·  👤 **Sponsor:** <@{gw['sponsor_id']}>"
        ))

        dt_end = _parse_db_datetime(gw['end_time'])
        ts = _unix_timestamp(dt_end)
        inner.append(TextDisplay(f"⏰ **Ends:** <t:{ts}:F> (<t:{ts}:R>)"))
        inner.append(TextDisplay(f"👥 **Participants:** {participant_count}"))

        if gw.get("allowed_roles"):
            roles_text = ", ".join(
                f"<@&{r.strip()}>" for r in gw["allowed_roles"].split(",") if r.strip()
            )
            inner.append(TextDisplay(f"🔒 **Allowed Roles:** {roles_text}"))

        if gw.get("require_bound"):
            inner.append(TextDisplay("🔗 **Bound-Only:** Only verified/bound accounts may enter."))

        if gw.get("chance_multiplier") and gw["chance_multiplier"] > 1.0 and gw.get("chance_role_id"):
            role = guild.get_role(gw["chance_role_id"]) if guild else None
            role_name = role.mention if role else f"Role ID {gw['chance_role_id']}"
            bound_tag = " (requires bound account)" if gw.get("chance_for_bound_only") else ""
            inner.append(TextDisplay(
                f"⚡ **Boosted Odds:** {gw['chance_multiplier']}x for {role_name}{bound_tag}"
            ))

        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay("*Click the green button to enter!*"))

        return inner


class _GiveawayActiveButtons:
    """Builds the action row for an active giveaway view."""

    @staticmethod
    def build_action_row(view_instance) -> ActionRow:
        row = ActionRow()

        enter_btn = discord.ui.Button(
            label="Enter Giveaway", style=discord.ButtonStyle.green, custom_id="gw_enter"
        )
        enter_btn.callback = view_instance._on_enter
        row.add_item(enter_btn)

        leave_btn = discord.ui.Button(
            label="Leave Giveaway", style=discord.ButtonStyle.red, custom_id="gw_leave"
        )
        leave_btn.callback = view_instance._on_leave
        row.add_item(leave_btn)

        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.gray, custom_id="gw_cancel"
        )
        cancel_btn.callback = view_instance._on_cancel
        row.add_item(cancel_btn)

        return row


class PersistentGiveawayView(LayoutView):
    """
    Components V2 layout for an active giveaway.
    When initialized WITHOUT arguments (for persistent view registration),
    only buttons are added. When initialized WITH giveaway data,
    the full content layout is built.
    """

    def __init__(self, gw_data: dict = None, participant_count: int = 0, guild: discord.Guild = None):
        super().__init__(timeout=None)
        if gw_data is not None:
            self._build_full(gw_data, participant_count, guild)
        else:
            self._build_buttons_only()

    def _build_buttons_only(self):
        self.add_item(_GiveawayActiveButtons.build_action_row(self))

    def _build_full(self, gw: dict, participant_count: int, guild: discord.Guild):
        inner = _ActiveGiveawayContent.build_text_displays(gw, participant_count, guild)
        inner.append(_GiveawayActiveButtons.build_action_row(self))
        self.add_item(Container(*inner, accent_color=discord.Color.gold().value))

    async def _get_giveaway(self, message_id: int) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, title, prize, winners, sponsor_id, end_time, allowed_roles, "
                "require_bound, chance_multiplier, chance_role_id, chance_for_bound_only, "
                "status, channel_id "
                "FROM giveaways WHERE message_id = ? AND status = 'active'",
                (message_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0], "title": row[1], "prize": row[2],
                    "winners": row[3], "sponsor_id": row[4], "end_time": row[5],
                    "allowed_roles": row[6], "require_bound": row[7],
                    "chance_multiplier": row[8], "chance_role_id": row[9],
                    "chance_for_bound_only": row[10], "status": row[11],
                    "channel_id": row[12],
                }

    async def _has_entered(self, giveaway_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, user_id),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def _rebuild_and_edit(self, interaction: discord.Interaction, gw: dict):
        new_count = await _get_participant_count(gw["id"])
        new_view = PersistentGiveawayView(gw_data=gw, participant_count=new_count, guild=interaction.guild)
        try:
            await interaction.message.edit(
                content=None, embeds=[], attachments=[], view=new_view
            )
        except Exception:
            pass

    async def _on_enter(self, interaction: discord.Interaction):
        gw = await self._get_giveaway(interaction.message.id)
        if not gw:
            return await interaction.response.send_message(
                "This giveaway is no longer active.", ephemeral=True
            )

        if gw["require_bound"] and not await _is_user_bound(interaction.user.id):
            return await interaction.response.send_message(
                "🔒 You must bind your game account first to enter this giveaway!\n"
                "Use the verification system in the server to bind your account.",
                ephemeral=True,
            )

        if gw["allowed_roles"]:
            allowed_ids = [
                int(r.strip()) for r in gw["allowed_roles"].split(",") if r.strip()
            ]
            if not any(role.id in allowed_ids for role in interaction.user.roles):
                return await interaction.response.send_message(
                    "You don't have the required role to enter.", ephemeral=True
                )

        if await self._has_entered(gw["id"], interaction.user.id):
            return await interaction.response.send_message(
                "You are already in this giveaway!", ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO entries (giveaway_id, user_id) VALUES (?, ?)",
                (gw["id"], interaction.user.id),
            )
            await db.commit()

        await self._rebuild_and_edit(interaction, gw)
        await interaction.response.send_message("Successfully entered! 🍀", ephemeral=True)

    async def _on_leave(self, interaction: discord.Interaction):
        gw = await self._get_giveaway(interaction.message.id)
        if not gw:
            return await interaction.response.send_message(
                "This giveaway is no longer active.", ephemeral=True
            )

        if not await self._has_entered(gw["id"], interaction.user.id):
            return await interaction.response.send_message(
                "You aren't in this giveaway.", ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM entries WHERE giveaway_id = ? AND user_id = ?",
                (gw["id"], interaction.user.id),
            )
            await db.commit()

        await self._rebuild_and_edit(interaction, gw)
        await interaction.response.send_message("You have left the giveaway.", ephemeral=True)

    async def _on_cancel(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Only admins can cancel.", ephemeral=True
            )

        gw = await self._get_giveaway(interaction.message.id)
        if not gw:
            return await interaction.response.send_message(
                "This giveaway is already ended or cancelled.", ephemeral=True
            )

        now = datetime.datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE giveaways SET status = 'cancelled', ended_at = ? WHERE id = ?",
                (_serialize_db_datetime(now), gw["id"]),
            )
            await db.commit()

        cancelled_view = CancelledGiveawayView()
        await interaction.message.edit(view=cancelled_view)
        await interaction.response.send_message("Giveaway cancelled.", ephemeral=True)


# ─── CANCELLED GIVEAWAY VIEW ────────────────────────────────────────────────

class CancelledGiveawayView(LayoutView):
    """Simple V2 layout shown when a giveaway is cancelled."""

    def __init__(self):
        super().__init__(timeout=None)
        inner = [
            TextDisplay("# ❌ GIVEAWAY CANCELLED"),
            TextDisplay("This giveaway has been cancelled by an administrator."),
        ]
        self.add_item(Container(*inner, accent_color=discord.Color.dark_gray().value))


# ─── ENDED GIVEAWAY VIEW (reroll button) ────────────────────────────────────

class EndedGiveawayView(LayoutView):
    """
    Components V2 layout for an ended giveaway showing results + Reroll button.
    When initialized WITHOUT arguments (for persistent registration),
    only buttons are added.
    """

    def __init__(
        self,
        gw_data: dict = None,
        total_participants: int = 0,
        winner_mentions_text: str = "",
        guild: discord.Guild = None,
    ):
        super().__init__(timeout=None)
        if gw_data is not None:
            self._build_full(gw_data, total_participants, winner_mentions_text, guild)
        else:
            self._build_buttons_only()

    def _build_buttons_only(self):
        row = ActionRow()
        reroll_btn = discord.ui.Button(
            label="Reroll 🔄", style=discord.ButtonStyle.primary, custom_id="gw_reroll"
        )
        reroll_btn.callback = self._on_reroll
        row.add_item(reroll_btn)
        self.add_item(row)

    def _build_full(self, gw: dict, total_participants: int, winner_text: str, guild: discord.Guild):
        inner = []
        inner.append(TextDisplay(f"# 🎉 GIVEAWAY ENDED — {gw['title']}"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay(f"🎁 **Prize:** {gw['prize']}"))

        winner_display = winner_text if winner_text else "😢 No one entered this time."
        inner.append(TextDisplay(f"👑 **Winners:** {winner_display}"))

        inner.append(TextDisplay(f"👥 **Total Participants:** {total_participants}"))
        inner.append(TextDisplay(f"👤 **Sponsored by:** <@{gw['sponsor_id']}>"))

        if gw.get("multiplier") and gw["multiplier"] > 1.0 and gw.get("chance_role_id"):
            role = guild.get_role(gw["chance_role_id"]) if guild else None
            role_name = role.mention if role else f"Role ID {gw['chance_role_id']}"
            bound_tag = " (bound accounts only)" if gw.get("chance_for_bound_only") else ""
            inner.append(TextDisplay(f"⚡ **Boosted Odds:** {gw['multiplier']}x for {role_name}{bound_tag}"))

        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay("*Admins can click Reroll to pick new winners.*"))

        row = ActionRow()
        reroll_btn = discord.ui.Button(
            label="Reroll 🔄", style=discord.ButtonStyle.primary, custom_id="gw_reroll"
        )
        reroll_btn.callback = self._on_reroll
        row.add_item(reroll_btn)
        inner.append(row)

        self.add_item(Container(*inner, accent_color=discord.Color.dark_gray().value))

    async def _get_ended_giveaway(self, message_id: int) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, title, prize, winners, sponsor_id, "
                "chance_multiplier, chance_role_id, chance_for_bound_only, channel_id, "
                "require_bound "
                "FROM giveaways WHERE message_id = ? AND status = 'ended'",
                (message_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0], "title": row[1], "prize": row[2],
                    "winners": row[3], "sponsor_id": row[4],
                    "multiplier": row[5], "chance_role_id": row[6],
                    "chance_for_bound_only": row[7], "channel_id": row[8],
                    "require_bound": bool(row[9]),
                }

    async def _on_reroll(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Only admins can reroll.", ephemeral=True)

        gw = await self._get_ended_giveaway(interaction.message.id)
        if not gw:
            return await interaction.response.send_message(
                "This giveaway hasn't ended yet or doesn't exist.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        participants = await _get_participant_pids(gw["id"])
        if not participants:
            return await interaction.followup.send("No participants to reroll from.", ephemeral=True)

        winners_list, mentions_text, _ = await _select_winners(
            participants, gw["winners"], gw["multiplier"],
            gw["chance_role_id"], gw["chance_for_bound_only"], interaction.channel,
            require_bound=gw.get("require_bound", False), deduplicate=True,
        )

        if not winners_list:
            return await interaction.followup.send("No winners could be selected.", ephemeral=True)

        new_view = EndedGiveawayView(
            gw_data=gw, total_participants=len(participants),
            winner_mentions_text=mentions_text, guild=interaction.guild,
        )
        try:
            await interaction.message.edit(view=new_view)
        except Exception:
            pass

        announcement = self._build_reroll_announcement(
            gw["title"], gw["prize"], mentions_text, winners_list
        )
        await interaction.message.reply(view=announcement)
        await interaction.followup.send("✅ Reroll successful!", ephemeral=True)

    @staticmethod
    def _build_reroll_announcement(title: str, prize: str, winner_text: str, winner_ids: list) -> LayoutView:
        inner = [
            TextDisplay("# 🔄 REROLL COMPLETED"),
            TextDisplay(f"**New winners for {title}**"),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(f"🎁 **Prize:** {prize}"),
            TextDisplay(f"👑 **Winners:** {winner_text}"),
        ]
        view = LayoutView(timeout=None)
        view.add_item(Container(*inner, accent_color=discord.Color.blue().value))
        return view


# ─── GIVEAWAY END ANNOUNCEMENT ──────────────────────────────────────────────

class GiveawayEndAnnouncementView(LayoutView):
    """Clean Components V2 card posted when a giveaway ends."""

    def __init__(
        self,
        title: str,
        prize: str,
        winner_mentions_text: str,
        total_participants: int,
        sponsor_mention: str,
        multiplier: Optional[float] = None,
        chance_role_id: Optional[int] = None,
        chance_bound_only: bool = False,
        guild: discord.Guild = None,
    ):
        super().__init__(timeout=None)
        inner = []
        inner.append(TextDisplay("# 🎉 GIVEAWAY ENDED!"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay(f"**{title}**"))
        inner.append(TextDisplay(f"🎁 **Prize:** {prize}"))

        if winner_mentions_text:
            inner.append(TextDisplay(f"👑 **Winners:** {winner_mentions_text}"))
        else:
            inner.append(TextDisplay("😢 **Winners:** No one entered this time."))

        inner.append(TextDisplay(f"👥 **Participants:** {total_participants}"))
        inner.append(TextDisplay(f"👤 **Sponsored by:** {sponsor_mention}"))

        if multiplier and multiplier > 1.0 and chance_role_id and guild:
            role = guild.get_role(chance_role_id)
            role_name = role.mention if role else f"Role ID {chance_role_id}"
            bound_tag = " (bound accounts only)" if chance_bound_only else ""
            inner.append(TextDisplay(f"⚡ **Boosted Odds:** {multiplier}x for {role_name}{bound_tag}"))

        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay("*Thank you for participating!*"))
        self.add_item(Container(*inner, accent_color=discord.Color.green().value))


# ─── PREVIEW VIEW (Components V2) ──────────────────────────────────────────

class GiveawayPreviewView(LayoutView):
    """Components V2 preview of a giveaway before posting."""

    def __init__(self, author_id: int, gw_data: dict, guild: discord.Guild):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.confirmed = False

        inner = _ActiveGiveawayContent.build_text_displays(gw_data, 0, guild)
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay("🔍 **PREVIEW MODE** — Preview only."))

        # Preview action row
        row = ActionRow()
        enter_btn = discord.ui.Button(label="Enter Giveaway", style=discord.ButtonStyle.green)
        enter_btn.callback = self._on_preview_enter
        row.add_item(enter_btn)

        leave_btn = discord.ui.Button(label="Leave Giveaway", style=discord.ButtonStyle.red)
        leave_btn.callback = self._on_preview_leave
        row.add_item(leave_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.gray)
        cancel_btn.callback = self._on_preview_cancel
        row.add_item(cancel_btn)
        inner.append(row)

        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Confirm / Cancel row
        confirm_row = ActionRow()
        confirm_btn = discord.ui.Button(label="✅ Confirm Post", style=discord.ButtonStyle.success)
        confirm_btn.callback = self._on_confirm
        confirm_row.add_item(confirm_btn)

        cancel_post_btn = discord.ui.Button(label="❌ Cancel", style=discord.ButtonStyle.danger)
        cancel_post_btn.callback = self._on_cancel_post
        confirm_row.add_item(cancel_post_btn)
        inner.append(confirm_row)

        self.add_item(Container(*inner, accent_color=discord.Color.gold().value))

    async def _on_preview_enter(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "🔍 **Preview mode** — This is just a preview! No entry recorded.", ephemeral=True,
        )

    async def _on_preview_leave(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "🔍 **Preview mode** — This is just a preview! Nothing to leave.", ephemeral=True,
        )

    async def _on_preview_cancel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "🔍 **Preview mode** — This is just a preview!", ephemeral=True,
        )

    async def _on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "Only the person who created this preview can confirm.", ephemeral=True
            )
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    async def _on_cancel_post(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "Only the creator can cancel this preview.", ephemeral=True
            )
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


# ─── GIVEAWAY HISTORY VIEW ──────────────────────────────────────────────────

class GiveawayHistoryView(discord.ui.View):
    """Paginated history with View Participants button for each giveaway."""

    def __init__(self, giveaways: list, author_id: int, current_page: int = 1):
        super().__init__(timeout=120)
        self.giveaways = giveaways
        self.author_id = author_id
        self.current_page = current_page
        self.items_per_page = 5
        self.total_pages = max(1, (len(giveaways) + self.items_per_page - 1) // self.items_per_page)
        self.update_buttons()

    def update_buttons(self):
        self.prev_page.disabled = self.current_page <= 1
        self.next_page.disabled = self.current_page >= self.total_pages

    def generate_embed(self):
        start = (self.current_page - 1) * self.items_per_page
        end = start + self.items_per_page
        page_gws = self.giveaways[start:end]

        embed = discord.Embed(
            title="📊 Giveaway History",
            description=f"**Total giveaways:** {len(self.giveaways)} | Page {self.current_page}/{self.total_pages}",
            color=discord.Color.blurple(),
        )

        for gw in page_gws:
            gw_id, title, prize, winners, end_time, status, created_at, ended_at, total_entries = gw
            status_emoji = {
                "active": "🟢",
                "ended": "✅",
                "cancelled": "❌",
            }.get(status, "❓")

            ended_str = ""
            if ended_at:
                try:
                    ended_dt = _parse_db_datetime(ended_at)
                    ended_str = f" | Ended: <t:{_unix_timestamp(ended_dt)}:R>"
                except Exception:
                    ended_str = f" | Ended: {ended_at}"

            try:
                created_dt = _parse_db_datetime(created_at)
                created_str = f"<t:{_unix_timestamp(created_dt)}:D>"
            except Exception:
                created_str = created_at

            embed.add_field(
                name=f"{status_emoji} {title} — {prize}",
                value=(
                    f"**ID:** {gw_id} | **Winners:** {winners} | **Entries:** {total_entries}\n"
                    f"**Created:** {created_str}{ended_str}"
                ),
                inline=False,
            )

        embed.set_footer(text="Use arrow buttons to navigate · Click 👁️ to view participants")
        return embed

    @discord.ui.button(label="← Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    @discord.ui.button(label="👁️ View Participants", style=discord.ButtonStyle.primary)
    async def view_participants(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open a modal to input a giveaway ID and view its participants."""
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        await interaction.response.send_modal(GiveawayParticipantsModal(author_id=self.author_id))

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass


# ─── GIVEAWAY PARTICIPANTS VIEW ─────────────────────────────────────────────

class GiveawayParticipantsView(discord.ui.View):
    """Paginated view of giveaway participants with duplicate detection and removal."""

    def __init__(self, author_id: int, gw_id: int, gw_title: str, participants: list, require_bound: bool = False):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.gw_id = gw_id
        self.gw_title = gw_title
        self.all_participants = participants  # list of (user_id, username, character_uid or None)
        self.require_bound = require_bound
        self.current_page = 1
        self.items_per_page = 10
        self.total_pages = max(1, (len(participants) + self.items_per_page - 1) // self.items_per_page)
        self.update_buttons()

    def update_buttons(self):
        self.prev_page.disabled = self.current_page <= 1
        self.next_page.disabled = self.current_page >= self.total_pages

    def _detect_duplicates(self):
        """Detect duplicate game accounts bound to multiple Discord users."""
        uid_to_users = {}
        for user_id, username, character_uid in self.all_participants:
            if character_uid:
                if character_uid not in uid_to_users:
                    uid_to_users[character_uid] = []
                uid_to_users[character_uid].append((user_id, username))
        
        duplicates = {uid: users for uid, users in uid_to_users.items() if len(users) > 1}
        return duplicates

    def generate_embed(self):
        start = (self.current_page - 1) * self.items_per_page
        end = start + self.items_per_page
        page_participants = self.all_participants[start:end]

        embed = discord.Embed(
            title=f"📋 Participants: {self.gw_title} (ID: {self.gw_id})",
            description=f"**Total:** {len(self.all_participants)} | Page {self.current_page}/{self.total_pages}",
            color=discord.Color.blurple(),
        )

        # Duplicate detection section
        duplicates = self._detect_duplicates()
        if duplicates:
            dup_text = []
            for uid, users in sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
                dup_text.append(f"**UID `{uid}`** — {len(users)} Discord user(s):\n")
                for user_id, username in users:
                    dup_text.append(f"  • <@{user_id}> (`{username}`)\n")
            
            embed.add_field(
                name="⚠️ Duplicate Game Accounts Detected",
                value="".join(dup_text)[:1024],
                inline=False
            )

        # Participants list
        if page_participants:
            participant_lines = []
            for idx, (user_id, username, character_uid) in enumerate(page_participants, start=start + 1):
                bound_status = "✅" if character_uid else "❌"
                uid_display = f"`{character_uid}`" if character_uid else "Not bound"
                participant_lines.append(
                    f"**#{idx}** <@{user_id}> (`{username}`)\n"
                    f"   └ {bound_status} Game UID: {uid_display}"
                )
            
            embed.add_field(
                name="Participants",
                value="\n".join(participant_lines)[:1024],
                inline=False
            )
        else:
            embed.add_field(name="Participants", value="No participants on this page.", inline=False)

        embed.set_footer(text=f"Page {self.current_page}/{self.total_pages} • Click 🗑️ to remove an entry")
        return embed

    @discord.ui.button(label="← Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    @discord.ui.button(label="🗑️ Remove Entry", style=discord.ButtonStyle.danger)
    async def remove_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open a modal to select and remove a participant."""
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        
        # Build options for the current page
        start = (self.current_page - 1) * self.items_per_page
        end = start + self.items_per_page
        page_participants = self.all_participants[start:end]
        
        if not page_participants:
            return await interaction.response.send_message("No participants to remove on this page.", ephemeral=True)
        
        # Send modal to input user ID to remove
        await interaction.response.send_modal(RemoveEntryModal(self))

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your menu.", ephemeral=True)
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def remove_participant(self, user_id: int):
        """Remove a participant from the giveaway."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM entries WHERE giveaway_id = ? AND user_id = ?",
                (self.gw_id, user_id)
            )
            await db.commit()
        
        # Update local list
        self.all_participants = [(uid, uname, cuid) for uid, uname, cuid in self.all_participants if uid != user_id]
        
        # Recalculate total pages
        self.total_pages = max(1, (len(self.all_participants) + self.items_per_page - 1) // self.items_per_page)
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages
        self.update_buttons()


class RemoveEntryModal(discord.ui.Modal, title="Remove Participant"):
    """Modal to remove a participant by user ID."""

    user_id_input = discord.ui.TextInput(
        label="User ID to remove",
        placeholder="Enter the Discord user ID to remove",
        style=discord.TextStyle.short,
        required=True,
    )

    def __init__(self, participants_view: GiveawayParticipantsView):
        super().__init__()
        self.participants_view = participants_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            target_user_id = int(self.user_id_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID format.", ephemeral=True)
            return

        # Check if user is in participants
        user_ids = [uid for uid, _, _ in self.participants_view.all_participants]
        if target_user_id not in user_ids:
            await interaction.followup.send(f"❌ User ID `{target_user_id}` is not in this giveaway.", ephemeral=True)
            return

        # Remove the participant
        await self.participants_view.remove_participant(target_user_id)
        
        # Get username for confirmation
        username = next((uname for uid, uname, _ in self.participants_view.all_participants if uid == target_user_id), "Unknown")
        
        await interaction.followup.send(
            f"✅ Removed **{username}** (`{target_user_id}`) from the giveaway.",
            ephemeral=True
        )
        
        # Edit the original message with updated view
        try:
            await interaction.edit_original_response(
                embed=self.participants_view.generate_embed(),
                view=self.participants_view
            )
        except Exception:
            pass


# ─── GIVEAWAY PARTICIPANTS MODAL (Legacy) ───────────────────────────────────

class GiveawayParticipantsModal(discord.ui.Modal, title="Enter Giveaway ID"):
    """Modal to input a giveaway ID for viewing participants."""
    giveaway_id_input = discord.ui.TextInput(
        label="Giveaway ID",
        placeholder="Enter the giveaway ID number",
        style=discord.TextStyle.short,
        required=True,
    )

    def __init__(self, author_id: int):
        super().__init__()
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            gw_id = int(self.giveaway_id_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid giveaway ID.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT title, prize, status, guild_id, require_bound FROM giveaways WHERE id = ?",
                (gw_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.followup.send(f"❌ Giveaway with ID `{gw_id}` not found.", ephemeral=True)
            return

        title, prize, status, guild_id, require_bound = row
        if guild_id and guild_id != interaction.guild_id:
            await interaction.followup.send("❌ That giveaway belongs to a different server.", ephemeral=True)
            return

        # Get participants with bound info
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT user_id FROM entries WHERE giveaway_id = ?",
                (gw_id,),
            ) as cursor:
                participant_rows = await cursor.fetchall()

        participants = []
        for (user_id,) in participant_rows:
            character_uid = await _get_game_id_by_user(user_id)
            username = str(interaction.guild.get_member(user_id) or f"User {user_id}")
            participants.append((user_id, username, character_uid))

        # If require_bound is enabled, filter to only bound participants and deduplicate
        if require_bound:
            bound_participants = []
            seen_game_ids = {}
            for user_id, username, character_uid in participants:
                if character_uid:
                    if character_uid not in seen_game_ids:
                        seen_game_ids[character_uid] = (user_id, username)
                        bound_participants.append((user_id, username, character_uid))
                else:
                    # Unbound users are excluded when require_bound is True
                    pass
            participants = bound_participants

        if not participants:
            await interaction.followup.send(
                f"📋 Giveaway **{title}** ({prize}) — Status: **{status}**\nNo participants yet.",
                ephemeral=True,
            )
            return

        # Create participants view
        view = GiveawayParticipantsView(
            author_id=interaction.user.id,
            gw_id=gw_id,
            gw_title=title,
            participants=participants,
            require_bound=bool(require_bound)
        )
        embed = view.generate_embed()
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = msg


# ─── COG ─────────────────────────────────────────────────────────────────────

class GiveawayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        self.bot.loop.create_task(self._initialize_and_start())

    async def _initialize_and_start(self):
        await init_db()
        self.check_giveaways_loop.start()
        self.cleanup_loop.start()

    # ── Background loop: end expired giveaways ──────────────────────────────

    @tasks.loop(minutes=1)
    async def check_giveaways_loop(self):
        if not self.bot.is_ready():
            return
        now_utc = datetime.datetime.now(timezone.utc)

        expired_ids = []
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, end_time FROM giveaways WHERE status = 'active'"
            ) as cursor:
                all_active = await cursor.fetchall()

            for gw_id, end_time_str in all_active:
                try:
                    dt_end = _parse_db_datetime(end_time_str)
                    if dt_end <= now_utc:
                        expired_ids.append(gw_id)
                except Exception:
                    logger.warning(f"Could not parse end_time for giveaway #{gw_id}: {end_time_str}")

            if not expired_ids:
                return

            for gw_id in expired_ids:
                async with db.execute(
                    "SELECT id, channel_id, message_id, title, prize, winners, sponsor_id, "
                    "chance_multiplier, chance_role_id, chance_for_bound_only, require_bound "
                    "FROM giveaways WHERE id = ?",
                    (gw_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        continue

                (
                    gw_id, chan_id, msg_id, title, prize, winners_count, sponsor_id,
                    multiplier, chance_role_id, chance_bound_only, require_bound,
                ) = row
                channel = self.bot.get_channel(chan_id)
                if not channel:
                    continue

                participants = await _get_participant_pids(gw_id)
                total_participants = len(participants)

                sponsor_user = self.bot.get_user(sponsor_id)
                sponsor_mention = sponsor_user.mention if sponsor_user else f"ID: {sponsor_id}"

                _, winner_mentions_text, winner_ids = await _select_winners(
                    participants, winners_count, multiplier, chance_role_id, chance_bound_only,
                    channel, require_bound=bool(require_bound), deduplicate=True,
                )

                ended_view = EndedGiveawayView(
                    gw_data={
                        "id": gw_id, "title": title, "prize": prize,
                        "winners": winners_count, "sponsor_id": sponsor_id,
                        "multiplier": multiplier, "chance_role_id": chance_role_id,
                        "chance_for_bound_only": chance_bound_only,
                    },
                    total_participants=total_participants,
                    winner_mentions_text=winner_mentions_text,
                    guild=channel.guild,
                )

                original_msg = None
                if msg_id:
                    try:
                        original_msg = await channel.fetch_message(msg_id)
                        await original_msg.edit(
                            content=None, embeds=[], attachments=[], view=ended_view
                        )
                    except Exception:
                        logger.warning(f"Could not edit original giveaway message for gw#{gw_id}")

                announcement_view = GiveawayEndAnnouncementView(
                    title=title, prize=prize,
                    winner_mentions_text=winner_mentions_text,
                    total_participants=total_participants,
                    sponsor_mention=sponsor_mention,
                    multiplier=multiplier, chance_role_id=chance_role_id,
                    chance_bound_only=chance_bound_only, guild=channel.guild,
                )

                if original_msg:
                    try:
                        await original_msg.reply(
                            view=announcement_view,
                            allowed_mentions=discord.AllowedMentions(
                                users=[discord.Object(id=uid) for uid in winner_ids]
                            ) if winner_ids else None,
                        )
                    except Exception:
                        try:
                            await channel.send(view=announcement_view)
                        except Exception:
                            pass
                else:
                    try:
                        await channel.send(view=announcement_view)
                    except Exception:
                        pass

                await db.execute(
                    "UPDATE giveaways SET status = 'ended', ended_at = ? WHERE id = ?",
                    (_serialize_db_datetime(now_utc), gw_id),
                )

            await db.commit()

    @check_giveaways_loop.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()

    # ── Background loop: cleanup old giveaways (once per day) ───────────────

    @tasks.loop(hours=24)
    async def cleanup_loop(self):
        if not self.bot.is_ready():
            return
        await _cleanup_old_giveaways(days=365)

    @cleanup_loop.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ── Helper: normalize allowed_roles input ───────────────────────────────

    def _normalize_allowed_roles(self, raw: Optional[str]) -> Optional[str]:
        """Normalize allowed_roles: accept role IDs, mentions, or comma-separated."""
        if not raw or not raw.strip():
            return None
        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if part.startswith("<@&") and part.endswith(">"):
                part = part[3:-1]
            try:
                rid = int(part)
                ids.add(str(rid))
            except (ValueError, TypeError):
                logger.warning(f"Invalid role ID in allowed_roles: '{part}'")
        return ",".join(ids) if ids else None

    # ── Command: giveaway start ─────────────────────────────────────────────

    @app_commands.command(name="giveaway", description="[ADMIN] Start a new giveaway")
    @app_commands.has_permissions(manage_server=True)
    @app_commands.guild_only()
    @app_commands.describe(
        prize="The prize to be won",
        winners="How many winners?",
        sponsor="Select the sponsor (User)",
        end_time="Format: YYYY-MM-DD HH:MM in your local time (e.g., 2025-06-01 15:00)",
        channel="Where to post the giveaway",
        timezone_offset="Your UTC offset. Example: +8 for Singapore, -5 for New York, 0 for UTC (default). Integers only.",
        title="Custom embed title (default: 🎁 NEW GIVEAWAY! 🎁)",
        require_bound="Only allow bound/verified accounts to enter?",
        chance_role="Role that gets boosted win odds",
        chance_multiplier="Multiplier for the boosted role (e.g., 1.5 = 50% higher chance)",
        chance_for_bound_only="Only apply multiplier if user is also bound?",
        allowed_roles="Comma separated Role IDs or mentions (Optional)",
        preview="Preview before posting? (default: True)",
    )
    @app_commands.rename(
        timezone_offset="timezone-offset",
        require_bound="require-bound",
        chance_role="chance-role",
        chance_multiplier="chance-multiplier",
        chance_for_bound_only="chance-for-bound-only",
        allowed_roles="allowed-roles",
    )
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        prize: str,
        winners: int,
        sponsor: discord.User,
        end_time: str,
        channel: discord.TextChannel,
        timezone_offset: int = 0,
        title: str = "🎁 NEW GIVEWAY! 🎁",
        require_bound: bool = False,
        chance_role: Optional[discord.Role] = None,
        chance_multiplier: Optional[float] = None,
        chance_for_bound_only: bool = False,
        allowed_roles: Optional[str] = None,
        preview: bool = True,
    ):
        await interaction.response.defer(ephemeral=True)

        if not -12 <= timezone_offset <= 14:
            return await interaction.followup.send(
                "Timezone offset must be between -12 and +14 (e.g., +8 for Singapore).",
                ephemeral=True,
            )

        try:
            user_local = datetime.datetime.strptime(end_time, "%Y-%m-%d %H:%M")
            user_tz = timezone(timedelta(hours=timezone_offset))
            local_aware = user_local.replace(tzinfo=user_tz)
            dt_utc = local_aware.astimezone(timezone.utc)

            now_utc = datetime.datetime.now(timezone.utc)
            if dt_utc < now_utc:
                return await interaction.followup.send(
                    "End time must be in the future! "
                    "Remember to use YOUR local time and set timezone_offset correctly.\n"
                    f"Example: if it's 15:00 in Singapore (+8), use `end_time: 2025-06-01 15:00` and `timezone_offset: 8`.",
                    ephemeral=True,
                )
        except ValueError:
            return await interaction.followup.send(
                "Format Error! Use YYYY-MM-DD HH:MM in your local time.\n"
                "Example: `2025-06-01 15:00`",
                ephemeral=True,
            )

        if chance_role and chance_multiplier is not None and chance_multiplier <= 1.0:
            return await interaction.followup.send(
                "Multiplier must be greater than 1.0 to have any effect.", ephemeral=True
            )
        effective_multiplier = chance_multiplier if chance_multiplier is not None else 1.0
        effective_chance_role_id = chance_role.id if chance_role else None

        normalized_roles = self._normalize_allowed_roles(allowed_roles)

        # Build data dict
        gw_data = {
            "title": title, "prize": prize, "winners": winners,
            "sponsor_id": sponsor.id, "end_time": _serialize_db_datetime(dt_utc),
            "allowed_roles": normalized_roles, "require_bound": require_bound,
            "chance_multiplier": effective_multiplier,
            "chance_role_id": effective_chance_role_id,
            "chance_for_bound_only": chance_for_bound_only,
        }

        # Preview flow
        if preview:
            preview_view = GiveawayPreviewView(
                author_id=interaction.user.id, gw_data=gw_data, guild=interaction.guild,
            )
            preview_msg = await interaction.followup.send(view=preview_view, ephemeral=True)

            timed_out = await preview_view.wait()
            if timed_out or not getattr(preview_view, "confirmed", False):
                try:
                    await preview_msg.edit(content="❌ Preview cancelled.", view=None)
                except Exception:
                    pass
                return
            try:
                await preview_msg.delete()
            except Exception:
                pass

        # Insert into database
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """INSERT INTO giveaways
                (title, channel_id, prize, winners, sponsor_id, end_time, allowed_roles,
                 require_bound, chance_multiplier, chance_role_id, chance_for_bound_only,
                 guild_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    title, channel.id, prize, winners, sponsor.id,
                    _serialize_db_datetime(dt_utc),
                    normalized_roles, int(require_bound), effective_multiplier,
                    effective_chance_role_id, int(chance_for_bound_only),
                    interaction.guild.id,
                ),
            )
            gw_id = cursor.lastrowid
            await db.commit()

        # Post the giveaway as Components V2
        view = PersistentGiveawayView(
            gw_data=gw_data, participant_count=0, guild=interaction.guild,
        )
        message = await channel.send(view=view)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE giveaways SET message_id = ? WHERE id = ?", (message.id, gw_id))
            await db.commit()

        await interaction.followup.send(
            f"✅ Giveaway **{title}** started in {channel.mention}!", ephemeral=True
        )

    # ── Subcommand: giveaway history ────────────────────────────────────────

    @app_commands.command(name="giveaway-history", description="[ADMIN] Browse past and present giveaways")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def giveaway_history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT g.id, g.title, g.prize, g.winners, g.end_time, g.status,
                          g.created_at, g.ended_at,
                          (SELECT COUNT(*) FROM entries e WHERE e.giveaway_id = g.id) as total_entries
                   FROM giveaways g
                   WHERE g.guild_id = ?
                   ORDER BY g.created_at DESC
                   LIMIT 100""",
                (interaction.guild.id,),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            embed = discord.Embed(
                title="📊 Giveaway History",
                description="No giveaways have been created yet in this server.",
                color=discord.Color.yellow(),
            )
            await interaction.followup.send(embed=embed)
            return

        view = GiveawayHistoryView(giveaways=rows, author_id=interaction.user.id, current_page=1)
        embed = view.generate_embed()
        msg = await interaction.followup.send(embed=embed, view=view)
        view.message = msg

    # ── Cleanup ──

    async def cog_unload(self):
        self.check_giveaways_loop.cancel()
        self.cleanup_loop.cancel()


# ─── VIEW REGISTRATION ───────────────────────────────────────────────────────

from cogs.view_registry import register

register(PersistentGiveawayView)
register(EndedGiveawayView)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))