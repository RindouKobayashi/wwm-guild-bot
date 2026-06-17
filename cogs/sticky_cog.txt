import discord
import json
import os
import asyncio
import aiosqlite
import time
from discord.ext import commands
from discord import app_commands
from discord.ui import Select, Button, View, Modal, TextInput
from settings import logger, BASE_DIR

DB_PATH = BASE_DIR / "data" / "stickies.db"
MANAGE_PERMISSION = "manage_messages"

# ─────────────────────────────────────────────
# Database Initialization
# ─────────────────────────────────────────────

async def init_db():
    """Initialize the database and create tables if they don't exist."""
    os.makedirs(DB_PATH.parent, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS sticky_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                title TEXT DEFAULT '',
                description TEXT DEFAULT '',
                color INTEGER DEFAULT 3447003,
                fields TEXT DEFAULT '[]',
                footer_text TEXT DEFAULT '',
                idle_timeout INTEGER DEFAULT 180,
                plain_text TEXT,
                is_embed INTEGER DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS sticky_state (
                sticky_id INTEGER PRIMARY KEY,
                sticky_message_id INTEGER,
                last_message_id INTEGER,
                last_message_time REAL DEFAULT 0,
                is_paused INTEGER DEFAULT 0,
                FOREIGN KEY (sticky_id) REFERENCES sticky_configs(id) ON DELETE CASCADE
            )
        ''')
        await conn.commit()
    logger.debug("Sticky database initialized")


# ─────────────────────────────────────────────
# StickyManager — All DB Operations
# ─────────────────────────────────────────────

class StickyManager:
    """Handles all database operations for stickies."""

    @staticmethod
    async def create_sticky(guild_id: int, channel_id: int, author_id: int,
                      title: str = "", description: str = "",
                      color: int = 3447003, fields: list = None,
                      footer_text: str = "", idle_timeout: int = 180,
                      is_embed: bool = True, plain_text: str = None) -> int:
        """Create a new sticky config and its state row. Returns the sticky ID."""
        now = time.time()
        fields_json = json.dumps(fields or [])
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute('''
                INSERT INTO sticky_configs
                    (guild_id, channel_id, author_id, title, description, color,
                     fields, footer_text, idle_timeout, is_embed, plain_text,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (guild_id, channel_id, author_id, title, description, color,
                  fields_json, footer_text, idle_timeout, 1 if is_embed else 0,
                  plain_text, now, now))
            sticky_id = cursor.lastrowid
            await conn.execute('''
                INSERT INTO sticky_state (sticky_id, last_message_time)
                VALUES (?, ?)
            ''', (sticky_id, now))
            await conn.commit()
            return sticky_id

    @staticmethod
    async def update_sticky(sticky_id: int, **kwargs):
        """Update sticky config fields. Pass column names as kwargs."""
        allowed = {'title', 'description', 'color', 'fields', 'footer_text',
                   'idle_timeout', 'is_embed', 'plain_text', 'channel_id'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if 'fields' in updates and isinstance(updates['fields'], list):
            updates['fields'] = json.dumps(updates['fields'])
        if 'is_embed' in updates:
            updates['is_embed'] = 1 if updates['is_embed'] else 0
        updates['updated_at'] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [sticky_id]
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(f"UPDATE sticky_configs SET {set_clause} WHERE id = ?", values)
            await conn.commit()

    @staticmethod
    async def delete_sticky(sticky_id: int):
        """Delete a sticky config and its state."""
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM sticky_state WHERE sticky_id = ?", (sticky_id,))
            await conn.execute("DELETE FROM sticky_configs WHERE id = ?", (sticky_id,))
            await conn.commit()

    @staticmethod
    async def get_guild_stickies(guild_id: int) -> list:
        """Get all sticky configs for a guild, joined with state."""
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute('''
                SELECT c.*, s.sticky_message_id, s.last_message_id,
                       s.last_message_time, s.is_paused
                FROM sticky_configs c
                LEFT JOIN sticky_state s ON s.sticky_id = c.id
                WHERE c.guild_id = ?
                ORDER BY c.created_at DESC
            ''', (guild_id,))
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if isinstance(d.get('fields'), str):
                    d['fields'] = json.loads(d['fields'])
                result.append(d)
            return result

    @staticmethod
    async def get_sticky(sticky_id: int) -> dict:
        """Get a single sticky config with state."""
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute('''
                SELECT c.*, s.sticky_message_id, s.last_message_id,
                       s.last_message_time, s.is_paused
                FROM sticky_configs c
                LEFT JOIN sticky_state s ON s.sticky_id = c.id
                WHERE c.id = ?
            ''', (sticky_id,))
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                if isinstance(d.get('fields'), str):
                    d['fields'] = json.loads(d['fields'])
                return d
            return None

    @staticmethod
    async def get_stickies_for_channel(channel_id: int) -> list:
        """Get all active (non-paused) stickies targeting a channel."""
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute('''
                SELECT c.*, s.sticky_message_id, s.last_message_id,
                       s.last_message_time, s.is_paused
                FROM sticky_configs c
                LEFT JOIN sticky_state s ON s.sticky_id = c.id
                WHERE c.channel_id = ? AND (s.is_paused IS NULL OR s.is_paused = 0)
                ORDER BY c.created_at ASC
            ''', (channel_id,))
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if isinstance(d.get('fields'), str):
                    d['fields'] = json.loads(d['fields'])
                result.append(d)
            return result

    @staticmethod
    async def set_state(sticky_id: int, **kwargs):
        """Update sticky state fields. Pass column names as kwargs."""
        allowed = {'sticky_message_id', 'last_message_id', 'last_message_time', 'is_paused'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [sticky_id]
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(f"UPDATE sticky_state SET {set_clause} WHERE sticky_id = ?", values)
            await conn.commit()

    @staticmethod
    async def is_paused(sticky_id: int) -> bool:
        """Check if a sticky is paused."""
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT is_paused FROM sticky_state WHERE sticky_id = ?", (sticky_id,))
            row = await cursor.fetchone()
            return bool(row and row[0])

    @staticmethod
    def build_embed(config: dict) -> discord.Embed:
        """Build a discord.Embed from a sticky config dict."""
        embed = discord.Embed(
            title=config.get('title') or None,
            description=config.get('description') or None,
            color=config.get('color', 3447003)
        )
        for field in config.get('fields', []):
            embed.add_field(
                name=field.get('name', ''),
                value=field.get('value', ''),
                inline=field.get('inline', False)
            )
        if config.get('footer_text'):
            embed.set_footer(text=config['footer_text'])
        return embed


# ─────────────────────────────────────────────
# UI — Confirm / Edit Buttons (for preview)
# ─────────────────────────────────────────────

class ConfirmEditView(View):
    """Shown after previewing a sticky draft. Confirm saves it, Edit reopens the modal."""

    def __init__(self, cog, sticky_id: int, config: dict, is_edit: bool = False):
        super().__init__(timeout=120)
        self.cog = cog
        self.sticky_id = sticky_id
        self.config = config
        self.is_edit = is_edit

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        sticky_id = self.sticky_id
        if self.is_edit and self.sticky_id:
            await StickyManager.update_sticky(self.sticky_id, **self.config)
            sticky_id = self.sticky_id
        else:
            sticky_id = await StickyManager.create_sticky(
                guild_id=interaction.guild_id,
                channel_id=self.config['channel_id'],
                author_id=interaction.user.id,
                title=self.config.get('title', ''),
                description=self.config.get('description', ''),
                color=self.config.get('color', 3447003),
                fields=self.config.get('fields', []),
                footer_text=self.config.get('footer_text', ''),
                idle_timeout=self.config.get('idle_timeout', 180),
                is_embed=self.config.get('is_embed', True),
                plain_text=self.config.get('plain_text')
            )
        # Start the idle timer for the new/updated sticky
        await self.cog._start_sticky_timer(sticky_id)
        # If there's an old sticky message in the channel, remove it
        old_state = await StickyManager.get_sticky(sticky_id)
        channel_id = self.config.get('channel_id', old_state and old_state.get('channel_id', 0))
        if old_state and old_state.get('sticky_message_id'):
            try:
                channel = interaction.guild.get_channel(old_state['channel_id'])
                if channel:
                    try:
                        old_msg = await channel.fetch_message(old_state['sticky_message_id'])
                        await old_msg.delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass
            except Exception:
                pass
        await interaction.response.edit_message(
            content=f"✅ Sticky saved! It will appear after {self.config.get('idle_timeout', 180)}s of inactivity in <#{channel_id}>.",
            embed=None,
            view=None
        )

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: Button):
        # Reopen the appropriate modal with pre-filled values
        if self.config.get('is_embed', True):
            modal = CreateEmbedModal(self.cog, self.config, self.sticky_id, self.is_edit)
        else:
            modal = CreatePlainModal(self.cog, self.config, self.sticky_id, self.is_edit)
        await interaction.response.send_modal(modal)


# ─────────────────────────────────────────────
# UI — Create / Edit Modals
# ─────────────────────────────────────────────

class CreateEmbedModal(Modal):
    def __init__(self, cog, prefill: dict = None, sticky_id: int = None, is_edit: bool = False):
        super().__init__(title="Edit Sticky Message" if is_edit else "Create Sticky Message", timeout=300)
        self.cog = cog
        self.prefill = prefill or {}
        self.sticky_id = sticky_id
        self.is_edit = is_edit

        self.title_input = TextInput(
            label="Embed Title",
            placeholder="📖 Guild Bot Commands",
            default=self.prefill.get('title', ''),
            required=False,
            max_length=256
        )
        self.add_item(self.title_input)

        self.desc_input = TextInput(
            label="Embed Description",
            placeholder="Welcome! Use these commands to get the most out of the bot.",
            default=self.prefill.get('description', ''),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=4000
        )
        self.add_item(self.desc_input)

        prefill_fields = self.prefill.get('fields', [])
        fields_text = ""
        for f in prefill_fields:
            name = f.get('name', '')
            value = f.get('value', '')
            inline = f.get('inline', False)
            if inline:
                fields_text += f"{name} || {value} || yes\n---\n"
            else:
                fields_text += f"{name} || {value}\n---\n"
        self.fields_input = TextInput(
            label="Fields (separate with --- between each)",
            placeholder="🔗 Step 1 || Bind your account first! || no\n---\nStep 2 || More info with\nmultiple lines || yes",
            default=fields_text.strip().rstrip('\n---'),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        self.add_item(self.fields_input)

        self.footer_input = TextInput(
            label="Footer Text",
            placeholder="This message appears after inactivity",
            default=self.prefill.get('footer_text', ''),
            required=False,
            max_length=256
        )
        self.add_item(self.footer_input)

        default_timeout = str(self.prefill.get('idle_timeout', 180))
        self.timeout_input = TextInput(
            label="Idle Timeout (seconds)",
            placeholder="180",
            default=default_timeout,
            required=True,
            max_length=5
        )
        self.add_item(self.timeout_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Parse fields: each field block separated by "---", with "||" as separator
        fields = []
        raw = self.fields_input.value.strip()
        if raw:
            blocks = raw.split('---')
            for block in blocks:
                block = block.strip()
                if not block:
                    continue
                # First line is the name, rest is the value
                lines = block.split('\n')
                first_line = lines[0].strip() if lines else ''
                if '||' in first_line:
                    parts = [p.strip() for p in first_line.split('||', 2)]
                    name = parts[0]
                    value = parts[1] if len(parts) > 1 else ''
                    inline = len(parts) > 2 and parts[2].lower() in ('yes', 'true', '1')
                else:
                    name = first_line
                    value = ''
                    inline = False
                # Append remaining lines to value
                for extra_line in lines[1:]:
                    value += '\n' + extra_line
                value = value.strip()
                fields.append({'name': name, 'value': value, 'inline': inline})

        # Parse idle timeout
        try:
            idle_timeout = int(self.timeout_input.value)
            if idle_timeout < 10:
                idle_timeout = 10
        except ValueError:
            idle_timeout = 180

        config = {
            'title': self.title_input.value,
            'description': self.desc_input.value,
            'color': self.prefill.get('color', 3447003),
            'footer_text': self.footer_input.value,
            'fields': fields,
            'is_embed': True,
            'plain_text': None,
            'channel_id': self.prefill.get('channel_id'),
            'idle_timeout': idle_timeout
        }

        # Build preview embed
        embed = StickyManager.build_embed(config)
        view = ConfirmEditView(self.cog, self.sticky_id, config, self.is_edit)
        await interaction.response.send_message(
            "📝 **Preview of your sticky:**",
            embed=embed,
            view=view,
            ephemeral=True
        )


class CreatePlainModal(Modal):
    def __init__(self, cog, prefill: dict = None, sticky_id: int = None, is_edit: bool = False):
        super().__init__(title="Edit Sticky Message" if is_edit else "Create Sticky Message", timeout=300)
        self.cog = cog
        self.prefill = prefill or {}
        self.sticky_id = sticky_id
        self.is_edit = is_edit

        self.content_input = TextInput(
            label="Message Content",
            placeholder="Type your sticky message here...",
            default=self.prefill.get('plain_text', ''),
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        self.add_item(self.content_input)

        default_timeout = str(self.prefill.get('idle_timeout', 180))
        self.timeout_input = TextInput(
            label="Idle Timeout (seconds)",
            placeholder="180",
            default=default_timeout,
            required=True,
            max_length=5
        )
        self.add_item(self.timeout_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            idle_timeout = int(self.timeout_input.value)
            if idle_timeout < 10:
                idle_timeout = 10
        except ValueError:
            idle_timeout = 180

        config = {
            'is_embed': False,
            'plain_text': self.content_input.value,
            'title': '',
            'description': '',
            'color': 3447003,
            'footer_text': '',
            'fields': [],
            'channel_id': self.prefill.get('channel_id'),
            'idle_timeout': idle_timeout
        }

        view = ConfirmEditView(self.cog, self.sticky_id, config, self.is_edit)
        preview_text = f"📝 **Preview of your sticky:**\n{self.content_input.value}"
        await interaction.response.send_message(
            content=preview_text,
            view=view,
            ephemeral=True
        )


# ─────────────────────────────────────────────
# UI — Sticky Manage Panel (Main View)
# ─────────────────────────────────────────────
# Uses an async factory to build the panel so the Select gets populated with the guild's stickies.

class StickyManagePanel(View):
    """Main management panel with sticky select dropdown and action buttons."""

    def __init__(self, cog, user_id: int, guild: discord.Guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.selected_sticky_id = None
        self.guild = guild

        # Build options from DB
        # We can't await here, so we'll create and populate async
        self.sticky_select = Select(
            custom_id="sticky_select",
            placeholder="Select a sticky...",
            options=[discord.SelectOption(label="Loading...", value="__loading__")],
            row=0
        )
        self.sticky_select.callback = self._on_sticky_select
        self.add_item(self.sticky_select)

    async def populate(self):
        """Populate the select options asynchronously."""
        stickies = await StickyManager.get_guild_stickies(self.guild.id)
        options = self._build_select_options(stickies)
        self.sticky_select.options = options

    def _build_select_options(self, stickies: list) -> list:
        """Build select options from sticky list."""
        if not stickies:
            return [discord.SelectOption(label="No stickies configured", value="__none__", default=True)]
        options = []
        for s in stickies:
            title = s.get('title', '') or "(plain text)"
            label = f"[{s['id']}] {title[:60]}"
            if len(label) > 100:
                label = label[:97] + "..."
            paused = " ⏸️" if s.get('is_paused') else ""
            desc = f"#{s['channel_id']} | {'Paused' if s.get('is_paused') else 'Active'}{paused}"
            options.append(discord.SelectOption(
                label=label,
                value=str(s['id']),
                description=desc[:100] if desc else None
            ))
        return options

    async def _on_sticky_select(self, interaction: discord.Interaction):
        value = self.sticky_select.values[0]
        if value == "__none__":
            return
        self.selected_sticky_id = int(value)
        await self._update_button_states()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        """Build the main panel embed listing all stickies."""
        stickies = await StickyManager.get_guild_stickies(self.guild.id)

        embed = discord.Embed(
            title="📌 Sticky Message Manager",
            description="Select a sticky below to manage it, or create a new one.",
            color=discord.Color.blue()
        )

        if not stickies:
            embed.description = "No stickies configured yet. Click **➕ Create New** to get started."
        else:
            lines = []
            for s in stickies:
                channel = self.guild.get_channel(s['channel_id'])
                ch_name = f"#{channel.name}" if channel else "deleted-channel"
                title = s.get('title', '') or "(plain text)"
                if len(title) > 40:
                    title = title[:37] + "..."
                paused = " ⏸️" if s.get('is_paused') else ""
                status = "🟢 Active" if not s.get('is_paused') else "🔴 Paused"
                lines.append(f"**`{s['id']}`** — {ch_name} — \"{title}\" — {status}{paused}")
            embed.description = "Select a sticky from the dropdown below.\n\n" + "\n".join(lines)

        embed.set_footer(text="Panel auto-closes after 5 minutes of inactivity")
        return embed

    async def _update_button_states(self):
        """Enable/disable buttons based on whether a sticky is selected."""
        has_selection = self.selected_sticky_id is not None
        for child in self.children:
            if isinstance(child, Button):
                if child.label in ("👁️ Preview", "✏️ Edit", "❌ Delete", "⚙️ Settings"):
                    child.disabled = not has_selection
                elif child.label in ("⏸️ Pause", "▶️ Resume"):
                    child.disabled = not has_selection

    @discord.ui.button(label="➕ Create New", style=discord.ButtonStyle.success, row=1)
    async def create_new(self, interaction: discord.Interaction, button: Button):
        channels = self.guild.text_channels
        options = []
        for ch in channels:
            if ch.permissions_for(self.guild.me).send_messages:
                label = f"#{ch.name}"
                if len(label) > 100:
                    label = label[:97] + "..."
                options.append(discord.SelectOption(label=label, value=str(ch.id)))

        if not options:
            await interaction.response.send_message("❌ No available text channels to post in.", ephemeral=True)
            return

        view = ChannelSelectView(self.cog, self.user_id)
        select = Select(
            placeholder="Select a channel for the sticky...",
            options=options[:25],
            row=0
        )

        async def channel_select_callback(interaction: discord.Interaction):
            channel_id = int(select.values[0])
            type_view = TypeSelectView(self.cog, {'channel_id': channel_id, 'idle_timeout': 180}, None, self.user_id)
            embed = discord.Embed(
                title="Step 2: Choose Message Type",
                description="Would you like an **Embed** message or a **Plain Text** message?",
                color=discord.Color.blue()
            )
            await interaction.response.edit_message(embed=embed, view=type_view)

        select.callback = channel_select_callback
        view.add_item(select)

        embed = discord.Embed(
            title="Step 1: Select Channel",
            description="Choose the text channel where this sticky will appear.",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="👁️ Preview", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def preview(self, interaction: discord.Interaction, button: Button):
        if not self.selected_sticky_id:
            return
        sticky = await StickyManager.get_sticky(self.selected_sticky_id)
        if not sticky:
            await interaction.response.send_message("❌ Sticky not found.", ephemeral=True)
            return
        if sticky.get('is_embed'):
            embed = StickyManager.build_embed(sticky)
            await interaction.response.send_message("📝 **Sticky Preview:**", embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                "📝 **Sticky Preview:**\n" + (sticky.get('plain_text') or "(empty)"),
                ephemeral=True
            )

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def edit(self, interaction: discord.Interaction, button: Button):
        if not self.selected_sticky_id:
            return
        sticky = await StickyManager.get_sticky(self.selected_sticky_id)
        if not sticky:
            await interaction.response.send_message("❌ Sticky not found.", ephemeral=True)
            return
        config = {
            'title': sticky.get('title', ''),
            'description': sticky.get('description', ''),
            'color': sticky.get('color', 3447003),
            'footer_text': sticky.get('footer_text', ''),
            'fields': sticky.get('fields', []),
            'is_embed': sticky.get('is_embed', True),
            'plain_text': sticky.get('plain_text'),
            'channel_id': sticky['channel_id'],
            'idle_timeout': sticky.get('idle_timeout', 180)
        }
        if sticky.get('is_embed'):
            modal = CreateEmbedModal(self.cog, config, self.selected_sticky_id, is_edit=True)
        else:
            modal = CreatePlainModal(self.cog, config, self.selected_sticky_id, is_edit=True)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.secondary, row=2, disabled=True)
    async def pause_resume(self, interaction: discord.Interaction, button: Button):
        if not self.selected_sticky_id:
            return
        sticky = await StickyManager.get_sticky(self.selected_sticky_id)
        if not sticky:
            return
        was_paused = bool(sticky.get('is_paused'))
        new_paused = 0 if was_paused else 1
        await StickyManager.set_state(self.selected_sticky_id, is_paused=new_paused)

        if new_paused:
            await self.cog._cancel_sticky_timer(self.selected_sticky_id)
            action = "Paused"
            label = "▶️ Resume"
        else:
            await self.cog._start_sticky_timer(self.selected_sticky_id)
            action = "Resumed"
            label = "⏸️ Pause"

        button.label = label
        await self._update_button_states()
        await interaction.response.send_message(
            f"{'▶️' if action == 'Resumed' else '⏸️'} {action} sticky **#{self.selected_sticky_id}**.",
            ephemeral=True,
            delete_after=3
        )
        self.selected_sticky_id = None
        # Rebuild panel
        view = StickyManagePanel(self.cog, self.user_id, self.guild)
        await view.populate()
        embed = await view.build_embed()
        await interaction.edit_original_response(embed=embed, view=view)

    @discord.ui.button(label="❌ Delete", style=discord.ButtonStyle.danger, row=2, disabled=True)
    async def delete(self, interaction: discord.Interaction, button: Button):
        if not self.selected_sticky_id:
            return
        sticky = await StickyManager.get_sticky(self.selected_sticky_id)
        if not sticky:
            return

        confirm_view = View(timeout=30)
        del_id = self.selected_sticky_id

        async def confirm_callback(interaction: discord.Interaction):
            await self.cog._cancel_sticky_timer(del_id)
            if sticky.get('sticky_message_id'):
                try:
                    channel = interaction.guild.get_channel(sticky['channel_id'])
                    if channel:
                        try:
                            old_msg = await channel.fetch_message(sticky['sticky_message_id'])
                            await old_msg.delete()
                        except (discord.NotFound, discord.Forbidden):
                            pass
                except Exception:
                    pass
            await StickyManager.delete_sticky(del_id)
            await interaction.response.edit_message(
                content=f"✅ Sticky **#{del_id}** deleted.",
                embed=None,
                view=None
            )
            view = StickyManagePanel(self.cog, self.user_id, self.guild)
            await view.populate()
            embed = await view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        async def cancel_callback(interaction: discord.Interaction):
            await interaction.response.edit_message(content="❌ Deletion cancelled.", embed=None, view=None)
            view = StickyManagePanel(self.cog, self.user_id, self.guild)
            await view.populate()
            embed = await view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        confirm_view.add_item(Button(label="✅ Yes, Delete", style=discord.ButtonStyle.danger))
        confirm_view.add_item(Button(label="❌ Cancel", style=discord.ButtonStyle.secondary))
        confirm_view.children[0].callback = confirm_callback
        confirm_view.children[1].callback = cancel_callback

        ch = interaction.guild.get_channel(sticky['channel_id'])
        ch_str = f"#{ch.name}" if ch else "deleted-channel"
        embed = discord.Embed(
            title="⚠️ Confirm Deletion",
            description=f"Are you sure you want to delete sticky **#{del_id}** in {ch_str}?",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=confirm_view)

    @discord.ui.button(label="⚙️ Settings", style=discord.ButtonStyle.secondary, row=2, disabled=True)
    async def settings(self, interaction: discord.Interaction, button: Button):
        if not self.selected_sticky_id:
            return
        sticky = await StickyManager.get_sticky(self.selected_sticky_id)
        if not sticky:
            return
        modal = SettingsModal(
            self.cog,
            self.selected_sticky_id,
            sticky.get('idle_timeout', 180),
            sticky.get('color', 3447003)
        )
        await interaction.response.send_modal(modal)


# ─────────────────────────────────────────────
# UI — Channel Selector for Create Flow
# ─────────────────────────────────────────────

class ChannelSelectView(View):
    """Wrapper view for channel selection during creation."""

    def __init__(self, cog, user_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id
        self.config = {}

    @discord.ui.button(label="⬅️ Back to Manager", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: Button):
        view = StickyManagePanel(self.cog, self.user_id, interaction.guild)
        await view.populate()
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class TypeSelectView(View):
    """View to select embed vs plain text type."""

    def __init__(self, cog, config: dict, sticky_id: int = None, user_id: int = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.config = config
        self.sticky_id = sticky_id
        self.user_id = user_id

    @discord.ui.button(label="📄 Embed Message", style=discord.ButtonStyle.primary, row=0)
    async def embed_type(self, interaction: discord.Interaction, button: Button):
        modal = CreateEmbedModal(self.cog, self.config, self.sticky_id, self.sticky_id is not None)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="💬 Plain Text", style=discord.ButtonStyle.secondary, row=0)
    async def plain_type(self, interaction: discord.Interaction, button: Button):
        modal = CreatePlainModal(self.cog, self.config, self.sticky_id, self.sticky_id is not None)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⬅️ Back to Manager", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: Button):
        view = StickyManagePanel(self.cog, self.user_id, interaction.guild)
        await view.populate()
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


# ─────────────────────────────────────────────
# UI — Settings Modal
# ─────────────────────────────────────────────

class SettingsModal(Modal):
    def __init__(self, cog, sticky_id: int, current_timeout: int, current_color: int):
        super().__init__(title="Sticky Settings", timeout=120)
        self.cog = cog
        self.sticky_id = sticky_id

        self.timeout_input = TextInput(
            label="Idle Timeout (seconds)",
            placeholder="180",
            default=str(current_timeout),
            required=True,
            max_length=5
        )
        self.add_item(self.timeout_input)

        default_color_hex = f"#{current_color:06x}" if isinstance(current_color, int) else "#3498db"
        self.color_input = TextInput(
            label="Embed Color (hex)",
            placeholder="#3498db",
            default=default_color_hex,
            required=False,
            max_length=7
        )
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            timeout = int(self.timeout_input.value)
            if timeout < 10:
                await interaction.response.send_message("❌ Timeout must be at least 10 seconds.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("❌ Invalid timeout value. Must be a number.", ephemeral=True)
            return

        updates = {'idle_timeout': timeout}

        # Parse color
        color_text = self.color_input.value.strip().lstrip('#')
        if color_text:
            try:
                color_val = int(color_text, 16)
                updates['color'] = color_val
            except ValueError:
                pass  # Keep existing color

        await StickyManager.update_sticky(self.sticky_id, **updates)
        await self.cog._start_sticky_timer(self.sticky_id)

        await interaction.response.send_message(
            f"✅ Settings updated! Timeout: {timeout}s, Color: #{updates.get('color', 'unchanged')}",
            ephemeral=True,
            delete_after=5
        )


# ─────────────────────────────────────────────
# Main Cog
# ─────────────────────────────────────────────

class StickyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        await init_db()

    def cog_unload(self):
        """Cancel all sticky timer tasks when cog is unloaded."""
        for task in self.active_tasks.values():
            task.cancel()
        self.active_tasks.clear()

    async def _cancel_sticky_timer(self, sticky_id: int):
        """Cancel the idle timer for a sticky."""
        task = self.active_tasks.pop(sticky_id, None)
        if task:
            task.cancel()

    async def _start_sticky_timer(self, sticky_id: int):
        """Start (or restart) the idle timer for a sticky."""
        await self._cancel_sticky_timer(sticky_id)

        sticky = await StickyManager.get_sticky(sticky_id)
        if not sticky:
            return
        if sticky.get('is_paused'):
            return

        timeout = sticky.get('idle_timeout', 180)
        last_msg_time = sticky.get('last_message_time', 0)
        now = time.time()

        elapsed = now - last_msg_time if last_msg_time > 0 else timeout
        remaining = max(0, timeout - elapsed)

        task = asyncio.create_task(self._idle_timeout_task(sticky_id, remaining))
        self.active_tasks[sticky_id] = task

    async def _idle_timeout_task(self, sticky_id: int, delay: float):
        """Wait for the delay, then post the sticky message."""
        try:
            await asyncio.sleep(delay)
            await self._post_sticky(sticky_id)
        except asyncio.CancelledError:
            pass

    async def _post_sticky(self, sticky_id: int):
        """Post/update a sticky message in its channel."""
        sticky = await StickyManager.get_sticky(sticky_id)
        if not sticky or sticky.get('is_paused'):
            return

        guild = self.bot.get_guild(sticky['guild_id'])
        if not guild:
            return
        channel = guild.get_channel(sticky['channel_id'])
        if not channel:
            return

        # Delete old sticky message if it exists
        if sticky.get('sticky_message_id'):
            try:
                old_msg = await channel.fetch_message(sticky['sticky_message_id'])
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        # Send new sticky
        try:
            if sticky.get('is_embed'):
                embed = StickyManager.build_embed(sticky)
                message = await channel.send(embed=embed)
            else:
                message = await channel.send(content=sticky.get('plain_text') or "(empty)")

            await StickyManager.set_state(sticky_id, sticky_message_id=message.id)
            logger.info(f"Sticky #{sticky_id} posted in #{channel.name} (ID: {message.id})")
        except discord.Forbidden:
            logger.warning(f"Cannot post sticky #{sticky_id} in #{channel.name} — no permission")
        except Exception as e:
            logger.error(f"Failed to post sticky #{sticky_id}: {e}", exc_info=True)

    # ── Slash Commands ──

    @app_commands.command(name="sticky", description="Manage sticky messages in your server")
    @app_commands.default_permissions(manage_messages=True)
    async def sticky_manage(self, interaction: discord.Interaction):
        """Open the sticky management panel."""
        view = StickyManagePanel(self, interaction.user.id, interaction.guild)
        await view.populate()
        embed = await view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── Listeners ──

    @commands.Cog.listener()
    async def on_ready(self):
        """On bot ready, load all active stickies and schedule their timers."""
        logger.info("StickyCog loading stickies from database...")
        for guild in self.bot.guilds:
            stickies = await StickyManager.get_guild_stickies(guild.id)
            for sticky in stickies:
                if sticky.get('is_paused'):
                    continue
                sticky_id = sticky['id']

                # Delete any leftover sticky message from previous session
                if sticky.get('sticky_message_id'):
                    channel = guild.get_channel(sticky['channel_id'])
                    if channel:
                        try:
                            old_msg = await channel.fetch_message(sticky['sticky_message_id'])
                            await old_msg.delete()
                            logger.debug(f"Deleted leftover sticky #{sticky_id} message in #{channel.name}")
                        except (discord.NotFound, discord.Forbidden):
                            pass
                    await StickyManager.set_state(sticky_id, sticky_message_id=None)

                # Calculate elapsed time since last user message
                last_msg_time = sticky.get('last_message_time', 0)
                now = time.time()
                timeout = sticky.get('idle_timeout', 180)
                elapsed = now - last_msg_time if last_msg_time > 0 else timeout

                if elapsed >= timeout:
                    logger.info(f"Sticky #{sticky_id} overdue — posting now")
                    await self._post_sticky(sticky_id)
                else:
                    remaining = timeout - elapsed
                    logger.info(f"Sticky #{sticky_id} will post in {remaining:.0f}s")
                    task = asyncio.create_task(self._idle_timeout_task(sticky_id, remaining))
                    self.active_tasks[sticky_id] = task

        logger.info("✅ StickyCog ready")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Watch for messages in channels that have active stickies."""
        if message.author.bot:
            return
        if not message.guild:
            return

        stickies = await StickyManager.get_stickies_for_channel(message.channel.id)
        if not stickies:
            return

        now = time.time()
        for sticky in stickies:
            sticky_id = sticky['id']
            await StickyManager.set_state(sticky_id,
                                    last_message_id=message.id,
                                    last_message_time=now)
            await self._start_sticky_timer(sticky_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(StickyCog(bot))