import asyncio
import io
import json
import os
import shutil
import aiosqlite
from datetime import datetime
from typing import Optional, Set, List
import discord
from discord.ext import commands, tasks
from discord.ui import (
    LayoutView, Container, TextDisplay, Separator, ActionRow,
    Button, Thumbnail, MediaGallery, Select, Section,
)
from settings import BASE_DIR, logger
from utility.wwm import get_club_chat, get_custom_guild_info, get_bulk_players_info, get_film_plan, get_teams_info
from utility.api_constants import get_kongfu_ids_from_player, format_kongfu_display
from googletrans import Translator


VERIFICATION_DB_PATH = BASE_DIR / "data" / "guild_verification.db"
AVATARS_DIR = BASE_DIR / "data" / "avatars"

# Channel where avatar-mapping approval requests are sent for admins to review
ADMIN_AVATAR_CHANNEL_ID = 1500005539256602774


# ── Components V2 view classes ──────────────────────────────────────
# Defined at module scope so the LiveChatCog below can reference them.

class ChatMessageView(LayoutView):
    """Components V2 message view for a normal (non-emote, non-artwork) chat message.

    Holds the author, body text (with translation), and footer timestamp.
    Optionally includes a Thumbnail of the head_id avatar when one exists locally,
    and a MediaGallery for msg_artwork_card image attachments.
    """

    def __init__(
        self,
        *,
        author_name: str,
        body_text: str,
        ts: int,
        discord_mention: str = "",
        head_id = None,
        head_avatar_path: Optional[str] = None,
        accent_color: int = 0x2ECC71,
        image_url: Optional[str] = None,
        image_files: Optional[List[discord.File]] = None,
    ):
        super().__init__(timeout=None)
        self._files: List[discord.File] = list(image_files) if image_files else []

        container_children: list = []

        if head_avatar_path:
            # Preserve original extension so animated .webp avatars stay animated
            thumb_ext = os.path.splitext(head_avatar_path)[1] or ".png"
            thumb_filename = f"head_{head_id}{thumb_ext}"
            self._files.append(discord.File(head_avatar_path, filename=thumb_filename))
            header_text = TextDisplay(f"**{author_name}**")
            section = Section(accessory=Thumbnail(media=f"attachment://{thumb_filename}"))
            section.add_item(header_text)
            container_children.append(section)
        else:
            container_children.append(TextDisplay(f"**{author_name}**"))

        container_children.append(TextDisplay(body_text))
        container_children.append(Separator(spacing=discord.SeparatorSpacing.small))
        footer = f"<t:{ts}:F> (<t:{ts}:R>)"
        if discord_mention:
            footer += f"\n{discord_mention}"
        container_children.append(TextDisplay(footer))

        if image_url:
            gallery = MediaGallery()
            gallery.add_item(media=image_url)
            container_children.append(gallery)

        container = Container(*container_children, accent_color=accent_color)
        self.add_item(container)

    def _resolve_files(self) -> List[discord.File]:
        return list(self._files)


class EmotionMessageView(LayoutView):
    """Components V2 view for an emote (msg_emotion) chat message.

    Shows the emotion PNG and the author/timestamp via a single Container.
    """

    def __init__(
        self,
        *,
        author_name: str,
        ts: int,
        discord_mention: str,
        emotion_id,
        emotion_path: str,
    ):
        super().__init__(timeout=None)
        self._files: List[discord.File] = [
            discord.File(emotion_path, filename=f"{emotion_id}.png")
        ]
        gallery = MediaGallery()
        gallery.add_item(media=f"attachment://{emotion_id}.png", description="Emote")

        footer = f"📅 <t:{ts}:F> (<t:{ts}:R>)"
        if discord_mention:
            footer += f"\n{discord_mention}"

        container = Container(
            TextDisplay(f"**{author_name}**"),
            gallery,
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(footer),
            accent_color=0x9B59B6,
        )
        self.add_item(container)

    def _resolve_files(self) -> List[discord.File]:
        return list(self._files)


class ExhibitionMessageView(LayoutView):
    """Components V2 view for an Exhibition (dance video) message."""

    def __init__(
        self,
        *,
        author_name: str,
        ts: int,
        discord_mention: str,
        video_name: str,
        video_url: str,
        video_msg: str,
        video_hot,
    ):
        super().__init__(timeout=None)
        body_lines = [f"🎬 **[Exhibition] [{video_name or 'Unknown'}]({video_url})**"]
        if video_msg:
            body_lines.append(video_msg)
        if video_hot:
            body_lines.append(f"❤️ {video_hot}")
        body_lines.append("")
        body_lines.append(f"📅 <t:{ts}:F> (<t:{ts}:R>)")
        if discord_mention:
            body_lines.append(discord_mention)
        container = Container(
            TextDisplay(f"**{author_name}**"),
            TextDisplay("\n".join(body_lines)),
            accent_color=0xE67E22,
        )
        self.add_item(container)


class HeadPickerRequestView(LayoutView):
    """Wraps a normal chat message and adds a "Set Avatar" button.

    Times out after 180 seconds, after which the button auto-disables.
    """

    PICKER_TIMEOUT = 180.0  # seconds

    def __init__(self, *, base_view: ChatMessageView, head_id, sender_nickname: str, sender_pid: Optional[str]):
        super().__init__(timeout=self.PICKER_TIMEOUT)
        self.base_view = base_view
        self.head_id = str(head_id) if head_id is not None else ""
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid

        # Carry over the base view's items (a single Container)
        for item in list(base_view.children):
            self.add_item(item)

        action_row = ActionRow()
        button = Button(
            label="🖼️ Set Avatar",
            style=discord.ButtonStyle.primary,
            custom_id=f"head_picker_open:{self.head_id}",
        )
        button.callback = self._on_click
        action_row.add_item(button)
        self.add_item(action_row)

        # Carry files across so the avatar thumbnail keeps working
        self._files: List[discord.File] = list(getattr(base_view, "_files", []))

    def _resolve_files(self) -> List[discord.File]:
        return list(self._files)

    async def _on_click(self, interaction: discord.Interaction):
        try:
            # Lazy imports to avoid circular references at module load
            from cogs.live_chat_cog import LiveChatCog  # noqa: F401
        except Exception:
            pass

        # Resolve the cog instance from the bot
        cog = interaction.client.get_cog("LiveChatCog")
        if cog is None:
            await interaction.response.send_message("❌ LiveChatCog is not loaded.", ephemeral=True)
            return

        # Mark this head_id as handled so we don't re-prompt on later messages.
        # We do this when the user clicks (not when the view is first created) so
        # that if the button times out without interaction, the next message from
        # this sender still gets a fresh "Set Avatar" button.
        if self.head_id and cog:
            cog.seen_head_ids.add(self.head_id)
            cog.save_config()

        avatar_files = cog._list_avatar_files(force_refresh=True)
        if not avatar_files:
            await interaction.response.send_message(
                "❌ No avatar PNGs are available in `data/avatars/`. "
                "Add at least one PNG to that folder and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        view = AvatarPickerView(
            cog=cog,
            head_id=self.head_id,
            avatar_files=avatar_files,
            suggested_by=interaction.user,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
        )
        files = view._resolve_files()
        await interaction.followup.send(
            content=None,
            view=view,
            files=files,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        # Remove the "Set Avatar" button row entirely (rather than just
        # disabling the button) so the live message stops showing a dead
        # control. If we know which channel message this view was attached
        # to, push the change to Discord so users see the button disappear.
        rows_to_remove = [
            child for child in self.children
            if isinstance(child, ActionRow)
            and any(isinstance(item, Button) and item.custom_id == f"head_picker_open:{self.head_id}"
                   for item in child.children)
        ]
        for row in rows_to_remove:
            self.remove_item(row)

        live_msg = getattr(self, "message", None)
        if live_msg is not None:
            try:
                await live_msg.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass


class AvatarPickerView(LayoutView):
    """Ephemeral paginated picker that lets a user choose one of the avatar PNGs.

    Shows up to 9 avatars per page (up to 3x3 grid, Discord's MediaGallery limit) plus pagination + a Select menu.
    """

    ITEMS_PER_PAGE = 9

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        head_id: str,
        avatar_files: List[str],
        suggested_by: discord.abc.User,
        sender_nickname: str,
        sender_pid: Optional[str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.head_id = head_id
        self.avatar_files = list(avatar_files)
        self.suggested_by = suggested_by
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid
        self.page = 0
        self._files: List[discord.File] = []
        self._build()

    def _resolve_files(self) -> List[discord.File]:
        return list(self._files)

    def _page_slice(self) -> List[str]:
        start = self.page * self.ITEMS_PER_PAGE
        end = start + self.ITEMS_PER_PAGE
        return self.avatar_files[start:end]

    def _build(self) -> None:
        self.clear_items()
        self._files = []

        page_files = self._page_slice()
        total_pages = max(1, -(-len(self.avatar_files) // self.ITEMS_PER_PAGE))

        inner: list = []
        inner.append(
            TextDisplay(
                f"# 🖼️ Pick an avatar for head_id `{self.head_id}`\n"
                f"Page **{self.page + 1}** / **{total_pages}** "
                f"({len(self.avatar_files)} avatars available)\n"
                "Use the dropdown to choose, then it will be sent to admins for confirmation."
            )
        )
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Build a media gallery with the current page of avatars
        if page_files:
            gallery = MediaGallery()
            for filename in page_files:
                disk_path = AVATARS_DIR / filename
                if not disk_path.exists():
                    continue
                attach_name = f"picker_{self.page}_{filename}"
                # Re-attach as a fresh file each build
                self._files.append(discord.File(str(disk_path), filename=attach_name))
                gallery.add_item(
                    media=f"attachment://{attach_name}",
                    description=filename[:80],
                )
            inner.append(gallery)

            # Build a Select with one option per avatar on this page
            select_options = [
                discord.SelectOption(
                    label=filename[:95],
                    description=f"Map head_id {self.head_id} → {filename}"[:95],
                    value=filename,
                )
                for filename in page_files
            ]
            select_row = ActionRow()
            picker_select = Select(
                placeholder="Choose an avatar for this head_id…",
                options=select_options,
                custom_id=f"avatar_picker_select:{self.head_id}",
            )
            picker_select.callback = self._on_select
            select_row.add_item(picker_select)
            inner.append(select_row)
        else:
            inner.append(TextDisplay("No avatars to display on this page."))

        # "Go to page" dropdown row
        go_row = ActionRow()
        if total_pages > 1:
            go_options = [
                discord.SelectOption(
                    label=f"Page {i + 1}",
                    description=f"Go to page {i + 1} of {total_pages}",
                    value=str(i + 1),
                    default=(i == self.page),
                )
                for i in range(total_pages)
            ]
            go_select = Select(
                placeholder="Go to page…",
                options=go_options,
                custom_id="avatar_picker_goto",
            )
            go_select.callback = self._on_goto
            go_row.add_item(go_select)
            inner.append(go_row)

        # Pagination row
        nav_row = ActionRow()
        prev_btn = Button(
            label="⬅ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="avatar_picker_prev",
            disabled=self.page <= 0,
        )
        prev_btn.callback = self._on_prev
        nav_row.add_item(prev_btn)

        next_btn = Button(
            label="Next ➡",
            style=discord.ButtonStyle.secondary,
            custom_id="avatar_picker_next",
            disabled=self.page >= total_pages - 1,
        )
        next_btn.callback = self._on_next
        nav_row.add_item(next_btn)

        close_btn = Button(
            label="Close",
            style=discord.ButtonStyle.danger,
            custom_id="avatar_picker_close",
        )
        close_btn.callback = self._on_close
        nav_row.add_item(close_btn)
        inner.append(nav_row)

        container = Container(*inner, accent_color=0x3498DB)
        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction):
        chosen = interaction.data.get("values", [None])[0]
        if not chosen:
            await interaction.response.send_message("❌ No avatar selected.", ephemeral=True)
            return
        # Disable the picker after a selection to prevent double-sends
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True
        await interaction.response.edit_message(view=self)

        # Record this head_id as "handled" so we don't re-prompt on later messages
        if self.head_id:
            self.cog.seen_head_ids.add(self.head_id)
            self.cog.save_config()

        jump_url = None
        if interaction.message and interaction.message.reference is not None:
            try:
                jump_url = interaction.message.reference.jump_url
            except Exception:
                jump_url = None

        await self.cog._send_admin_avatar_approval(
            head_id=self.head_id,
            chosen_filename=chosen,
            suggested_by=self.suggested_by,
            source_message_jump_url=jump_url,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
        )

        try:
            await interaction.followup.send(
                f"✅ Sent **{chosen}** to admins for approval for head_id `{self.head_id}`.",
                ephemeral=True,
            )
        except Exception:
            pass

    async def _on_prev(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self._build()
        # Re-upload the new page's PNGs so the rebuilt MediaGallery's
        # `attachment://picker_<page>_<file>.png` URLs resolve.
        # `InteractionResponse.edit_message` uses `attachments=` (not `files=`)
        # to pass new uploads.
        await interaction.response.edit_message(
            view=self,
            attachments=self._files,
        )

    async def _on_next(self, interaction: discord.Interaction):
        total_pages = max(1, -(-len(self.avatar_files) // self.ITEMS_PER_PAGE))
        if self.page < total_pages - 1:
            self.page += 1
            self._build()
        # See note in _on_prev: use response.edit_message and pass the
        # new page's files via `attachments=`.
        await interaction.response.edit_message(
            view=self,
            attachments=self._files,
        )

    async def _on_goto(self, interaction: discord.Interaction):
        """Handle the 'Go to page' select menu."""
        page_str = interaction.data.get("values", [None])[0]
        if page_str is None:
            return
        try:
            target = int(page_str) - 1  # values are 1-based
            total_pages = max(1, -(-len(self.avatar_files) // self.ITEMS_PER_PAGE))
            if 0 <= target < total_pages and target != self.page:
                self.page = target
                self._build()
                await interaction.response.edit_message(
                    view=self,
                    attachments=self._files,
                )
                return
        except (ValueError, TypeError):
            pass
        # If we couldn't navigate, just acknowledge the interaction
        await interaction.response.edit_message(view=self)

    async def _on_close(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True
        # Don't clear attachments — the view isn't rebuilt, so the existing
        # `attachment://picker_<page>_<file>.png` references are still valid.
        # Use a LayoutView + Container + TextDisplay (not embed= or content=)
        # because the original picker was sent with a LayoutView (Components V2)
        # and Discord rejects `embed=` / `content=` on IS_COMPONENTS_V2 messages.
        close_container = Container(
            TextDisplay("Picker closed."),
            accent_color=0x3498DB,
        )
        close_view = LayoutView(timeout=None)
        close_view.add_item(close_container)
        await interaction.response.edit_message(
            view=close_view,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True
        self.stop()


class AdminConfirmView(LayoutView):
    """Persistent Components V2 view that admins use to approve or reject a proposed avatar.

    The actual `approve` / `reject` buttons live in `self.action_row` so the parent
    LayoutView can place them as a separate top-level item in the admin message.
    """

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        head_id: str,
        head_target_filename: str,
        chosen_filename: str,
        suggested_by_id: Optional[int],
        source_message_jump_url: Optional[str],
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.head_id = head_id
        self.head_target_filename = head_target_filename
        self.chosen_filename = chosen_filename
        self.suggested_by_id = suggested_by_id
        self.source_message_jump_url = source_message_jump_url

        self.action_row = ActionRow()
        approve_btn = Button(
            label="✅ Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"avatar_admin_approve:{head_id}:{chosen_filename}",
        )
        approve_btn.callback = self._on_approve
        self.action_row.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"avatar_admin_reject:{head_id}",
        )
        reject_btn.callback = self._on_reject
        self.action_row.add_item(reject_btn)

    def _disable(self) -> None:
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    async def _on_approve(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        source_path = AVATARS_DIR / self.chosen_filename
        mapped_dir = AVATARS_DIR / "mapped"
        os.makedirs(str(mapped_dir), exist_ok=True)
        target_path = mapped_dir / self.head_target_filename
        try:
            if source_path.resolve() != target_path.resolve():
                # Copy the file contents to the mapped subfolder. shutil.copy2
                # only needs read access to the source, which is nearly always
                # granted even when Explorer/antivirus has a lock on Windows.
                shutil.copy2(str(source_path), str(target_path))
                # Write an empty .done_ marker next to the source so the picker
                # skips it. This works without needing to rename/delete the
                # locked source file.
                done_marker = source_path.with_name(f".done_{source_path.name}")
                try:
                    done_marker.touch(exist_ok=True)
                except OSError:
                    pass
            # Refresh the in-memory cache so the new file is recognized
            self.cog._avatar_files_cache = None
        except Exception as e:
            logger.error(f"Failed to copy avatar for head_id {self.head_id}: {e}")
            self._disable()
            # Use a LayoutView + Container + TextDisplay instead of `content=` or
            # `embed=` — the original message was sent with a LayoutView (Components
            # V2), and the Discord API rejects both `content` and `embed` fields on
            # messages with the IS_COMPONENTS_V2 flag set.
            err_container = Container(
                TextDisplay(
                    f"❌ Failed to copy `{self.chosen_filename}` → "
                    f"`{self.head_target_filename}`: {e}"
                ),
                accent_color=0xE74C3C,
            )
            err_view = LayoutView(timeout=None)
            err_view.add_item(err_container)
            await interaction.edit_original_response(
                view=err_view,
                attachments=[],
            )
            return

        # Mark the head_id as mapped so we won't re-prompt
        if self.head_id:
            self.cog.seen_head_ids.add(self.head_id)
            self.cog.save_config()

        # Edit the original admin message to mark the approval, drop the
        # now-stale proposed-avatar attachment, and remove the buttons entirely.
        # Use a LayoutView + Container + TextDisplay (not embed= or content=) to
        # stay compatible with Components V2.
        try:
            ok_container = Container(
                TextDisplay(
                    f"✅ **Approved** by {interaction.user.mention}\n"
                    f"head_id `{self.head_id}` → `data/avatars/{self.head_target_filename}`"
                ),
                accent_color=0x2ECC71,
            )
            ok_view = LayoutView(timeout=None)
            ok_view.add_item(ok_container)
            await interaction.edit_original_response(
                view=ok_view,
                attachments=[],
            )
        except Exception:
            pass

        logger.info(
            f"✅ Avatar approved: head_id={self.head_id} → {self.head_target_filename} "
            f"(renamed from {self.chosen_filename}, by {interaction.user})"
        )

    async def _on_reject(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        # Edit the original admin message to mark the rejection, drop the
        # proposed-avatar attachment, and remove the buttons entirely.
        # Use a LayoutView + Container + TextDisplay (not embed= or content=) to
        # stay compatible with Components V2.
        try:
            reject_container = Container(
                TextDisplay(
                    f"❌ **Rejected** by {interaction.user.mention}\n"
                    f"head_id `{self.head_id}` mapping to `{self.chosen_filename}` was not applied."
                ),
                accent_color=0xE74C3C,
            )
            reject_view = LayoutView(timeout=None)
            reject_view.add_item(reject_container)
            await interaction.edit_original_response(
                view=reject_view,
                attachments=[],
            )
        except Exception:
            pass
        logger.info(
            f"❌ Avatar rejected: head_id={self.head_id}, file={self.chosen_filename} "
            f"(by {interaction.user})"
        )


class LiveChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_seen_msg_ids: Set[str] = set()
        self._last_seen_npc_ts = 0.0
        self._last_seen_max_ts = 0.0
        self.is_running = False
        self.translator = Translator()
        self._translate_retry_delay = 2.0
        self._translate_max_retries = 2
        self.pid_to_discord_user: dict[str, int] = {}  # player_pid -> discord user_id
        # head_ids we've already prompted the user to map (so we only attach
        # the "Set Avatar" button once per unknown head_id, per bot lifetime).
        self.seen_head_ids: Set[str] = set()
        # Cached list of avatar filenames for the picker UI.
        self._avatar_files_cache: Optional[List[str]] = None
        # Configuration
        self.CONFIG_FILE = "data/live_chat_config.json"
        self.CLUB_ID = "aRvTyiPA8WMSXrRj"      # Your guild ID
        self.HOSTNUM = 10103                    # Your server hostnum
        self.CHANNEL_ID = None                  # Set via /chatenable command
        self.POLL_INTERVAL = 10                 # Seconds between checks
        self.ranks = None                       # To store rank information
        # Team-up alert configuration
        self.TEAMUP_CHANNEL_ID = 1442853064053756028  # General channel for teamup pings
        self.TEAMUP_ROLE_ID = 1470861369107681587     # Team Up role
        self.TEAMUP_KEYWORD = "@teamup"               # Trigger keyword
        self.TEAMUP_EMBED_COLOR = 0xE74C3C            # Red

        # Ensure data directory exists
        os.makedirs("data", exist_ok=True)
        
        # Load saved configuration
        self.load_config()
        
        # Start the avatar cleanup loop (always runs, even if chat is disabled)
        self.avatar_cleanup.start()

        # Only start poller if already enabled in config
        if self.is_running and self.CHANNEL_ID:
            # Reset running flag so first poll processes new messages (with persisted timestamps)
            self.is_running = False
            self.chat_poller.start()
            logger.debug(f"✅ Live chat auto-started. Last max ts: {self._last_seen_max_ts}")

    def cog_unload(self):
        self.chat_poller.cancel()
        self.avatar_cleanup.cancel()
        self.save_config()

    @tasks.loop(seconds=30)
    async def avatar_cleanup(self):
        """Periodically try to remove source files that have .done_ markers."""
        await self._cleanup_done_markers()

    @tasks.loop(seconds=10)
    async def chat_poller(self):
        self._poll_counter = getattr(self, '_poll_counter', 0) + 1
        if not self.bot.is_ready():
            return
            
        try:
            # Fetch chat data in thread pool (avoid blocking event loop)
            chat_result = await asyncio.to_thread(get_club_chat, self.CLUB_ID, self.HOSTNUM)
            
            if not chat_result or 'result' not in chat_result or 'chat' not in chat_result['result']:
                logger.debug("No chat data received")
                return
                
            chat_messages = chat_result['result']['chat']['chat_history']
            
            # Track the max timestamps for dedup, initialized from persisted values
            max_npc_ts = self._last_seen_npc_ts
            max_all_ts = self._last_seen_max_ts
            new_messages = []  # Will hold any new messages found
            
            # On first run, catch up on ALL messages missed during downtime
            if not self.is_running:
                self.is_running = True
                new_messages = []
                
                # If we have saved state, diff against it to catch missed messages
                if self.last_seen_msg_ids or self._last_seen_npc_ts > 0:
                    for msg in chat_messages:
                        msg_id = msg.get('msg_id')
                        msg_ts = msg.get('ts', 0)
                        # Skip messages older than our maximum seen timestamp (secondary dedup)
                        if msg_ts <= max_all_ts:
                            if msg_id:
                                self.last_seen_msg_ids.add(msg_id)
                            continue
                        if msg_id:
                            if msg_id not in self.last_seen_msg_ids:
                                new_messages.append(msg)
                                self.last_seen_msg_ids.add(msg_id)
                        else:
                            # NPC message — track by timestamp
                            if msg_ts > max_npc_ts:
                                new_messages.append(msg)
                                if msg_ts > max_npc_ts:
                                    max_npc_ts = msg_ts
                    
                    if new_messages:
                        logger.info(f"↩️ Caught up {len(new_messages)} missed messages from downtime")
                    else:
                        logger.debug(f"✅ Live chat monitoring resumed. No missed messages.")
                else:
                    # Fresh start (no saved state) — just record current state, don't process
                    self.last_seen_msg_ids = {msg['msg_id'] for msg in chat_messages if 'msg_id' in msg}
                    max_npc_ts = max((msg.get('ts', 0) for msg in chat_messages if 'msg_id' not in msg), default=0)
                    max_all_ts = max((msg.get('ts', 0) for msg in chat_messages), default=0)
                    logger.debug(f"✅ Live chat monitoring started. Tracking {len(self.last_seen_msg_ids)} existing messages.")
                
                self._last_seen_npc_ts = max_npc_ts
                
                # If fresh start with no history, skip processing
                if not new_messages and not self.last_seen_msg_ids:
                    return
                # If no new messages to process, return
                if not new_messages and self.last_seen_msg_ids:
                    return
            
            # Regular polling: find new messages (skipped if first run already found some)
            if not new_messages:
                new_messages = []
                for msg in chat_messages:
                    msg_id = msg.get('msg_id')
                    msg_ts = msg.get('ts', 0)
                    if msg_id:
                        if msg_id in self.last_seen_msg_ids:
                            continue
                        new_messages.append(msg)
                        self.last_seen_msg_ids.add(msg_id)
                    else:
                        # NPC system messages (no msg_id) — track by timestamp
                        if msg_ts > max_npc_ts:
                            new_messages.append(msg)
                            if msg_ts > max_npc_ts:
                                max_npc_ts = msg_ts
                
                # Update the persisted NPC timestamp
                if max_npc_ts > self._last_seen_npc_ts:
                    self._last_seen_npc_ts = max_npc_ts
            
            # Update the universal max timestamp from all messages in this poll cycle
            if chat_messages:
                cycle_max = max(msg.get('ts', 0) for msg in chat_messages)
                if cycle_max > self._last_seen_max_ts:
                    self._last_seen_max_ts = cycle_max

            if new_messages:
                logger.debug(f"🔔 Found {len(new_messages)} new chat messages")

                # Call guild api so that we can get rank of sender and other info that might not be included in chat message data
                self.ranks = await asyncio.to_thread(get_custom_guild_info, self.CLUB_ID, self.HOSTNUM, {'members': ['custom_posts']})
                self.ranks = self.ranks.get('result', {}).get('members', {}).get('custom_posts', {}) if self.ranks else {}
                #logger.info(f"Ranks data: {self.ranks}")
                # Load PID -> Discord user mapping from guild verification database
                await self._load_verified_mapping()
                # Sort messages by timestamp (oldest first)
                new_messages.sort(key=lambda x: x.get('ts', 0))
                
                # Process system NPC messages first (Breaking Army timing, etc.)
                for msg in new_messages:
                    ext = msg.get('ext', {})
                    npc_msg_no = ext.get('npc_msg_no')
                    if npc_msg_no == 1082:
                        npc_args = ext.get('npc_msg_args', [])
                        if len(npc_args) >= 2:
                            nickname = str(npc_args[0])
                            seconds = float(npc_args[1])
                            timestamp = msg.get('ts', 0)
                            self.bot.dispatch('breaking_army_timing', nickname, seconds, timestamp)
                
                # Post to Discord
                channel = self.bot.get_channel(self.CHANNEL_ID)
                teamup_channel = self.bot.get_channel(self.TEAMUP_CHANNEL_ID)
                if channel:
                    for msg in new_messages:
                        # Build & post a Components V2 view for this message.
                        # Handles emotion, exhibition, normal messages, the head_id
                        # avatar picker, and the @teamup keyword alert.
                        await self._post_v2_for_message(channel, msg, teamup_channel)

                # Keep only last 200 message IDs to prevent memory leak
                if len(self.last_seen_msg_ids) > 500:
                    self.last_seen_msg_ids = set(list(self.last_seen_msg_ids)[-300:])
        
        except Exception as e:
            logger.error(f"Error in chat poller: {str(e)}", exc_info=True)
        
        # Save config every 10 seconds to persist tracking state (runs even if no new messages)
        self.save_config()

    async def translate_with_retry(self, text: str, src: str, dest: str) -> str:
        """Translate with automatic retry on failure."""
        last_error = None
        for attempt in range(self._translate_max_retries + 1):
            try:
                translation = await self.translator.translate(text, src=src, dest=dest)
                return translation.text
            except Exception as e:
                last_error = e
                if attempt < self._translate_max_retries:
                    logger.debug(f"Translation retry {attempt + 1}/{self._translate_max_retries} after error: {e}")
                    await asyncio.sleep(self._translate_retry_delay)
                else:
                    logger.error(f"Failed to translate message: {last_error}")
                    return None

    async def format_message_embed(self, msg: dict) -> discord.Embed:
        """Format chat message into Discord embed"""
        ts = int(msg.get('ts', 0))
        nickname = msg.get('nickname', 'Unknown')
        level = msg.get('level', 0)
        ext = msg.get('ext', {})
        head_id = msg.get('head_id', None)
        msg_type = ext.get('msg_type', 'msg_normal')
        sender_pid = msg.get('from_pid', None)
        
        # Determine sender's rank if possible
        rank_name = "Unknown"
        if sender_pid:
            # Get all ranks for sender PID
            sender_ranks = []
            for rank_id, rank_info in self.ranks.items():
                if sender_pid in rank_info.get('pids', []):
                    sender_ranks.append((rank_id, rank_info.get('name', 'Unknown')))

            if sender_ranks:
                sender_ranks.sort(key=lambda x: int(x[0]), reverse=False)  # Sort by rank ID ascending (assuming lower ID = higher rank)
                # Include some custom ranks like 1 = Guild Leader, 2 = Vice Leader,etc
                custom_rank_names = {
                    1: "Guild Leader",
                    2: "Vice Leader",
                    5: "Command",
                    7: "Half Time Performer"
                }
                # Get the highest rank (lowest ID) and use custom name if available
                highest_rank_id, highest_rank_name = sender_ranks[0]
                rank_name = custom_rank_names.get(highest_rank_id, highest_rank_name)
        
        # Determine message content based on type
        message = msg.get('msg', '').strip()
        picture_url = None
        video_url = None
        
        # Handle NPC system messages (Breaking Army timing, etc.)
        npc_msg_no = ext.get('npc_msg_no')
        if npc_msg_no and not message:
            if npc_msg_no == 1082:
                npc_args = ext.get('npc_msg_args', [])
                if len(npc_args) >= 2:
                    player_name = str(npc_args[0])
                    seconds = float(npc_args[1])
                    minutes_val = int(seconds) // 60
                    secs_val = int(seconds) % 60
                    if minutes_val > 0:
                        time_str = f"{minutes_val}m {secs_val}s"
                    else:
                        time_str = f"{secs_val}s"
                    message = f"⚔️ **Breaking Army** — **{player_name}** cleared in `{time_str}`"
                    # Override color to the BA purple color
                    ba_color = 0xBB8FCE
                    embed = discord.Embed(description=f"{message}", color=ba_color)
                    embed.set_author(name="Guild Steward")
                    return embed
            else:
                message = f"[System Message #{npc_msg_no}]"

        if not message:
            # Handle msg_common_share with empty text (e.g. activity cards, team invites)
            share_text = ext.get('share_text_info') or ext.get('extra_data', {}).get('share_text_info')
            if share_text:
                message = "[Shared] " + ", ".join(share_text)
        elif msg_type == 'msg_share_position' and message == "Share Location":
            # Replace generic location text with actual region name
            region_name = ext.get('region_name', '')
            if region_name:
                # Strip color tags like #G[Co-op]#E from the region name
                import re
                region_name = re.sub(r'#[A-Z](\[.*?\])?#E?', '', region_name)
                message = f"[Location] {region_name}"
        elif msg_type == 'msg_stuff' and message == "Item Share Message":
            # Show item number instead of generic text
            stuff_item = ext.get('stuff_item', {})
            item_no = stuff_item.get('No', '')
            if item_no:
                message = f"[Item] #{item_no}"
        elif msg_type == 'msg_hongbao':
            hongbao = msg.get('hongbao_info', {})
            if message:
                message = f"[Red Envelope] {message}"
            else:
                message = "[Red Envelope]"
            reward_no = hongbao.get('reward_no', '')
            if reward_no:
                message += f" ({reward_no} coins)"
        elif msg_type == 'msg_artwork_card':
            # Display gallery artwork image
            artwork_data = ext.get('extra_data', {}).get('artwork_data', {})
            picture_url = artwork_data.get('picture_url', '')
            artwork_name = artwork_data.get('name', 'Gallery')
            heat_val = artwork_data.get('heat_val', 0)
            plan_id = artwork_data.get('plan_id', '')
            message_type_label = msg.get('msg', '').strip()
            
            # Check if this is an Exhibition (dance video)
            if message_type_label == "[Exhibition]" and plan_id:
                # Fetch video details via film plan API
                film_data = await asyncio.to_thread(get_film_plan, plan_id)
                if film_data and 'result' in film_data:
                    video_url = film_data['result'].get('video_url', '')
                    video_name = film_data['result'].get('name', artwork_name)
                    video_msg = film_data['result'].get('msg', '')
                    video_hot = film_data['result'].get('hot', heat_val)
                    
                    message = f"[Exhibition] {video_name}"
                    if video_msg:
                        message += f"\n{video_msg}"
                    if video_hot:
                        message += f" | ❤️ {video_hot}"
                else:
                    message = f"[Exhibition] {artwork_name}"
                    if heat_val:
                        message += f" | ❤️ {heat_val}"
            else:
                message = f"[Gallery] {artwork_name}"
                if heat_val:
                    message += f" | ❤️ {heat_val}"

        elif msg_type == 'msg_common_share': # sharing of team invite
            extra_data = ext.get('extra_data', {})
            team_id = extra_data.get('team_id')
            team_hostnum = extra_data.get('team_hostnum')
            if team_id and team_hostnum:
                # Fetch team details via teams info API
                team_data = await asyncio.to_thread(get_teams_info, team_hostnum, team_id)
                if team_data and 'result' in team_data and team_id in team_data['result']:
                    members_data = team_data['result'][team_id].get('members', {}).get('members', [])
                    
                    # Group member PIDs by hostnum for bulk lookup
                    hostnum_pids = {}
                    for member in members_data:
                        m_hostnum = member.get('hostnum')
                        m_pid = member.get('pid')
                        if m_hostnum and m_pid:
                            hostnum_pids.setdefault(m_hostnum, []).append(m_pid)
                    
                    # Fetch nickname, level and kongfu for all members
                    member_info = {}
                    for m_hostnum, pids in hostnum_pids.items():
                        bulk_result = await asyncio.to_thread(get_bulk_players_info, pids, ["base", "kongfu"], m_hostnum)
                        if bulk_result and 'result' in bulk_result:
                            for pid_key, player_data in bulk_result['result'].items():
                                base_info = player_data.get('base', {})
                                weapon_ids = get_kongfu_ids_from_player(player_data)
                                weapon_display = format_kongfu_display(weapon_ids) if weapon_ids else ""
                                member_info[pid_key] = {
                                    'nickname': base_info.get('nickname', 'Unknown'),
                                    'level': base_info.get('level', '?'),
                                    'weapons': weapon_display,
                                }
                    
                    # Build team objective text
                    share_text_info = ext.get('share_text_info') or extra_data.get('share_text_info', [])
                    recruit_info = ext.get('recruit_info', '')
                    objective_parts = []
                    if recruit_info:
                        objective_parts.append(f"[{recruit_info}]")
                    if share_text_info:
                        objective_parts.append(", ".join(share_text_info))
                    
                    # Build member list
                    member_lines = []
                    for idx, member in enumerate(members_data, 1):
                        m_pid = member.get('pid', '')
                        info = member_info.get(m_pid, {})
                        m_nickname = info.get('nickname', 'Unknown')
                        m_level = info.get('level', '?')
                        m_weapons = info.get('weapons', '')
                        line = f"{idx}. {m_nickname} (Lv.{m_level})"
                        if m_weapons:
                            line += f"\n   {m_weapons}"
                        member_lines.append(line)
                    
                    header = " ".join(objective_parts) if objective_parts else "Team Invitation"
                    message = f"[Team] {header}\n\n" + "\n".join(member_lines)
                else:
                    message = "[Team Invitation]"
            else:
                message = "[Team Invitation]"
        elif msg_type == 'msg_normal':
            # Translate englsih to chinese and vice versa for normal messages to make it more accessible for all users
            # Check if message contains Chinese characters
            if any('\u4e00' <= char <= '\u9fff' for char in message):
                # Contains Chinese characters, translate to English
                translated = await self.translate_with_retry(message, src='zh-cn', dest='en')
                if translated:
                    message += f"\n\n[Translated] {translated}"
            else:
                # No Chinese characters, translate to Chinese
                translated = await self.translate_with_retry(message, src='en', dest='zh-cn')
                if translated:
                    message += f"\n\n[Translated] {translated}"

        
        channel_type = msg.get('channel', 'club_chat')
        
        # Channel styling
        channel_colors = {
            "club_chat": 0x2ECC71,      # Green
            "officer_chat": 0xE67E22,   # Orange
            "private": 0x9B59B6         # Purple
        }
        
        # Check if sender has a bound Discord account
        discord_mention = self._get_discord_mention(sender_pid)
        desc = f"{message}\n\n<t:{ts}:F> (<t:{ts}:R>)"
        
        embed = discord.Embed(
            description=desc,
            color=channel_colors.get(channel_type, 0x3498DB)
        )
        
        embed.set_author(
            name=f"{nickname} ({rank_name}) (Lv.{level})" if rank_name != "Unknown" else f"{nickname} (Lv.{level})",
        )
        
        # Add picture if available but do not add if there's video
        if picture_url and not video_url:
            embed.set_image(url=picture_url)
        return embed

    async def send_teamup_alert(self, msg: dict, teamup_channel: discord.TextChannel):
        """Send @teamup alert to general channel with role ping"""
        if not teamup_channel:
            logger.warning("Team-up channel not found, cannot send alert")
            return

        ts = int(msg.get('ts', 0))
        nickname = msg.get('nickname', 'Unknown')
        level = msg.get('level', 0)
        sender_pid = msg.get('from_pid', None)
        raw_message = msg.get('msg', '').strip()

        # Determine sender's rank if possible (reuse self.ranks data)
        rank_name = "Unknown"
        if sender_pid and self.ranks:
            sender_ranks = []
            for rank_id, rank_info in self.ranks.items():
                if sender_pid in rank_info.get('pids', []):
                    sender_ranks.append((rank_id, rank_info.get('name', 'Unknown')))
            if sender_ranks:
                sender_ranks.sort(key=lambda x: int(x[0]), reverse=False)
                custom_rank_names = {
                    1: "Guild Leader",
                    2: "Vice Leader",
                    5: "Command",
                    7: "Half Time Performer"
                }
                highest_rank_id, highest_rank_name = sender_ranks[0]
                rank_name = custom_rank_names.get(highest_rank_id, highest_rank_name)

        # Fetch Number ID (long account ID) and team info using the PID
        number_id = None
        team_info_str = ""
        if sender_pid:
            try:
                sender_hostnum = msg.get('hostnum', 10595)
                bulk_data = await asyncio.to_thread(get_bulk_players_info, [sender_pid], ["base", "team"], sender_hostnum)
                if bulk_data and 'result' in bulk_data:
                    player_info = bulk_data['result'].get(str(sender_pid), {})
                    if player_info:
                        base_info = player_info.get('base', {})
                        number_id = base_info.get('number_id')
                        
                        # Check if sender is in a team
                        team_data = player_info.get('team', {})
                        team_id = team_data.get('team_id')
                        team_hostnum = team_data.get('hostnum')
                        
                        if team_id and team_hostnum:
                            team_result = await asyncio.to_thread(get_teams_info, team_hostnum, team_id)
                            if team_result and 'result' in team_result and team_id in team_result['result']:
                                members_data = team_result['result'][team_id].get('members', {}).get('members', [])
                                if members_data:
                                    # Group member PIDs by hostnum for bulk lookup
                                    hostnum_pids = {}
                                    for member in members_data:
                                        m_hostnum = member.get('hostnum')
                                        m_pid = member.get('pid')
                                        if m_hostnum and m_pid:
                                            hostnum_pids.setdefault(m_hostnum, []).append(m_pid)
                                    
                                    # Fetch nickname, level and kongfu for all members
                                    member_info = {}
                                    for m_hostnum, pids in hostnum_pids.items():
                                        bulk_result = await asyncio.to_thread(get_bulk_players_info, pids, ["base", "kongfu"], m_hostnum)
                                        if bulk_result and 'result' in bulk_result:
                                            for pid_key, player_data in bulk_result['result'].items():
                                                base_info = player_data.get('base', {})
                                                weapon_ids = get_kongfu_ids_from_player(player_data)
                                                weapon_display = format_kongfu_display(weapon_ids) if weapon_ids else ""
                                                member_info[pid_key] = {
                                                    'nickname': base_info.get('nickname', 'Unknown'),
                                                    'level': base_info.get('level', '?'),
                                                    'weapons': weapon_display,
                                                }
                                    
                                    # Build member list
                                    member_lines = []
                                    for idx, member in enumerate(members_data, 1):
                                        m_pid = member.get('pid', '')
                                        info = member_info.get(m_pid, {})
                                        line = f"{idx}. {info.get('nickname', 'Unknown')} (Lv.{info.get('level', '?')})"
                                        m_weapons = info.get('weapons', '')
                                        if m_weapons:
                                            line += f"\n   {m_weapons}"
                                        member_lines.append(line)
                                    
                                    team_info_str = "\n\n👥 **Current Team:**\n" + "\n".join(member_lines)
            except Exception as e:
                logger.error(f"Failed to fetch player/team info for PID {sender_pid}: {e}")

        # Translate the message (auto-detect)
        translated = None
        if any('\u4e00' <= char <= '\u9fff' for char in raw_message):
            # Contains Chinese -> translate to English
            translated = await self.translate_with_retry(raw_message, src='zh-cn', dest='en')
        else:
            # No Chinese -> translate to Chinese
            translated = await self.translate_with_retry(raw_message, src='en', dest='zh-cn')

        # Check if sender has a bound Discord account
        discord_mention = self._get_discord_mention(sender_pid)
        # Build embed description
        description = f"**{nickname}**"
        if rank_name != "Unknown":
            description += f" ({rank_name})"
        description += f" (Lv.{level})"
        if discord_mention:
            description += f" — {discord_mention}"
        if number_id:
            description += f" | ID: {number_id}"
        description += " is looking for a team!\n\n"
        description += f"*{raw_message}*"
        if translated:
            description += f"\n\n[Translated] {translated}"
        description += team_info_str
        description += f"\n\n<t:{ts}:F> (<t:{ts}:R>)"

        embed = discord.Embed(
            title="🔔 Team Up Request",
            description=description,
            color=self.TEAMUP_EMBED_COLOR
        )

        # Send role ping as a separate text message first, then embed
        await teamup_channel.send(f"<@&{self.TEAMUP_ROLE_ID}>")
        await teamup_channel.send(embed=embed)
        logger.info(f"📢 Team-up alert sent for {nickname} in #{teamup_channel.name}")

    def _get_discord_mention(self, sender_pid: str) -> str:
        """Return a Discord mention string if the sender PID is bound to a Discord user"""
        if sender_pid and sender_pid in self.pid_to_discord_user:
            return f"<@{self.pid_to_discord_user[sender_pid]}>"
        return ""

    @chat_poller.before_loop
    async def before_chat_poller(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="chatstatus", description="Check status of live guild chat monitor")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def chat_status(self, interaction: discord.Interaction):
        """Check status of live chat monitor"""
        status = "✅ Running" if self.chat_poller.is_running() else "❌ Stopped"
        await interaction.response.send_message(
            f"Live Chat Monitor Status: {status}\nTracked message IDs: {len(self.last_seen_msg_ids)}",
            ephemeral=True
        )

    @discord.app_commands.command(name="chatenable", description="Enable live guild chat monitoring")
    @discord.app_commands.checks.has_permissions(administrator=True)
    @discord.app_commands.describe(channel="Discord channel to post live chat messages")
    async def chat_enable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Enable live chat monitoring and set output channel"""
        self.CHANNEL_ID = channel.id
        self.is_running = True
        self.save_config()
        
        if not self.chat_poller.is_running():
            self.chat_poller.start()
            
        await interaction.response.send_message(
            f"✅ Live chat monitoring enabled. Messages will be posted to: {channel.mention}",
            ephemeral=True
        )

    @discord.app_commands.command(name="chatdisable", description="Disable live guild chat monitoring")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def chat_disable(self, interaction: discord.Interaction):
        """Disable live chat monitoring"""
        self.is_running = False
        self.save_config()
        
        if self.chat_poller.is_running():
            self.chat_poller.cancel()
            
        await interaction.response.send_message("❌ Live chat monitoring disabled", ephemeral=True)


    def load_config(self):
        """Load saved configuration from file"""
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.CHANNEL_ID = config.get('channel_id')
                    self.is_running = config.get('enabled', False)
                    self.last_seen_msg_ids = set(config.get('last_msg_ids', []))
                    self._last_seen_npc_ts = config.get('last_npc_ts', 0)
                    self._last_seen_max_ts = config.get('last_max_ts', 0)
                    # head_ids we've already prompted (so we don't keep re-attaching the picker button)
                    self.seen_head_ids = set(str(h) for h in config.get('seen_head_ids', []))
                logger.debug(f"Loaded live chat config: enabled={self.is_running}, channel={self.CHANNEL_ID}, last_max_ts={self._last_seen_max_ts}, seen_head_ids={len(self.seen_head_ids)}")
            except Exception as e:
                logger.error(f"Failed to load live chat config: {str(e)}")

    def save_config(self):
        """Save current configuration to file"""
        try:
            config = {
                'channel_id': self.CHANNEL_ID,
                'enabled': self.is_running,
                'last_msg_ids': list(self.last_seen_msg_ids)[-300:],
                'last_npc_ts': self._last_seen_npc_ts,
                'last_max_ts': self._last_seen_max_ts,
                'seen_head_ids': list(self.seen_head_ids),
            }
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=4)
            logger.debug("Saved live chat configuration")
        except Exception as e:
            logger.error(f"Failed to save live chat config: {str(e)}")

    async def _load_verified_mapping(self):
        """Load the player_pid -> discord_user_id mapping from guild_verification.db"""
        try:
            if not VERIFICATION_DB_PATH.exists():
                logger.debug("Verification DB not found, skipping PID mapping load")
                return
            async with aiosqlite.connect(str(VERIFICATION_DB_PATH)) as conn:
                cursor = await conn.execute("SELECT player_pid, user_id FROM verified_members WHERE player_pid IS NOT NULL")
                rows = await cursor.fetchall()
            self.pid_to_discord_user = {str(pid): uid for pid, uid in rows}
            if rows:
                logger.debug(f"Loaded {len(rows)} PID -> Discord user mappings from verification DB")
        except Exception as e:
            logger.error(f"Failed to load verified mapping: {e}")

    # ── Avatar / head_id helpers ──────────────────────────────────────
    # ── V2 message posting helper ──────────────────────────────────
    async def _post_v2_for_message(
        self,
        channel: discord.TextChannel,
        msg: dict,
        teamup_channel: Optional[discord.TextChannel],
    ) -> None:
        """Build and post a Components V2 view for one chat message.

        Also handles the head_id avatar picker (if the head_id has no
        local PNG yet) and the @teamup alert dispatch.
        """
        ext = msg.get("ext", {}) or {}
        msg_type = ext.get("msg_type", "msg_normal")
        msg_label = (msg.get("msg", "") or "").strip()
        ts = int(msg.get("ts", 0) or 0)
        nickname = msg.get("nickname", "Unknown")
        level = msg.get("level", 0) or 0
        sender_pid = msg.get("from_pid", None)
        head_id = msg.get("head_id", None)

        rank_name = self._get_rank_name(sender_pid)
        author_name = (
            f"{nickname} ({rank_name}) (Lv.{level})"
            if rank_name != "Unknown"
            else f"{nickname} (Lv.{level})"
        )
        discord_mention = self._get_discord_mention(sender_pid)
        head_avatar_path = self._avatar_path(head_id)
        channel_type = msg.get("channel", "club_chat")
        accent_color = {
            "club_chat": 0x2ECC71,
            "officer_chat": 0xE67E22,
            "private": 0x9B59B6,
        }.get(channel_type, 0x3498DB)

        view: Optional[LayoutView] = None
        files: List[discord.File] = []
        handled_separately = False  # True if emotion / exhibition took ownership

        # ── Emotion messages (custom emote PNGs) ──
        if msg_type == "msg_emotion":
            emotion_id = ext.get("emotion_id")
            if emotion_id:
                emotion_path = f"data/emotion/{emotion_id}.png"
                if os.path.exists(emotion_path):
                    view = EmotionMessageView(
                        author_name=author_name,
                        ts=ts,
                        discord_mention=discord_mention,
                        emotion_id=emotion_id,
                        emotion_path=emotion_path,
                    )
                    files = view._resolve_files()
                    handled_separately = True

        # ── Exhibition (dance video) messages ──
        if not handled_separately and msg_type == "msg_artwork_card" and msg_label == "[Exhibition]":
            artwork_data = ext.get("extra_data", {}).get("artwork_data", {}) or {}
            plan_id = artwork_data.get("plan_id", "") or ""
            if plan_id:
                film_data = await asyncio.to_thread(get_film_plan, plan_id)
                if film_data and "result" in film_data:
                    video_url = film_data["result"].get("video_url", "") or ""
                    if video_url:
                        view = ExhibitionMessageView(
                            author_name=author_name,
                            ts=ts,
                            discord_mention=discord_mention,
                            video_name=film_data["result"].get("name", "") or "",
                            video_url=video_url,
                            video_msg=film_data["result"].get("msg", "") or "",
                            video_hot=film_data["result"].get("hot", "") or "",
                        )
                        handled_separately = True

        # ── Default: normal / share / location / item / red envelope / etc. ──
        if not handled_separately and view is None:
            # Reuse the existing embed builder to get the body text via Embed.description
            embed = await self.format_message_embed(msg)
            body_text = (embed.description or "").strip()
            # Strip the timestamp line that `format_message_embed` appended
            # to the description — the ChatMessageView footer adds its own
            # timestamp, so we'd otherwise render it twice.
            import re as _re
            body_text = _re.sub(
                r"<t:\d+:[FRT]>(?:\s*\(?<t:\d+:[FRT]>?\))?",
                "",
                body_text,
            )
            # Collapse any blank lines left behind after stripping
            body_text = _re.sub(r"\n{2,}", "\n\n", body_text).strip()
            picture_url = None
            if msg_type == "msg_artwork_card":
                artwork_data = ext.get("extra_data", {}).get("artwork_data", {}) or {}
                picture_url = artwork_data.get("picture_url", "") or None
            view = ChatMessageView(
                author_name=author_name,
                body_text=body_text,
                ts=ts,
                discord_mention=discord_mention,
                head_id=str(head_id) if head_id is not None else None,
                head_avatar_path=head_avatar_path,
                accent_color=accent_color,
                image_url=picture_url,
            )
            files = view._resolve_files()

        if view is None:
            return

        # ── Offer head_id picker only for non-emote / non-exhibition messages ──
        if (
            not handled_separately
            and head_id is not None
            and head_avatar_path is None
            and self._should_offer_avatar_picker(head_id)
            and isinstance(view, ChatMessageView)
        ):
            view = HeadPickerRequestView(
                base_view=view,
                head_id=head_id,
                sender_nickname=nickname,
                sender_pid=sender_pid,
            )
            files = view._resolve_files()

        try:
            sent_message = await channel.send(
                view=view,
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Stash a reference to the live message on the head-picker view so
            # `on_timeout` can push the button-removal update to Discord.
            if isinstance(view, HeadPickerRequestView):
                view.message = sent_message
        except Exception as e:
            logger.error(f"Failed to send V2 message: {e}", exc_info=True)
            return

        # Check for @teamup keyword
        raw_msg = (msg.get("msg", "") or "").strip().lower()
        if teamup_channel is not None and self.TEAMUP_KEYWORD in raw_msg:
            await self.send_teamup_alert(msg, teamup_channel)

    def _get_rank_name(self, sender_pid: Optional[str]) -> str:
        """Look up the highest custom rank name for the given PID."""
        if not sender_pid or not self.ranks:
            return "Unknown"
        sender_ranks = []
        for rank_id, rank_info in self.ranks.items():
            if sender_pid in rank_info.get("pids", []):
                try:
                    sender_ranks.append((int(rank_id), rank_info.get("name", "Unknown")))
                except (TypeError, ValueError):
                    continue
        if not sender_ranks:
            return "Unknown"
        sender_ranks.sort(key=lambda x: x[0])
        highest_rank_id, highest_rank_name = sender_ranks[0]
        custom_rank_names = {
            1: "Guild Leader",
            2: "Vice Leader",
            5: "Command",
            7: "Half Time Performer",
        }
        return custom_rank_names.get(highest_rank_id, highest_rank_name)

    def _avatar_path(self, head_id) -> Optional[str]:
        """Return absolute path to a local avatar for the given head_id, or None.

        Checks for .png first, then .webp in the mapped subfolder.
        """
        if head_id is None:
            return None
        head_str = str(head_id).strip()
        if not head_str:
            return None
        # Convention: data/avatars/mapped/{head_id}.png or .webp
        for ext in (".png", ".webp"):
            candidate = AVATARS_DIR / "mapped" / f"{head_str}{ext}"
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        return None

    def _list_avatar_files(self, force_refresh: bool = False) -> List[str]:
        """Return a sorted list of PNG/WEBP filenames available in data/avatars/ root (not mapped/)."""
        if self._avatar_files_cache is not None and not force_refresh:
            return self._avatar_files_cache
        try:
            os.makedirs(str(AVATARS_DIR), exist_ok=True)
            files = []
            for f in os.listdir(str(AVATARS_DIR)):
                name_lower = f.lower()
                if not (name_lower.endswith('.png') or name_lower.endswith('.webp')):
                    continue
                # Skip files that have a .done_ marker (already approved/mapped)
                if (AVATARS_DIR / f".done_{f}").exists():
                    continue
                files.append(f)
            files.sort(key=str.lower)
        except Exception as e:
            logger.error(f"Failed to list avatar files: {e}")
            files = []
        self._avatar_files_cache = files
        return files

    async def _cleanup_done_markers(self):
        """Background task: try to delete source avatars that have .done_ markers.

        Once the OS releases its file lock (e.g. Explorer finishes indexing),
        the file can finally be removed. Also tries to remove the marker itself.
        Runs every 30 seconds.
        """
        try:
            avatars_dir = str(AVATARS_DIR)
            if not os.path.isdir(avatars_dir):
                return
            for f in os.listdir(avatars_dir):
                if not f.startswith(".done_"):
                    continue
                # Derive the original source filename from the marker name
                source_name = f[6:]  # strip ".done_" prefix
                source_path = os.path.join(avatars_dir, source_name)
                done_path = os.path.join(avatars_dir, f)
                # Try to delete the source first
                try:
                    os.remove(source_path)
                except OSError:
                    continue  # still locked, try again next cycle
                # Source gone — remove the marker too
                try:
                    os.remove(done_path)
                except OSError:
                    pass
                logger.debug(f"Cleaned up approved avatar: {source_name}")
        except Exception:
            pass

    def _should_offer_avatar_picker(self, head_id) -> bool:
        """True if this is a new (unmapped) head_id worth offering the picker for."""
        if head_id is None:
            return False
        head_str = str(head_id).strip()
        if not head_str:
            return False
        # Already on disk → nothing to do
        if self._avatar_path(head_str) is not None:
            return False
        # Already prompted once → don't keep spamming the button
        if head_str in self.seen_head_ids:
            return False
        return True

    async def _send_admin_avatar_approval(
        self,
        *,
        head_id: str,
        chosen_filename: str,
        suggested_by: discord.abc.User,
        source_message_jump_url: Optional[str],
        sender_nickname: str,
        sender_pid: Optional[str],
    ) -> None:
        """Send the admin-confirmation LayoutView to the configured mod channel."""
        channel = self.bot.get_channel(ADMIN_AVATAR_CHANNEL_ID)
        if channel is None:
            logger.error(f"Admin avatar channel {ADMIN_AVATAR_CHANNEL_ID} not found")
            return

        source_path = AVATARS_DIR / chosen_filename
        if not source_path.exists():
            logger.error(f"Chosen avatar file missing on disk: {source_path}")
            try:
                await channel.send(
                    f"❌ Avatar approval failed: file `{chosen_filename}` is missing on disk."
                )
            except Exception:
                pass
            return

        # Determine target extension from source (support .png and animated .webp)
        target_ext = ".png"
        if chosen_filename.lower().endswith(".webp"):
            target_ext = ".webp"
        head_filename = f"{head_id}{target_ext}"
        head_target_path = AVATARS_DIR / head_filename
        # Avoid filename collisions with existing local files
        copy_source = source_path
        copy_source_name = chosen_filename
        # If the source file is *not* already named after the head_id, the approve
        # action will copy it. We re-attach the original chosen file to the admin
        # message so admins can see what they're approving.

        confirm_view = AdminConfirmView(
            cog=self,
            head_id=head_id,
            head_target_filename=head_filename,
            chosen_filename=chosen_filename,
            suggested_by_id=suggested_by.id if suggested_by else None,
            source_message_jump_url=source_message_jump_url,
        )

        # Read the file into a BytesIO so the disk file is closed immediately.
        # This is important because on Windows the file lock would otherwise
        # block the copy later in the approve flow.
        with open(str(source_path), "rb") as _f:
            file_bytes = _f.read()
        file = discord.File(io.BytesIO(file_bytes), filename=copy_source_name)

        info_lines = [
            f"**New head_id avatar request**",
            f"• `head_id`: **{head_id}**",
            f"• Suggested by: {suggested_by.mention if suggested_by else 'unknown'}",
            f"• Sender nickname: **{sender_nickname}**"
            + (f" (PID: `{sender_pid}`)" if sender_pid else ""),
        ]
        if source_message_jump_url:
            info_lines.append(f"• Original message: {source_message_jump_url}")
        info_lines.append(
            f"\n📎 Chosen file: `{chosen_filename}`"
            f"\n✅ Approve will copy it to `data/avatars/{head_filename}`."
        )

        container = Container(
            TextDisplay("\n".join(info_lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay("**Proposed avatar:**"),
            accent_color=0xE67E22,
        )
        container.add_item(MediaGallery())
        container.children[-1].add_item(media=f"attachment://{copy_source_name}", description=f"Proposed avatar for head_id {head_id}")

        view = LayoutView(timeout=None)
        view.add_item(container)
        view.add_item(confirm_view.action_row)
        view._files = [file]  # attach when sending

        try:
            await channel.send(
                view=view,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.info(
                f"📤 Avatar approval request sent to {channel} for head_id={head_id}, file={chosen_filename}"
            )
        except Exception as e:
            logger.error(f"Failed to send admin avatar approval message: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveChatCog(bot))