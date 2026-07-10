import discord
import aiosqlite
import asyncio
import json
import datetime
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow, Button, Select
from settings import logger, BASE_DIR, WWM_LIST_GAME_HISTORY_URL, WWM_UID, WWM_API_URL, WWM_TOKEN, HOMESTEAD_FORUM_CHANNEL_ID
from utility.wwm import _wwm_api_post, get_player_info

HOMESTEAD_DB_PATH = BASE_DIR / "data" / "homestead.db"
VERIFICATION_DB_PATH = BASE_DIR / "data" / "guild_verification.db"
FORUM_CHANNEL_ID = HOMESTEAD_FORUM_CHANNEL_ID  # Forum channel for homestead notifications

# Farm quality mapping for text_param 1005
FARM_QUALITY_MAP = {
    1: "Bountiful Farmland",
    2: "Rich Farmland",
    3: "Fertile Farmland",
    4: "Lush Farmland",
}

# Embed colors and titles per serial_id
SERIAL_ID_EMBED_META = {
    12001: (0x2ECC71, "🌾 Crops Matured"),
    12002: (0x3498DB, "💧 Water Needed"),
    12003: (0xE67E22, "👤 Harvest"),
    12005: (0x9B59B6, "🤖 Auto-Farm Started"),
    12006: (0xE74C3C, "⏹️ Auto-Farm Canceled"),
    12007: (0xF1C40F, "🌱 Auto-Farm Ended"),
    12009: (0x1ABC9C, "📍 Farm Placed"),
}

# Human-readable names for serial_ids (for UI)
SERIAL_ID_NAMES = {
    12001: "Crops Matured",
    12002: "Water Needed",
    12003: "Harvest",
    12005: "Auto-Farm Started",
    12006: "Auto-Farm Canceled",
    12007: "Auto-Farm Ended",
    12009: "Farm Placed",
}

# Default config
DEFAULT_ENABLED_QUALITIES = [1, 2, 3, 4]
DEFAULT_ENABLED_SERIAL_IDS = [12001, 12002, 12003, 12005, 12006, 12007, 12009]
# ping_map: { quality_id_str: [serial_id, ...] }
DEFAULT_PING_MAP = {}

BLURPLE = 0x5865F2

SERIAL_ID_LIST = sorted(SERIAL_ID_NAMES.keys())


class HomesteadConfigView(LayoutView):
    """Components V2 LayoutView for configuring homestead notification settings."""

    def __init__(self, cog, user_id: int, config: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.config = config  # mutable dict: {enabled_qualities, enabled_serial_ids, ping_map}
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        inner_items = []

        # Header
        inner_items.append(TextDisplay(
            "# 🌾 Homestead Notification Settings\n"
            "Configure which events to receive and which should ping you."
        ))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # ── Section 1: Farm Quality Filters ──
        quality_options = [
            discord.SelectOption(
                label=name,
                value=str(qid),
                default=qid in self.config.get('enabled_qualities', DEFAULT_ENABLED_QUALITIES)
            )
            for qid, name in FARM_QUALITY_MAP.items()
        ]
        quality_select = Select(
            placeholder="Select farmlands to track...",
            options=quality_options,
            min_values=0,
            max_values=len(quality_options),
            custom_id="homestead_config_qualities"
        )
        quality_select.callback = self._on_quality_select
        quality_row = ActionRow()
        quality_row.add_item(quality_select)
        inner_items.append(TextDisplay("### 🌱 Farmlands to Track\nChoose which farmlands to receive notifications for."))
        inner_items.append(quality_row)
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # ── Section 2: Event Type Filters ──
        event_options = [
            discord.SelectOption(
                label=name,
                value=str(sid),
                default=sid in self.config.get('enabled_serial_ids', DEFAULT_ENABLED_SERIAL_IDS)
            )
            for sid, name in SERIAL_ID_NAMES.items()
        ]
        event_select = Select(
            placeholder="Select event types to track...",
            options=event_options,
            min_values=0,
            max_values=len(event_options),
            custom_id="homestead_config_events"
        )
        event_select.callback = self._on_event_select
        event_row = ActionRow()
        event_row.add_item(event_select)
        inner_items.append(TextDisplay("### 📋 Event Types to Track\nChoose which events to receive notifications for."))
        inner_items.append(event_row)
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # ── Section 3: Per-Farmland Ping Settings ──
        inner_items.append(TextDisplay("### 🔔 Ping Settings — Per Farmland\n"
                                       "For each farmland, select which event types should @mention you."))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        ping_map = self.config.get('ping_map', {})

        for qid in sorted(FARM_QUALITY_MAP.keys()):
            farm_name = FARM_QUALITY_MAP[qid]
            qid_str = str(qid)
            current_ping_events = ping_map.get(qid_str, [])

            ping_options = [
                discord.SelectOption(
                    label=name,
                    value=str(sid),
                    default=sid in current_ping_events
                )
                for sid, name in SERIAL_ID_NAMES.items()
            ]
            ping_select = Select(
                placeholder=f"Ping events for {farm_name}...",
                options=ping_options,
                min_values=0,
                max_values=len(ping_options),
                custom_id=f"homestead_ping_{qid}"
            )
            # Use a closure to capture qid_str
            def _make_ping_callback(qid_s):
                async def callback(interaction: discord.Interaction):
                    if interaction.user.id != self.user_id:
                        await interaction.response.send_message("You cannot modify these settings.", ephemeral=True)
                        return
                    values = interaction.data.get('values', [])
                    ping_map = self.config.get('ping_map', {})
                    ping_map[qid_s] = [int(v) for v in values]
                    self.config['ping_map'] = ping_map
                    self._rebuild()
                    await interaction.response.edit_message(content=None, view=self)
                return callback
            ping_select.callback = _make_ping_callback(qid_str)
            ping_row = ActionRow()
            ping_row.add_item(ping_select)
            inner_items.append(TextDisplay(f"**{farm_name}**"))
            inner_items.append(ping_row)
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # ── Summary ──
        summary = self._build_summary()
        inner_items.append(TextDisplay(summary))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # ── Save Button ──
        save_row = ActionRow()
        save_btn = Button(
            label="💾 Save Settings",
            style=discord.ButtonStyle.green,
            custom_id="homestead_config_save"
        )
        save_btn.callback = self._on_save
        save_row.add_item(save_btn)
        inner_items.append(save_row)

        container = Container(*inner_items, accent_color=BLURPLE)
        self.add_item(container)

    def _build_summary(self) -> str:
        """Build a summary of current settings."""
        enabled_qualities = self.config.get('enabled_qualities', DEFAULT_ENABLED_QUALITIES)
        enabled_serial_ids = self.config.get('enabled_serial_ids', DEFAULT_ENABLED_SERIAL_IDS)
        ping_map = self.config.get('ping_map', {})

        quality_names = [FARM_QUALITY_MAP[q] for q in enabled_qualities if q in FARM_QUALITY_MAP]
        event_names = [SERIAL_ID_NAMES[s] for s in enabled_serial_ids if s in SERIAL_ID_NAMES]

        lines = ["### 📊 Current Settings\n"]
        lines.append(f"**Farmlands Tracked:** {', '.join(quality_names) if quality_names else 'None'}")
        lines.append(f"**Events Tracked:** {', '.join(event_names) if event_names else 'None'}")
        lines.append("")

        # Per-farmland ping summary
        has_any_ping = False
        for qid in sorted(FARM_QUALITY_MAP.keys()):
            qid_str = str(qid)
            farm_name = FARM_QUALITY_MAP[qid]
            ping_events = ping_map.get(qid_str, [])
            if ping_events:
                has_any_ping = True
                event_names_list = [SERIAL_ID_NAMES[s] for s in ping_events if s in SERIAL_ID_NAMES]
                lines.append(f"**{farm_name} Pings:** {', '.join(event_names_list)}")
            else:
                lines.append(f"**{farm_name} Pings:** None")

        if not has_any_ping:
            lines.append("\n*No pings configured. You won't be @mentioned.*")

        return "\n".join(lines)

    async def _on_quality_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot modify these settings.", ephemeral=True)
            return
        values = interaction.data.get('values', [])
        self.config['enabled_qualities'] = [int(v) for v in values]
        self._rebuild()
        await interaction.response.edit_message(content=None, view=self)

    async def _on_event_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot modify these settings.", ephemeral=True)
            return
        values = interaction.data.get('values', [])
        self.config['enabled_serial_ids'] = [int(v) for v in values]
        self._rebuild()
        await interaction.response.edit_message(content=None, view=self)

    async def _on_save(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot modify these settings.", ephemeral=True)
            return
        # Save to database
        await self.cog._save_homestead_config(self.user_id, self.config)
        # Build a success view (V2 can't use content/embed)
        success_view = LayoutView(timeout=None)
        success_items = []
        success_items.append(TextDisplay("# ✅ Settings Saved\nYour homestead notification preferences have been updated."))
        success_view.add_item(Container(*success_items, accent_color=0x2ECC71))
        await interaction.response.edit_message(view=success_view)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, (Button, Select)):
                                item.disabled = True


class HomesteadCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self._init_database()
        self.homestead_polling_loop.start()

    async def cog_unload(self):
        if self.homestead_polling_loop.is_running():
            self.homestead_polling_loop.cancel()

    async def _init_database(self):
        """Initialize the homestead database."""
        async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
            # Subscriptions table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS homestead_subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    character_uid TEXT NOT NULL,
                    player_pid TEXT NOT NULL,
                    avatar TEXT NOT NULL,
                    thread_id INTEGER,
                    last_history_id TEXT,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
            ''')
            # Config table — ping_map replaces ping_qualities + ping_serial_ids
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS homestead_config (
                    user_id INTEGER PRIMARY KEY,
                    enabled_qualities TEXT NOT NULL DEFAULT '[1,2,3,4]',
                    enabled_serial_ids TEXT NOT NULL DEFAULT '[12001,12002,12003,12005,12006,12007,12009]',
                    ping_map TEXT NOT NULL DEFAULT '{}'
                )
            ''')
            await conn.commit()
        logger.info("✅ Initialized homestead database")

    # ------------------------------------------------------------------
    # /homestead command group
    # ------------------------------------------------------------------
    homestead = app_commands.Group(name="homestead", description="Manage your homestead notifications")

    @homestead.command(name="setup", description="Set up homestead notifications in a forum thread")
    async def homestead_setup(self, interaction: discord.Interaction):
        """Set up homestead notifications for the user."""
        await interaction.response.defer(ephemeral=True)

        # 1. Check if user is bound
        bound_record = await self._get_bound_record(interaction.user.id)
        if not bound_record:
            await interaction.followup.send(
                "❌ You need to bind your account first in <#1469961307154288703>.",
                ephemeral=True
            )
            return

        user_id, username, character_uid, player_pid = bound_record
        avatar = player_pid  # player_pid IS the avatar string

        # 2. Fetch player nickname for the thread title
        nickname = await self._fetch_player_nickname(player_pid)
        if not nickname:
            nickname = username  # fallback to discord username

        # 3. Get or create forum thread
        thread = await self._get_or_create_forum_thread(nickname, user_id)
        if not thread:
            await interaction.followup.send(
                "❌ Failed to create or find your forum thread. Please contact an admin.",
                ephemeral=True
            )
            return

        # 4. Store/update subscription in DB (preserve last_history_id if already exists)
        now = datetime.datetime.utcnow()
        async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
            # Check if we already have a last_history_id to preserve
            cursor = await conn.execute(
                "SELECT last_history_id FROM homestead_subscriptions WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            existing_last_history_id = row[0] if row else None

            await conn.execute('''
                INSERT OR REPLACE INTO homestead_subscriptions
                (user_id, character_uid, player_pid, avatar, thread_id, last_history_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                character_uid,
                player_pid,
                avatar,
                thread.id,
                existing_last_history_id,
                now,
                now,
            ))
            await conn.commit()

        # 5. Ensure config exists with defaults
        config = await self._get_homestead_config(user_id)
        if config is None:
            config = {
                'enabled_qualities': DEFAULT_ENABLED_QUALITIES,
                'enabled_serial_ids': DEFAULT_ENABLED_SERIAL_IDS,
                'ping_map': {},
            }
            await self._save_homestead_config(user_id, config)

        # 6. Show config panel
        config_view = HomesteadConfigView(self, user_id, config)
        await interaction.followup.send(view=config_view, ephemeral=True)
        logger.info(f"Homestead setup complete for user {user_id} ({character_uid}), thread: {thread.id}")

    @homestead.command(name="config", description="Open homestead notification settings")
    async def homestead_config(self, interaction: discord.Interaction):
        """Open the configuration panel for homestead notifications."""
        async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM homestead_subscriptions WHERE user_id = ?",
                (interaction.user.id,)
            )
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(
                    "❌ You haven't set up homestead notifications yet. Use `/homestead setup` first.",
                    ephemeral=True
                )
                return

        config = await self._get_homestead_config(interaction.user.id)
        if config is None:
            config = {
                'enabled_qualities': DEFAULT_ENABLED_QUALITIES,
                'enabled_serial_ids': DEFAULT_ENABLED_SERIAL_IDS,
                'ping_map': {},
            }

        config_view = HomesteadConfigView(self, interaction.user.id, config)
        await interaction.response.send_message(view=config_view, ephemeral=True)

    @homestead.command(name="stop", description="Stop homestead notifications and unsubscribe")
    async def homestead_stop(self, interaction: discord.Interaction):
        """Stop homestead notifications for the user."""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT thread_id FROM homestead_subscriptions WHERE user_id = ?",
                (interaction.user.id,)
            )
            row = await cursor.fetchone()
            if not row:
                await interaction.followup.send(
                    "❌ You don't have homestead notifications set up.",
                    ephemeral=True
                )
                return

            await conn.execute(
                "DELETE FROM homestead_subscriptions WHERE user_id = ?",
                (interaction.user.id,)
            )
            await conn.execute(
                "DELETE FROM homestead_config WHERE user_id = ?",
                (interaction.user.id,)
            )
            await conn.commit()

        await interaction.followup.send(
            "✅ Homestead notifications have been stopped.",
            ephemeral=True
        )
        logger.info(f"Homestead stopped for user {interaction.user.id}")

    # ------------------------------------------------------------------
    # Background polling loop
    # ------------------------------------------------------------------
    @tasks.loop(seconds=60)
    async def homestead_polling_loop(self):
        """Poll for new homestead events every 60 seconds."""
        try:
            subscriptions = await self._get_all_subscriptions()
            if not subscriptions:
                return

            logger.debug(f"Homestead polling: checking {len(subscriptions)} subscriptions")

            for sub in subscriptions:
                user_id, character_uid, player_pid, avatar, thread_id, last_history_id = sub
                try:
                    await self._process_subscription(
                        user_id, player_pid, avatar, thread_id, last_history_id
                    )
                except Exception as e:
                    logger.error(f"Homestead polling failed for user {user_id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Homestead polling loop error: {e}", exc_info=True)

    @homestead_polling_loop.before_loop
    async def before_polling_loop(self):
        await self.bot.wait_until_ready()

    async def _process_subscription(
        self, user_id: int, player_pid: str, avatar: str, thread_id: int, last_history_id: str
    ):
        """Fetch new game history events for a single subscription and post them."""
        # Load user's config
        config = await self._get_homestead_config(user_id)
        if config is None:
            config = {
                'enabled_qualities': DEFAULT_ENABLED_QUALITIES,
                'enabled_serial_ids': DEFAULT_ENABLED_SERIAL_IDS,
                'ping_map': {},
            }

        enabled_qualities = config.get('enabled_qualities', DEFAULT_ENABLED_QUALITIES)
        enabled_serial_ids = config.get('enabled_serial_ids', DEFAULT_ENABLED_SERIAL_IDS)
        ping_map = config.get('ping_map', {})

        # Derive entity_id from avatar
        last_char = avatar[-1]
        entity_last_char = chr(ord(last_char) + 2)
        entity_id = avatar[:-1] + entity_last_char

        payload = {
            "type": 1,
            "sub_type": 2,
            "entity_id": entity_id,
            "avatar": avatar,
            "start": 0,
            "uid": "1",
        }

        result = await _wwm_api_post(WWM_LIST_GAME_HISTORY_URL, payload)
        if not result or result.get('code') != 0 or 'result' not in result:
            logger.warning(f"Homestead: API request failed for user {user_id}")
            return

        events = result.get('result', [])
        if not events:
            return

        # Filter new events (those after last_history_id)
        new_events = []
        found_last = (last_history_id is None)
        for event in reversed(events):
            history_id = event.get('history_id', '')
            if not found_last and history_id == last_history_id:
                found_last = True
                continue
            if found_last:
                new_events.append(event)

        if not new_events:
            return

        # Sort by time ascending
        new_events.sort(key=lambda e: e.get('time', 0))

        # Get the forum thread
        thread = self.bot.get_channel(thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden):
                logger.warning(f"Homestead: Could not find thread {thread_id} for user {user_id}")
                return

        # Post each new event as an embed (with optional ping)
        for event in new_events:
            text_param = event.get('text_param', [])
            serial_id = event.get('serial_id', 0)
            quality_raw = self._get_text_param(text_param, 1005)
            quality_id = int(quality_raw) if quality_raw is not None and not isinstance(quality_raw, int) else (quality_raw if quality_raw is not None else None)

            # Apply filters
            if quality_id is not None and quality_id not in enabled_qualities:
                continue
            if serial_id not in enabled_serial_ids:
                continue

            embed = self._format_event_embed(event)
            if not embed:
                continue

            # Check ping_map: which serial_ids should ping for this quality
            should_ping = False
            if quality_id is not None:
                qid_str = str(quality_id)
                ping_events_for_quality = ping_map.get(qid_str, [])
                should_ping = serial_id in ping_events_for_quality

            content = None
            if should_ping:
                content = f"<@{user_id}>"

            try:
                await thread.send(content=content, embed=embed)
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.error(f"Homestead: Failed to send embed to thread {thread_id}: {e}")
                break

        # Update last_history_id to the latest processed event
        latest_history_id = new_events[-1].get('history_id', '')
        async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
            await conn.execute(
                "UPDATE homestead_subscriptions SET last_history_id = ?, updated_at = ? WHERE user_id = ?",
                (latest_history_id, datetime.datetime.utcnow(), user_id)
            )
            await conn.commit()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    async def _get_homestead_config(self, user_id: int) -> dict:
        """Get a user's homestead config, or None if not set up."""
        try:
            async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT enabled_qualities, enabled_serial_ids, ping_map FROM homestead_config WHERE user_id = ?",
                    (user_id,)
                )
                row = await cursor.fetchone()
                if row:
                    return {
                        'enabled_qualities': json.loads(row[0]),
                        'enabled_serial_ids': json.loads(row[1]),
                        'ping_map': json.loads(row[2]),
                    }
                return None
        except Exception as e:
            logger.error(f"Failed to get homestead config for {user_id}: {e}")
            return None

    async def _save_homestead_config(self, user_id: int, config: dict):
        """Save a user's homestead config."""
        try:
            async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
                await conn.execute('''
                    INSERT OR REPLACE INTO homestead_config
                    (user_id, enabled_qualities, enabled_serial_ids, ping_map)
                    VALUES (?, ?, ?, ?)
                ''', (
                    user_id,
                    json.dumps(config.get('enabled_qualities', DEFAULT_ENABLED_QUALITIES)),
                    json.dumps(config.get('enabled_serial_ids', DEFAULT_ENABLED_SERIAL_IDS)),
                    json.dumps(config.get('ping_map', {})),
                ))
                await conn.commit()
                logger.debug(f"Saved homestead config for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to save homestead config for {user_id}: {e}")

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_text_param(text_param: list, key):
        """Get a value from text_param list by key (supports both int and str keys)."""
        for param in text_param:
            if isinstance(param, dict):
                if key in param:
                    return param[key]
                str_key = str(key)
                if str_key in param:
                    return param[str_key]
                int_key = int(key) if isinstance(key, str) else key
                if int_key in param:
                    return param[int_key]
        return None

    def _format_event_embed(self, event: dict) -> discord.Embed:
        """Format a single game history event into a Discord embed."""
        serial_id = event.get('serial_id', 0)
        text_param = event.get('text_param', [])
        event_time = event.get('time', 0)
        creator = event.get('creator', '')

        color, title = SERIAL_ID_EMBED_META.get(serial_id, (0x5865F2, f"Event #{serial_id}"))

        quality_raw = self._get_text_param(text_param, 1005)
        if quality_raw is not None:
            quality = FARM_QUALITY_MAP.get(
                int(quality_raw) if not isinstance(quality_raw, int) else quality_raw,
                f"Farmland ({quality_raw})"
            )
        else:
            quality = "Farmland"

        player_name = self._get_text_param(text_param, 10000)
        if not player_name:
            player_name = creator if creator else None

        embed = discord.Embed(title=title, color=color)

        if serial_id == 12001:
            embed.description = f"🌾 **{quality}**'s crops have matured!"
        elif serial_id == 12002:
            embed.description = f"💧 **{quality}** is short of water."
        elif serial_id == 12003:
            embed.description = f"👤 **{player_name or 'Unknown'}** has harvested **{quality}**'s crops."
        elif serial_id == 12005:
            embed.description = f"🤖 **{player_name or 'Unknown'}** started Auto-Farm on **{quality}**."
        elif serial_id == 12006:
            embed.description = f"⏹️ **{player_name or 'Unknown'}** canceled Auto-Farm on **{quality}**."
        elif serial_id == 12007:
            embed.description = f"🌱 Auto-Farm on **{quality}** has ended due to insufficient seeds."
        elif serial_id == 12009:
            embed.description = f"📍 **{player_name or 'Unknown'}** placed **{quality}** in Qinghe."
        else:
            logger.debug(f"Homestead: Unknown serial_id {serial_id}, skipping")
            return None

        if event_time:
            embed.add_field(
                name="🕐 Time",
                value=f"<t:{event_time}:F> (<t:{event_time}:R>)",
                inline=False
            )

        return embed

    async def _fetch_player_nickname(self, player_pid: str) -> str:
        """Fetch player nickname using the bulk player info endpoint."""
        try:
            from utility.wwm import get_bulk_players_info
            result = await get_bulk_players_info([player_pid], fields=["base"])
            if result and result.get('code') == 0:
                players = result.get('result', {})
                if player_pid in players:
                    return players[player_pid].get('base', {}).get('nickname', None)
        except Exception as e:
            logger.warning(f"Failed to fetch nickname for {player_pid}: {e}")
        return None

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------
    async def _get_bound_record(self, discord_user_id: int):
        """Check if a Discord user is bound in the verification database."""
        try:
            async with aiosqlite.connect(VERIFICATION_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT user_id, username, character_uid, player_pid FROM verified_members WHERE user_id = ?",
                    (discord_user_id,)
                )
                row = await cursor.fetchone()
                return row
        except Exception as e:
            logger.error(f"Failed to check bound record for {discord_user_id}: {e}")
            return None

    async def _get_all_subscriptions(self) -> list:
        """Get all active homestead subscriptions."""
        try:
            async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT user_id, character_uid, player_pid, avatar, thread_id, last_history_id FROM homestead_subscriptions"
                )
                rows = await cursor.fetchall()
                return rows
        except Exception as e:
            logger.error(f"Failed to fetch subscriptions: {e}")
            return []

    async def _get_or_create_forum_thread(self, nickname: str, user_id: int) -> discord.Thread:
        """Get existing thread for this user, or create a new one in the forum channel."""
        forum_channel = self.bot.get_channel(FORUM_CHANNEL_ID)
        if not forum_channel:
            forum_channel = await self.bot.fetch_channel(FORUM_CHANNEL_ID)

        if not forum_channel:
            logger.error(f"Could not find forum channel {FORUM_CHANNEL_ID}")
            return None

        # Verify channel type and bot permissions before attempting thread creation
        if not isinstance(forum_channel, discord.ForumChannel):
            logger.error(
                f"Channel {FORUM_CHANNEL_ID} is type {type(forum_channel).__name__}, not ForumChannel"
            )
            return None

        # Sanity check: admin bots should still be able to create threads
        bot_member = forum_channel.guild.me
        if not bot_member:
            logger.error(f"Could not resolve bot member in guild {forum_channel.guild.id}")
            return None

        perms = forum_channel.permissions_for(bot_member)
        if not perms.send_messages or not perms.manage_threads:
            logger.error(
                f"Bot lacks send_messages={perms.send_messages} or manage_threads={perms.manage_threads} "
                f"in forum channel {FORUM_CHANNEL_ID}"
            )
            return None

        # Check if forum is archived/locked — admins can still create threads unless locked
        try:
            if getattr(forum_channel, 'archived', False):
                logger.error(f"Forum channel {FORUM_CHANNEL_ID} is archived")
                return None
        except Exception:
            pass

        # Helper to grant forum access to a member
        async def _grant_forum_access(member):
            try:
                await forum_channel.set_permissions(
                    member,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                    reason=f"Homestead notification setup for user {user_id}"
                )
                logger.debug(f"Granted forum access to user {user_id}")
            except discord.Forbidden:
                logger.warning(f"Failed to grant forum access to user {user_id}: Missing permissions")
            except Exception as perm_error:
                logger.error(f"Failed to grant forum access to user {user_id}: {perm_error}")

        # Check for existing thread
        async with aiosqlite.connect(HOMESTEAD_DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT thread_id FROM homestead_subscriptions WHERE user_id = ? AND thread_id IS NOT NULL",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                thread_id = row[0]
                try:
                    thread = self.bot.get_channel(thread_id)
                    if not thread:
                        thread = await self.bot.fetch_channel(thread_id)
                    if thread:
                        # Ensure user has access to the forum channel even for existing threads
                        member = thread.guild.get_member(user_id)
                        if member:
                            await _grant_forum_access(member)
                        return thread
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"Stored thread {thread_id} not found for user {user_id}, creating new one")

        # Create new thread
        try:
            thread, message = await forum_channel.create_thread(
                name=f"🌾 {nickname}'s Homestead",
                content=f"🌾 **{nickname}'s Homestead Notifications**\n\n"
                        f"This thread will receive real-time updates about your farmland.\n"
                        f"Use `/homestead config` to customize your notification settings.",
                reason=f"Homestead notification setup for user {user_id}",
            )

            # Add the user as a participant to their own thread
            member = thread.guild.get_member(user_id)
            if member:
                await thread.add_user(member)
                logger.debug(f"Added user {user_id} to thread {thread.id}")

            # Grant the user permission to view and access the forum channel
            if member:
                await _grant_forum_access(member)

            return thread
        except discord.Forbidden as e:
            logger.error(f"Failed to create forum thread for user {user_id}: 403 Forbidden - Bot may lack 'Manage Threads' permission in the forum channel")
            return None
        except Exception as e:
            logger.error(f"Failed to create forum thread for user {user_id}: {e}")
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(HomesteadCog(bot))