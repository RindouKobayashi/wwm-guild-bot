import asyncio
import io
import json
import os
import shutil
import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional, Set, List, Tuple
import re as _re
import discord

from discord.ext import commands, tasks
from discord.ui import (
    LayoutView, Container, TextDisplay, Separator, ActionRow,
    Button, Thumbnail, MediaGallery, Select, Section,
    Label, RadioGroup,
)
import difflib

from settings import BASE_DIR, logger
from utility.wwm import get_club_chat, get_custom_guild_info, get_bulk_players_info, get_film_plan, get_teams_info
from utility.api_constants import get_kongfu_ids_from_player, format_kongfu_display
from googletrans import Translator


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


VERIFICATION_DB_PATH = BASE_DIR / "data" / "guild_verification.db"
AVATARS_DIR = BASE_DIR / "data" / "avatars"
EMOTION_DIR = BASE_DIR / "data" / "emotion"
EMOTION_PENDING_DIR = BASE_DIR / "data" / "emotion" / "pending"

# Channel where avatar-mapping approval requests are sent for admins to review
ADMIN_AVATAR_CHANNEL_ID = 1500005539256602774

# ── Avatar subfolder layout (still vs animated × male / female / shared) ──
# Source files (the PNGs/WEBPs the picker shows) live in 6 subfolders:
#   data/avatars/still_male/      – PNG-only, male-only still images
#   data/avatars/animated_male/   – WEBP-only, male-only animated
#   data/avatars/still_female/    – PNG-only, female-only still images
#   data/avatars/animated_female/ – WEBP-only, female-only animated
#   data/avatars/still_shared/    – PNG-only, works for both genders
#   data/avatars/animated_shared/ – WEBP-only, works for both genders
# Approved copies land in the parallel `mapped/` subfolders with the
# {head_id}.{ext} naming convention:
#   data/avatars/mapped/still_male/1037.png
#   data/avatars/mapped/animated_shared/1234.webp
# etc.
AVATARS_STILL_MALE_DIR = AVATARS_DIR / "still_male"
AVATARS_ANIMATED_MALE_DIR = AVATARS_DIR / "animated_male"
AVATARS_STILL_FEMALE_DIR = AVATARS_DIR / "still_female"
AVATARS_ANIMATED_FEMALE_DIR = AVATARS_DIR / "animated_female"
AVATARS_STILL_SHARED_DIR = AVATARS_DIR / "still_shared"
AVATARS_ANIMATED_SHARED_DIR = AVATARS_DIR / "animated_shared"

AVATARS_MAPPED_DIR = AVATARS_DIR / "mapped"
AVATARS_MAPPED_STILL_MALE_DIR = AVATARS_MAPPED_DIR / "still_male"
AVATARS_MAPPED_ANIMATED_MALE_DIR = AVATARS_MAPPED_DIR / "animated_male"
AVATARS_MAPPED_STILL_FEMALE_DIR = AVATARS_MAPPED_DIR / "still_female"
AVATARS_MAPPED_ANIMATED_FEMALE_DIR = AVATARS_MAPPED_DIR / "animated_female"
AVATARS_MAPPED_STILL_SHARED_DIR = AVATARS_MAPPED_DIR / "still_shared"
AVATARS_MAPPED_ANIMATED_SHARED_DIR = AVATARS_MAPPED_DIR / "animated_shared"

# All 6 subfolders in lookup priority order for a given body_type.
# Picker concatenates files from these (gender-specific first, shared last),
# in still→animated order, so the fast PNGs load before the slow WEBPs.
AVATARS_SOURCE_SUBFOLDERS_BY_BODY_TYPE = {
    0: [  # female
        AVATARS_STILL_FEMALE_DIR,
        AVATARS_STILL_SHARED_DIR,
        AVATARS_ANIMATED_FEMALE_DIR,
        AVATARS_ANIMATED_SHARED_DIR,
    ],
    1: [  # male
        AVATARS_STILL_MALE_DIR,
        AVATARS_STILL_SHARED_DIR,
        AVATARS_ANIMATED_MALE_DIR,
        AVATARS_ANIMATED_SHARED_DIR,
    ],
}

# All 6 source subfolders, in still→animated order. Used for the unknown
# body_type case and for cleanup tasks.
AVATARS_ALL_SOURCE_SUBFOLDERS = [
    AVATARS_STILL_MALE_DIR,
    AVATARS_STILL_FEMALE_DIR,
    AVATARS_STILL_SHARED_DIR,
    AVATARS_ANIMATED_MALE_DIR,
    AVATARS_ANIMATED_FEMALE_DIR,
    AVATARS_ANIMATED_SHARED_DIR,
]

# Lookup priority (most preferred → least preferred) for the
# body-type-aware resolve. The first existing file wins.
# Female:  still_female → animated_female → still_shared → animated_shared
# Male:    still_male   → animated_male   → still_shared → animated_shared
# Unknown: still_shared → animated_shared
AVATARS_MAPPED_LOOKUP_ORDER_BY_BODY_TYPE = {
    0: [  # female
        AVATARS_MAPPED_STILL_FEMALE_DIR,
        AVATARS_MAPPED_ANIMATED_FEMALE_DIR,
        AVATARS_MAPPED_STILL_SHARED_DIR,
        AVATARS_MAPPED_ANIMATED_SHARED_DIR,
    ],
    1: [  # male
        AVATARS_MAPPED_STILL_MALE_DIR,
        AVATARS_MAPPED_ANIMATED_MALE_DIR,
        AVATARS_MAPPED_STILL_SHARED_DIR,
        AVATARS_MAPPED_ANIMATED_SHARED_DIR,
    ],
}

# Valid subfolder values for the UploadAvatarModal Select / for the
# inferred-subfolder from a source path. Strings, lowercase, with underscores.
AVATAR_VALID_SUBFOLDERS = {
    "still_male",
    "animated_male",
    "still_female",
    "animated_female",
    "still_shared",
    "animated_shared",
}

# Body-type constants (mirrors the WWM API: 0 = female, 1 = male).
BODY_TYPE_FEMALE = 0
BODY_TYPE_MALE = 1


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
            # Group the author name and the message body (which already includes
            # the [Translated] block from format_message_embed) into a single
            # Section so they render alongside the avatar thumbnail as one
            # neat chat-bubble-style block.
            section = Section(accessory=Thumbnail(media=f"attachment://{thumb_filename}"))
            section.add_item(TextDisplay(f"**{author_name}**"))
            section.add_item(TextDisplay(body_text))
            container_children.append(section)
        else:
            container_children.append(TextDisplay(f"**{author_name}**"))
            if body_text:
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

    Shows the emotion PNG/WEBP and the author/timestamp via a single Container.
    Optionally includes a Thumbnail of the head_id avatar when one is mapped
    locally — mirrors the Section-based layout used by ChatMessageView so
    emotes line up visually with regular chat messages.
    """

    @staticmethod
    def _is_animated_webp(path: str) -> bool:
        """Check if a WebP file is actually animated by looking for VP8X chunk."""
        if not path.lower().endswith('.webp'):
            return False
        try:
            with open(path, 'rb') as f:
                riff = f.read(4)
                if riff != b'RIFF':
                    return False
                f.seek(8)
                vp8x = f.read(4)
                return vp8x == b'VP8X'
        except Exception:
            return False

    def __init__(
        self,
        *,
        author_name: str,
        ts: int,
        discord_mention: str,
        emotion_id,
        emotion_path: str,
        head_id = None,
        head_avatar_path: Optional[str] = None,
    ):
        super().__init__(timeout=None)
        ext = os.path.splitext(emotion_path)[1] or ".png"
        filename = f"{emotion_id}{ext}"
        self._files: List[discord.File] = [
            discord.File(emotion_path, filename=filename)
        ]
        gallery = MediaGallery()
        gallery.add_item(
            media=f"attachment://{filename}",
            description="Emote",
        )

        footer = f"📅 <t:{ts}:F> (<t:{ts}:R>)"
        if discord_mention:
            footer += f"\n{discord_mention}"

        container_children: list = []

        if head_avatar_path:
            # Preserve original extension so animated .webp avatars stay animated
            thumb_ext = os.path.splitext(head_avatar_path)[1] or ".png"
            thumb_filename = f"head_{head_id}{thumb_ext}"
            self._files.append(discord.File(head_avatar_path, filename=thumb_filename))
            section = Section(accessory=Thumbnail(media=f"attachment://{thumb_filename}"))
            section.add_item(TextDisplay(f"**{author_name}**"))
            container_children.append(section)
        else:
            container_children.append(TextDisplay(f"**{author_name}**"))

        container_children.append(gallery)
        container_children.append(Separator(spacing=discord.SeparatorSpacing.small))
        container_children.append(TextDisplay(footer))

        container = Container(*container_children, accent_color=0x9B59B6)
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


class UploadAvatarModal(discord.ui.Modal, title="Upload Custom Avatar"):
    """Modal that lets the user upload an image from their computer.

    Fields:
      - `avatar_file`  (discord.ui.FileUpload) — native file picker. Accepts
        one PNG or WEBP (≤ 10 MB). Wrapped in a `Label` because
        Components V2 file pickers (type 19) are not auto-wrapped in an
        ActionRow by discord.py — they have to be wrapped in a Label or
        Discord rejects the modal payload.
      - `subfolder`    (discord.ui.RadioGroup) — pick one of the 6 source
        subfolders. The default option is pre-selected based on the
        sender's `body_type` (male → still_male, female → still_female,
        unknown → still_shared). Like FileUpload, RadioGroup is also a
        Label-only component in modals.

    On submit we validate the attachment + the selected subfolder, then
    call the cog's `_stage_uploaded_avatar` directly (no follow-up view
    needed) and reply with a confirmation.
    """

    # Stable custom_id for the RadioGroup so on_submit can find the
    # selected value inside `interaction.data["components"]` (RadioGroup
    # has no `.values` accessor like Select / FileUpload).
    SUBFOLDER_COMPONENT_ID = "upload_avatar_subfolder"

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        head_id: str,
        body_type: Optional[int],
        sender_nickname: str,
        sender_pid: Optional[str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.head_id = str(head_id) if head_id is not None else ""
        self.body_type: Optional[int] = body_type if body_type in (0, 1) else None
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid

        # Pick the suggested subfolder from the sender's body_type.
        if body_type == BODY_TYPE_MALE:
            suggested = "still_male"
        elif body_type == BODY_TYPE_FEMALE:
            suggested = "still_female"
        else:
            suggested = "still_shared"

        # Field 1: FileUpload wrapped in a Label.
        self.avatar_file = discord.ui.FileUpload(
            required=True,
            min_values=1,
            max_values=1,
        )
        self.add_item(
            Label(
                text="Upload a PNG or WEBP avatar (max 10 MB)",
                component=self.avatar_file,
            )
        )

        # Field 2: RadioGroup wrapped in a Label for subfolder selection.
        self.subfolder_group = RadioGroup(
            required=True,
            custom_id=self.SUBFOLDER_COMPONENT_ID,
            options=[
                discord.RadioGroupOption(
                    label="🖼️ Still Male",
                    description="PNG — male-only still image",
                    value="still_male",
                    default=(suggested == "still_male"),
                ),
                discord.RadioGroupOption(
                    label="🎞️ Animated Male",
                    description="WEBP — male-only animated",
                    value="animated_male",
                    default=(suggested == "animated_male"),
                ),
                discord.RadioGroupOption(
                    label="🖼️ Still Female",
                    description="PNG — female-only still image",
                    value="still_female",
                    default=(suggested == "still_female"),
                ),
                discord.RadioGroupOption(
                    label="🎞️ Animated Female",
                    description="WEBP — female-only animated",
                    value="animated_female",
                    default=(suggested == "animated_female"),
                ),
                discord.RadioGroupOption(
                    label="🤝 Still Shared",
                    description="PNG — works for both genders, still",
                    value="still_shared",
                    default=(suggested == "still_shared"),
                ),
                discord.RadioGroupOption(
                    label="🤝 Animated Shared",
                    description="WEBP — works for both genders, animated",
                    value="animated_shared",
                    default=(suggested == "animated_shared"),
                ),
            ],
        )
        self.add_item(
            Label(
                text="Target subfolder",
                component=self.subfolder_group,
            )
        )

    @staticmethod
    def _extract_subfolder(interaction: discord.Interaction) -> Optional[str]:
        """Find the RadioGroup's selected value in `interaction.data`.

        Modal-submitted RadioGroups surface in `interaction.data` under
        `components` (mirroring the layout sent to Discord). Each entry has
        a `custom_id` and a `value` (the selected option's `value`).
        """
        try:
            for comp in (interaction.data or {}).get("components", []) or []:
                # comp may be the Label wrapper (with nested "component") or
                # the RadioGroup itself depending on discord.py's layout.
                inner = comp.get("component", comp)
                if inner.get("custom_id") == UploadAvatarModal.SUBFOLDER_COMPONENT_ID:
                    val = inner.get("value")
                    if isinstance(val, str) and val:
                        return val
                    # Older payload shape: values list on the component.
                    vals = inner.get("values") or []
                    if vals:
                        return vals[0]
        except Exception:
            pass
        return None

    async def on_submit(self, interaction: discord.Interaction):
        # discord.ui.FileUpload.values is a list[discord.Attachment].
        attachments = self.avatar_file.values
        if not attachments:
            await interaction.response.send_message(
                "❌ No file was attached.", ephemeral=True
            )
            return
        attachment: discord.Attachment = attachments[0]

        # Quick sanity checks (the cog does deeper validation).
        if attachment.size > 10 * 1024 * 1024:
            await interaction.response.send_message(
                f"❌ File too large ({attachment.size // 1024} KB; max 10 MB).",
                ephemeral=True,
            )
            return
        fname_lower = attachment.filename.lower()
        if not (fname_lower.endswith(".png") or fname_lower.endswith(".webp")):
            await interaction.response.send_message(
                "❌ File must be a `.png` or `.webp`.", ephemeral=True
            )
            return

        # Pull the selected subfolder from the RadioGroup in interaction.data.
        subfolder = self._extract_subfolder(interaction)
        if subfolder is None or subfolder not in AVATAR_VALID_SUBFOLDERS:
            await interaction.response.send_message(
                f"❌ Please pick a target subfolder from the radio list.",
                ephemeral=True,
            )
            return

        # Acknowledge the modal right away so we don't hit the 3-second
        # deadline while we read the upload bytes.
        await interaction.response.defer(ephemeral=True)

        # Read the attachment bytes before calling _stage_uploaded_avatar,
        # because we may need them for the DuplicateCheckView.
        try:
            upload_data = await attachment.read()
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to read attachment: {e}", ephemeral=True)
            return

        # Re-create the attachment with the already-read bytes so
        # _stage_uploaded_avatar can read() it again (Discord attachments
        # are single-read).
        class _ReReadableAttachment:
            """Minimal wrapper that mimics discord.Attachment for .read()."""
            def __init__(self, data: bytes, filename: str):
                self._data = data
                self.filename = filename
            async def read(self):
                return self._data

        re_readable = _ReReadableAttachment(upload_data, attachment.filename)
        ok, err_or_filename = await self.cog._stage_uploaded_avatar(
            attachment=re_readable,  # type: ignore[arg-type]
            subfolder=subfolder,
            head_id=self.head_id,
            body_type=self.body_type,
            suggested_by=interaction.user,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
            source_message_jump_url=None,
        )
        if not ok and err_or_filename.startswith("duplicate:"):
            # Similar file found — show the DuplicateCheckView
            existing_relative = err_or_filename[len("duplicate:"):]
            dupe_view = DuplicateCheckView(
                cog=self.cog,
                uploaded_data=upload_data,
                uploaded_filename=attachment.filename,
                existing_filename=existing_relative,
                head_id=self.head_id,
                body_type=self.body_type,
                suggested_by=interaction.user,
                sender_nickname=self.sender_nickname,
                sender_pid=self.sender_pid,
                source_message_jump_url=None,
                subfolder=subfolder,
            )
            dupe_files = dupe_view._resolve_files()
            try:
                await interaction.followup.send(
                    content=None,
                    view=dupe_view,
                    files=dupe_files,
                    ephemeral=True,
                )
            except Exception:
                pass
            return
        elif not ok:
            await interaction.followup.send(f"❌ {err_or_filename}", ephemeral=True)
            return

        # Mark (head_id, body_type) as seen so we don't re-prompt.
        if self.head_id and self.body_type is not None:
            self.cog.seen_head_map.add((self.head_id, self.body_type))
            self.cog.save_config()

        await interaction.followup.send(
            f"✅ Saved `{err_or_filename}` to `data/avatars/{subfolder}/` and "
            f"sent to admins for approval for head_id `{self.head_id}`.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"UploadAvatarModal on_error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ Upload failed: {error}", ephemeral=True
                )
        except Exception:
            pass


class HeadPickerRequestView(LayoutView):
    """Wraps a normal chat message and adds a "Set Avatar" button.

    Times out after 180 seconds, after which the button auto-disables.
    Knows about the sender's `body_type` so the picker it opens can filter
    avatars to the correct gender / format subfolders.
    """


    PICKER_TIMEOUT = 180.0  # seconds

    def __init__(
        self,
        *,
        base_view: ChatMessageView,
        head_id,
        sender_nickname: str,
        sender_pid: Optional[str],
        body_type: Optional[int] = None,
    ):
        super().__init__(timeout=self.PICKER_TIMEOUT)
        self.base_view = base_view
        self.head_id = str(head_id) if head_id is not None else ""
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid
        # Body type of the sender (0=female, 1=male, None=unknown). The
        # picker uses this to filter the source subfolders it scans.
        self.body_type: Optional[int] = body_type if body_type in (0, 1) else None

        # Carry over the base view's items (a single Container)
        for item in list(base_view.children):
            self.add_item(item)

        action_row = ActionRow()
        button = Button(
            label="🖼️ Set Avatar",
            style=discord.ButtonStyle.primary,
            custom_id=f"head_picker_open:{self.head_id}:{self.body_type if self.body_type is not None else ''}",
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

        # Note: head_id is NOT marked as "seen" here — the user may close the
        # picker without completing a selection. Marking happens only when the
        # user actually selects an avatar (see AvatarPickerView._on_select) or
        # when an admin approves (AdminConfirmView._on_approve).
        #
        # Show a category picker FIRST (no images) so the user picks which
        # gender/format subfolder to browse. Avatar files only get loaded
        # once the user has chosen a category — that way the Set Avatar
        # button never makes Discord pay the cost of reading every avatar
        # in every subfolder upfront.
        view = CategoryPickerView(
            cog=cog,
            head_id=self.head_id,
            body_type=self.body_type,
            suggested_by=interaction.user,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
        )
        await interaction.response.send_message(
            content=None,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        # Remove the "Set Avatar" button row entirely (rather than just
        # disabling the button) so the live message stops showing a dead
        # control. If we know which channel message this view was attached
        # to, push the change to Discord so users see the button disappear.
        bt_part = "" if self.body_type is None else str(self.body_type)
        target_custom_id = f"head_picker_open:{self.head_id}:{bt_part}"
        rows_to_remove = [
            child for child in self.children
            if isinstance(child, ActionRow)
            and any(isinstance(item, Button) and item.custom_id == target_custom_id
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



class CategoryPickerView(LayoutView):
    """Ephemeral landing view that asks the user WHICH subfolder they want to browse.

    Shown after the user clicks "🖼️ Set Avatar" on the chat message. Does
    NOT load any avatar images up front — that only happens after the
    user picks a category from the Select menu (or hits Upload Custom).

    The dropdown offers up to 6 categories depending on the sender's
    `body_type`:

        body_type=1 (male)   → Still Male, Animated Male,
                               Still Shared, Animated Shared
        body_type=0 (female) → Still Female, Animated Female,
                               Still Shared, Animated Shared
        body_type=None       → the 4 shared-only options

    Each option's `value` is the source subfolder name (e.g. ``still_male``)
    which is then handed to ``LiveChatCog._list_avatar_files_in_subfolder``
    to load ONLY that one subfolder's files into the actual picker.
    """

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        head_id: str,
        body_type: Optional[int],
        suggested_by: discord.abc.User,
        sender_nickname: str,
        sender_pid: Optional[str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.head_id = head_id
        self.body_type: Optional[int] = body_type if body_type in (0, 1) else None
        self.suggested_by = suggested_by
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid
        self._build()

    def _build(self) -> None:
        self.clear_items()

        # Build the Select options filtered by body_type. Always include
        # the shared options so the user can fall back to a gender-neutral
        # avatar if they don't want their-gender-specific one.
        if self.body_type == BODY_TYPE_MALE:
            options = [
                discord.SelectOption(
                    label="🖼️ Still Male",
                    description="PNG — male-only still images",
                    value="still_male",
                ),
                discord.SelectOption(
                    label="🎞️ Animated Male",
                    description="WEBP — male-only animated",
                    value="animated_male",
                ),
                discord.SelectOption(
                    label="🖼️ Still Shared",
                    description="PNG — works for both genders, still",
                    value="still_shared",
                ),
                discord.SelectOption(
                    label="🎞️ Animated Shared",
                    description="WEBP — works for both genders, animated",
                    value="animated_shared",
                ),
            ]
        elif self.body_type == BODY_TYPE_FEMALE:
            options = [
                discord.SelectOption(
                    label="🖼️ Still Female",
                    description="PNG — female-only still images",
                    value="still_female",
                ),
                discord.SelectOption(
                    label="🎞️ Animated Female",
                    description="WEBP — female-only animated",
                    value="animated_female",
                ),
                discord.SelectOption(
                    label="🖼️ Still Shared",
                    description="PNG — works for both genders, still",
                    value="still_shared",
                ),
                discord.SelectOption(
                    label="🎞️ Animated Shared",
                    description="WEBP — works for both genders, animated",
                    value="animated_shared",
                ),
            ]
        else:
            # Unknown / no body_type → only shared subfolders are meaningful.
            options = [
                discord.SelectOption(
                    label="🖼️ Still Shared",
                    description="PNG — works for both genders, still",
                    value="still_shared",
                ),
                discord.SelectOption(
                    label="🎞️ Animated Shared",
                    description="WEBP — works for both genders, animated",
                    value="animated_shared",
                ),
            ]

        gender_hint = {
            BODY_TYPE_FEMALE: "female",
            BODY_TYPE_MALE: "male",
        }.get(self.body_type, "either")

        inner: list = [
            TextDisplay(
                f"# 🖼️ Pick a category for head_id `{self.head_id}`\n"
                f"Choose which {gender_hint} avatar subfolder to browse, or upload a custom image.\n"
                f"\n"
                f"_No images are loaded until you pick a category — this just lets you pick the folder first._"
            ),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        # Select menu for picking a category
        select_row = ActionRow()
        category_select = Select(
            placeholder="Pick a category to browse…",
            options=options,
            custom_id=f"avatar_category_pick:{self.head_id}",
        )
        category_select.callback = self._on_category
        select_row.add_item(category_select)
        inner.append(select_row)

        # Bottom row: Upload Custom + Close
        nav_row = ActionRow()
        upload_btn = Button(
            label="📤 Upload Custom",
            style=discord.ButtonStyle.success,
            custom_id="avatar_picker_upload",
        )
        upload_btn.callback = self._on_upload_custom
        nav_row.add_item(upload_btn)

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

    def _disable_controls(self) -> None:
        """Disable the Select and all buttons to prevent double-clicks."""
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True

    async def _on_category(self, interaction: discord.Interaction):
        chosen = interaction.data.get("values", [None])[0]
        if not chosen or chosen not in AVATAR_VALID_SUBFOLDERS:
            await interaction.response.send_message(
                "❌ Invalid category selected.", ephemeral=True
            )
            return
        # Lock the category picker right away so a second click doesn't
        # open two avatar pickers.
        self._disable_controls()
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # Load ONLY this one subfolder's files — no scanning the other 5
        # subfolders just to throw their results away.
        avatar_files = self.cog._list_avatar_files_in_subfolder(
            subfolder=chosen,
            force_refresh=True,
        )
        if not avatar_files:
            await interaction.followup.send(
                f"❌ No avatar files are available in `data/avatars/{chosen}/`.\n"
                f"Add at least one PNG/WEBP to that subfolder (or pick a different category) and try again.",
                ephemeral=True,
            )
            return

        # Build the actual paginated picker and edit the same message in
        # place — this keeps the user in one message and avoids cluttering
        # the channel with a follow-up.
        view = AvatarPickerView(
            cog=self.cog,
            head_id=self.head_id,
            body_type=self.body_type,
            avatar_files=avatar_files,
            suggested_by=self.suggested_by,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
        )
        files = view._resolve_files()
        # Defer to satisfy Discord's 3-second window while we attach files
        # (especially animated .webp which can be slow).
        try:
            await interaction.edit_original_response(
                view=view,
                attachments=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            # Fallback: send as a new follow-up if the edit raced.
            await interaction.followup.send(
                content=None,
                view=view,
                files=files,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _on_upload_custom(self, interaction: discord.Interaction):
        """Open the UploadAvatarModal so the user can attach a fresh image."""
        self._disable_controls()
        try:
            await interaction.response.send_modal(
                UploadAvatarModal(
                    cog=self.cog,
                    head_id=self.head_id,
                    body_type=self.body_type,
                    sender_nickname=self.sender_nickname,
                    sender_pid=self.sender_pid,
                )
            )
        except Exception as e:
            logger.error(f"Failed to open upload modal: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Failed to open the upload form. Please try again.",
                        ephemeral=True,
                    )
            except Exception:
                pass

    async def _on_close(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True
        close_container = Container(
            TextDisplay("Picker closed."),
            accent_color=0x3498DB,
        )
        close_view = LayoutView(timeout=None)
        close_view.add_item(close_container)
        await interaction.response.edit_message(view=close_view)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True
        self.stop()


class AvatarPickerView(LayoutView):
    """Ephemeral paginated picker that lets a user choose one of the avatar PNGs.

    Shows up to 9 avatars per page (up to 3x3 grid, Discord's MediaGallery limit)
    plus pagination + a Select menu + a "📤 Upload Custom" button that opens
    a Modal for attaching a fresh image from the user's computer.

    The list of source files is pre-filtered by the sender's `body_type` (via
    `LiveChatCog._list_avatar_files`) so the picker never shows irrelevant
    gender-specific folders. PNGs load before WEBPs for snappier first paint,
    and per-page file objects are cached to speed up pagination.
    """

    ITEMS_PER_PAGE = 9
    _page_cache: dict = {}  # page_number -> (List[discord.File], List[str])

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        head_id: str,
        body_type: Optional[int],
        avatar_files: List[str],
        suggested_by: discord.abc.User,
        sender_nickname: str,
        sender_pid: Optional[str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.head_id = head_id
        # body_type of the original sender (0=female, 1=male, None=unknown).
        # Used to filter the picker source subfolders and to pre-select the
        # suggested subfolder in the UploadAvatarModal.
        self.body_type: Optional[int] = body_type if body_type in (0, 1) else None
        # Files are already sorted PNG-first by _list_avatar_files. Each
        # entry is a RELATIVE path under AVATARS_DIR (e.g. "still_male/foo.png").
        self.avatar_files = list(avatar_files)
        self.suggested_by = suggested_by
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid
        self.page = 0
        self._files: List[discord.File] = []
        self._nav_in_progress = False
        # Clear any stale cache from a previous picker instance
        AvatarPickerView._page_cache.clear()
        self._build()


    def _resolve_files(self) -> List[discord.File]:
        return list(self._files)

    def _page_slice(self) -> List[str]:
        start = self.page * self.ITEMS_PER_PAGE
        end = start + self.ITEMS_PER_PAGE
        return self.avatar_files[start:end]

    @staticmethod
    def _page_format_counts(page_files: List[str]) -> str:
        """Return a short summary of file formats on this page, e.g. '5 PNG, 4 WEBP'."""
        png = sum(1 for f in page_files if f.lower().endswith('.png'))
        webp = sum(1 for f in page_files if f.lower().endswith('.webp'))
        parts = []
        if png:
            parts.append(f"{png} PNG")
        if webp:
            parts.append(f"{webp} WEBP")
        return ", ".join(parts) if parts else "empty"

    def _load_page_files(self, page_num: int) -> tuple[List[discord.File], List[str]]:
        """Load (files, filenames) for a given page from cache or disk."""
        if page_num in AvatarPickerView._page_cache:
            return AvatarPickerView._page_cache[page_num]

        start = page_num * self.ITEMS_PER_PAGE
        end = start + self.ITEMS_PER_PAGE
        page_files = self.avatar_files[start:end]
        loaded: List[discord.File] = []
        for filename in page_files:
            disk_path = AVATARS_DIR / filename
            if not disk_path.exists():
                continue
            attach_name = f"picker_{page_num}_{filename}"
            loaded.append(discord.File(str(disk_path), filename=attach_name))
        result = (loaded, page_files)
        AvatarPickerView._page_cache[page_num] = result
        return result

    def _build(self) -> None:
        self.clear_items()
        self._files = []
        is_loading = getattr(self, '_nav_in_progress', False)

        page_files = self._page_slice()
        total_pages = max(1, -(-len(self.avatar_files) // self.ITEMS_PER_PAGE))

        inner: list = []

        # Show a loading indicator if navigating to a page with WEBP files
        header_text = (
            f"# 🖼️ Pick an avatar for head_id `{self.head_id}`\n"
            f"Page **{self.page + 1}** / **{total_pages}** "
            f"({len(self.avatar_files)} avatars available)\n"
        )
        if is_loading:
            header_text += "🔄 Loading page… please wait."
        else:
            header_text += "Use the dropdown to choose, then it will be sent to admins for confirmation."

        inner.append(TextDisplay(header_text))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Build a media gallery with the current page of avatars
        if page_files:
            # Defensive: _load_page_files should always return a tuple now
            # that the neighbour-placeholder loop has been removed, but guard
            # against any future regression that could re-introduce a `None`
            # cache entry (which would cause the tuple-unpack to crash).
            loaded = self._load_page_files(self.page)
            if loaded is None:
                loaded_files, _ = [], page_files
            else:
                loaded_files, _ = loaded
            self._files = loaded_files
            gallery = MediaGallery()
            for fname in page_files:
                attach_name = f"picker_{self.page}_{fname}"
                gallery.add_item(
                    media=f"attachment://{attach_name}",
                    description=fname[:80],
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

        # "Go to page" dropdown row — includes format counts
        go_row = ActionRow()
        if total_pages > 1:
            go_options = []
            for i in range(total_pages):
                start = i * self.ITEMS_PER_PAGE
                end = start + self.ITEMS_PER_PAGE
                page_file_list = self.avatar_files[start:end]
                fmt_count = self._page_format_counts(page_file_list)
                label = f"Page {i + 1} ({fmt_count})" if fmt_count else f"Page {i + 1}"
                go_options.append(
                    discord.SelectOption(
                        label=label[:98],
                        description=f"Go to page {i + 1} of {total_pages}",
                        value=str(i + 1),
                        default=(i == self.page),
                    )
                )
            go_select = Select(
                placeholder="Go to page…",
                options=go_options,
                custom_id="avatar_picker_goto",
            )
            go_select.callback = self._on_goto
            go_row.add_item(go_select)
            inner.append(go_row)

        # Navigation row: ⬅ Prev | Next ➡ | 📤 Upload Custom | Close
        # (4 buttons — within Discord's 5-per-ActionRow limit)
        nav_row = ActionRow()
        prev_btn = Button(
            label="⬅ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="avatar_picker_prev",
            disabled=self.page <= 0 or is_loading,
        )
        prev_btn.callback = self._on_prev
        nav_row.add_item(prev_btn)

        next_btn = Button(
            label="Next ➡",
            style=discord.ButtonStyle.secondary,
            custom_id="avatar_picker_next",
            disabled=self.page >= total_pages - 1 or is_loading,
        )
        next_btn.callback = self._on_next
        nav_row.add_item(next_btn)

        upload_btn = Button(
            label="📤 Upload Custom",
            style=discord.ButtonStyle.success,
            custom_id="avatar_picker_upload",
        )
        upload_btn.callback = self._on_upload_custom
        nav_row.add_item(upload_btn)

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

    def _disable_nav_buttons(self) -> None:
        """Disable all prev/next/goto controls to prevent double-clicks."""
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, (Button, Select)):
                        item.disabled = True

    async def _navigate(self, interaction: discord.Interaction, new_page: int) -> None:
        """Navigate to a page with loading indicator and cached file reuse."""
        # Defer immediately so we don't hit Discord's 3-second interaction
        # token expiry (animated .webp files can be slow to upload).
        try:
            await interaction.response.defer()
        except Exception:
            pass

        # Check if the target page is already cached
        if new_page in AvatarPickerView._page_cache and AvatarPickerView._page_cache[new_page] is not None:
            self.page = new_page
            self._nav_in_progress = False
            self._build()
            try:
                await interaction.edit_original_response(
                    view=self,
                    attachments=self._files,
                )
            except Exception:
                pass
            return

        # Not cached — show loading indicator
        self.page = new_page
        self._nav_in_progress = True
        self._build()
        try:
            await interaction.edit_original_response(
                view=self,
                attachments=[],  # Don't send stale attachments during loading
            )
        except Exception:
            pass

        # Load the page with actual files (triggers cache population)
        self._nav_in_progress = False
        self._build()
        try:
            await interaction.edit_original_response(
                view=self,
                attachments=self._files,
            )
        except Exception:
            pass

    async def _on_select(self, interaction: discord.Interaction):
        chosen = interaction.data.get("values", [None])[0]
        if not chosen:
            await interaction.response.send_message("❌ No avatar selected.", ephemeral=True)
            return
        # Disable the picker after a selection to prevent double-sends
        self._disable_nav_buttons()
        await interaction.response.edit_message(view=self)

        # Record this (head_id, body_type) as "handled" so we don't re-prompt on
        # later messages from the same gender. (A male and a female sender with
        # the same head_id are now tracked independently.)
        if self.head_id and self.body_type is not None:
            self.cog.seen_head_map.add((self.head_id, self.body_type))
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
            body_type=self.body_type,
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

    async def _on_upload_custom(self, interaction: discord.Interaction):
        """Open the UploadAvatarModal so the user can attach a fresh image.

        The modal uses discord.ui.FileUpload (native file picker) for the
        file + a discord.ui.RadioGroup for picking the target subfolder.
        The pre-selected subfolder is suggested based on the sender's
        `body_type` (e.g. male sender → `still_male`).
        """
        # Disable the picker controls to prevent double-clicks while the
        # modal is open. We don't try to edit the message here because the
        # modal opens in parallel and any edit would race with the modal
        # launch.
        self._disable_nav_buttons()
        try:
            # Pop the modal via the interaction. The FileUpload field plus
            # the subfolder RadioGroup are defined on UploadAvatarModal.
            await interaction.response.send_modal(
                UploadAvatarModal(
                    cog=self.cog,
                    head_id=self.head_id,
                    body_type=self.body_type,
                    sender_nickname=self.sender_nickname,
                    sender_pid=self.sender_pid,
                )
            )
        except Exception as e:
            logger.error(f"Failed to open upload modal: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Failed to open the upload form. Please try again.",
                        ephemeral=True,
                    )
            except Exception:
                pass


    async def _on_prev(self, interaction: discord.Interaction):
        if self.is_finished() or self._nav_in_progress:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return
        if self.page > 0:
            await self._navigate(interaction, self.page - 1)

    async def _on_next(self, interaction: discord.Interaction):
        if self.is_finished() or self._nav_in_progress:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return
        total_pages = max(1, -(-len(self.avatar_files) // self.ITEMS_PER_PAGE))
        if self.page < total_pages - 1:
            await self._navigate(interaction, self.page + 1)

    async def _on_goto(self, interaction: discord.Interaction):
        """Handle the 'Go to page' select menu."""
        if self.is_finished() or self._nav_in_progress:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return
        page_str = interaction.data.get("values", [None])[0]
        if page_str is None:
            return
        try:
            target = int(page_str) - 1  # values are 1-based
            total_pages = max(1, -(-len(self.avatar_files) // self.ITEMS_PER_PAGE))
            if 0 <= target < total_pages and target != self.page:
                await self._navigate(interaction, target)
                return
        except (ValueError, TypeError):
            pass
        # If we couldn't navigate, just leave the message as-is

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

    `chosen_filename` is a RELATIVE path under `AVATARS_DIR` (e.g.
    `still_male/foo.png` or `legacy/foo.png`). The approve flow copies the
    file into the matching `mapped/<subfolder>/{head_id}.{ext}` so the
    body-type-aware resolver can find it later.
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
        body_type: Optional[int] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.head_id = head_id
        self.head_target_filename = head_target_filename
        self.chosen_filename = chosen_filename
        self.suggested_by_id = suggested_by_id
        self.source_message_jump_url = source_message_jump_url
        # body_type of the original sender (0=female, 1=male, None=unknown).
        # Used by `_on_approve` to mark the right (head_id, body_type) key
        # in `seen_head_map`, and by `_on_reject` to remove it.
        self.body_type: Optional[int] = body_type if body_type in (0, 1) else None

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

    @staticmethod
    def _infer_source_subfolder(chosen_filename: str) -> str:
        """Extract the source subfolder from a relative path.

        `still_male/foo.png` → `still_male`
        `foo.png` (legacy flat file) → `""` (empty string; the destination will
        then be `mapped/{head_id}.{ext}` for backwards compatibility).
        """
        chosen_norm = chosen_filename.replace("\\", "/")
        if "/" in chosen_norm:
            return chosen_norm.split("/", 1)[0]
        return ""

    async def _on_approve(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        source_path = AVATARS_DIR / self.chosen_filename
        if not source_path.exists() or not source_path.is_file():
            logger.error(f"Source avatar missing on disk: {source_path}")
            self._disable()
            err_container = Container(
                TextDisplay(
                    f"❌ Source file `data/avatars/{self.chosen_filename}` is missing on disk."
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

        # Determine destination subfolder from the source's parent directory.
        # e.g. chosen "still_male/foo.png" → destination "mapped/still_male/".
        # For legacy flat files (no parent), destination is "mapped/" root.
        source_subfolder = self._infer_source_subfolder(self.chosen_filename)
        target_dir = AVATARS_MAPPED_DIR
        if source_subfolder and source_subfolder in AVATAR_VALID_SUBFOLDERS:
            target_dir = AVATARS_MAPPED_DIR / source_subfolder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / self.head_target_filename
        # Display string for the success/info embeds (e.g. "mapped/still_male/1037.png").
        target_display = f"mapped/{source_subfolder}/{self.head_target_filename}" if source_subfolder else f"mapped/{self.head_target_filename}"

        try:
            if source_path.resolve() != target_path.resolve():
                # Copy the file contents to the destination subfolder. shutil.copy2
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
            # (the cache is a dict keyed by body_type — clear all of them).
            try:
                self.cog._avatar_files_cache.clear()
            except Exception:
                self.cog._avatar_files_cache = {}
        except Exception as e:
            logger.error(f"Failed to copy avatar for head_id {self.head_id}: {e}")
            self._disable()
            err_container = Container(
                TextDisplay(
                    f"❌ Failed to copy `{self.chosen_filename}` → "
                    f"`data/avatars/{target_display}`: {e}"
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

        # Mark the (head_id, body_type) pair as mapped so we won't re-prompt
        # for this gender. The same head_id for a different body_type is
        # still allowed to be prompted (and may resolve to a different file).
        if self.head_id and self.body_type is not None:
            self.cog.seen_head_map.add((self.head_id, self.body_type))
            self.cog.save_config()

        # Build a detailed approval summary that explicitly names the head_id,
        # the source subfolder + filename, the destination path, the suggester,
        # and the approver so anyone reading the admin channel later has the
        # full context without having to scroll up.
        body_type_label = {
            BODY_TYPE_FEMALE: "Female",
            BODY_TYPE_MALE: "Male",
        }.get(self.body_type, "Unknown")
        subfolder_label = source_subfolder if source_subfolder else "(legacy / root)"
        source_filename = source_path.name
        summary_lines = [
            f"✅ **Approved** by {interaction.user.mention}",
            "",
            f"• `head_id`: **{self.head_id}**",
            f"• `body_type`: **{body_type_label}**",
            f"• Source subfolder: **{subfolder_label}**",
            f"• Source filename: `{source_filename}`",
            f"• Saved to: `data/avatars/{target_display}`",
        ]
        if self.suggested_by_id:
            summary_lines.append(f"• Suggested by: <@{self.suggested_by_id}>")
        if self.source_message_jump_url:
            summary_lines.append(f"• Original message: {self.source_message_jump_url}")
        summary_text = "\n".join(summary_lines)

        # Edit the original admin message to mark the approval, drop the
        # now-stale proposed-avatar attachment, and remove the buttons entirely.
        try:
            ok_container = Container(
                TextDisplay(summary_text),
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
            f"✅ Avatar approved: head_id={self.head_id} → {target_display} "
            f"(renamed from {self.chosen_filename}, by {interaction.user})"
        )

    async def _on_reject(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)

        # If the upload was fresh from a Modal, also remove the now-orphaned
        # source file so it doesn't get re-listed by the picker. (For picker
        # selections, the source stays — admins may pick the same file again.)
        try:
            if self.chosen_filename.startswith("still_") or self.chosen_filename.startswith("animated_"):
                source_path = AVATARS_DIR / self.chosen_filename
                if source_path.exists():
                    try:
                        source_path.unlink()
                    except OSError:
                        pass
                    # Also drop any .done_ marker next to the source if present
                    done_marker = source_path.with_name(f".done_{source_path.name}")
                    if done_marker.exists():
                        try:
                            done_marker.unlink()
                        except OSError:
                            pass
        except Exception:
            pass

        # Remove the (head_id, body_type) pair from seen_head_map so the
        # bot can re-prompt the same gender if it sends another message.
        if self.head_id and self.body_type is not None:
            self.cog.seen_head_map.discard((self.head_id, self.body_type))
            self.cog.save_config()

        # Build a detailed rejection summary mirroring the approval format.
        body_type_label = {
            BODY_TYPE_FEMALE: "Female",
            BODY_TYPE_MALE: "Male",
        }.get(self.body_type, "Unknown")
        source_subfolder = AdminConfirmView._infer_source_subfolder(self.chosen_filename)
        subfolder_label = source_subfolder if source_subfolder else "(legacy / root)"
        source_filename = self.chosen_filename.split("/")[-1]
        summary_lines = [
            f"❌ **Rejected** by {interaction.user.mention}",
            "",
            f"• `head_id`: **{self.head_id}**",
            f"• `body_type`: **{body_type_label}**",
            f"• Source subfolder: **{subfolder_label}**",
            f"• Source filename: `{source_filename}`",
            f"• Rejected path: `data/avatars/{self.chosen_filename}`",
        ]
        if self.suggested_by_id:
            summary_lines.append(f"• Suggested by: <@{self.suggested_by_id}>")
        if self.source_message_jump_url:
            summary_lines.append(f"• Original message: {self.source_message_jump_url}")
        summary_lines.append(
            "\nThe Set Avatar button will appear again on the next message from this player."
        )
        summary_text = "\n".join(summary_lines)

        # Edit the original admin message to mark the rejection, drop the
        # proposed-avatar attachment, and remove the buttons entirely.
        try:
            reject_container = Container(
                TextDisplay(summary_text),
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



class UploadEmotionModal(discord.ui.Modal, title="Upload Custom Emotion"):
    """Modal that lets the user upload an emotion image from their computer.

    Much simpler than UploadAvatarModal because emotions don't need subfolders
    or body_type. The file is saved as `{emotion_id}.{ext}` in data/emotion/pending/
    and then sent to admins for approval.
    """

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        emotion_id: str,
        sender_nickname: str,
        sender_pid: Optional[str],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.emotion_id = str(emotion_id)
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid

        self.emotion_file = discord.ui.FileUpload(
            required=True,
            min_values=1,
            max_values=1,
        )
        self.add_item(
            Label(
                text="Upload emotion image (PNG/WEBP, max 10 MB)",
                component=self.emotion_file,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        attachments = self.emotion_file.values
        if not attachments:
            await interaction.response.send_message(
                "❌ No file was attached.", ephemeral=True
            )
            return
        attachment: discord.Attachment = attachments[0]

        if attachment.size > 10 * 1024 * 1024:
            await interaction.response.send_message(
                f"❌ File too large ({attachment.size // 1024} KB; max 10 MB).",
                ephemeral=True,
            )
            return
        fname_lower = attachment.filename.lower()
        if not (fname_lower.endswith(".png") or fname_lower.endswith(".webp")):
            await interaction.response.send_message(
                "❌ File must be a `.png` or `.webp`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            upload_data = await attachment.read()
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to read attachment: {e}", ephemeral=True)
            return

        class _ReReadableAttachment:
            def __init__(self, data: bytes, filename: str):
                self._data = data
                self.filename = filename
            async def read(self):
                return self._data

        re_readable = _ReReadableAttachment(upload_data, attachment.filename)
        ok, err_or_filename = await self.cog._stage_emotion_upload(
            attachment=re_readable,  # type: ignore[arg-type]
            emotion_id=self.emotion_id,
            suggested_by=interaction.user,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
            source_message_jump_url=None,
        )
        if not ok:
            await interaction.followup.send(f"❌ {err_or_filename}", ephemeral=True)
            return

        self.cog.seen_emotion_ids.add(self.emotion_id)
        self.cog.save_config()

        await interaction.followup.send(
            f"✅ Saved `{err_or_filename}` and sent to admins for approval for emotion_id `{self.emotion_id}`.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"UploadEmotionModal on_error: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ Upload failed: {error}", ephemeral=True
                )
        except Exception:
            pass


class AdminEmotionConfirmView(LayoutView):
    """Persistent Components V2 view that admins use to approve or reject a proposed emotion.

    `emotion_id` is the numeric/string ID from the game. The approve flow copies
    the pending file to `data/emotion/{emotion_id}.{ext}`.
    """

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        emotion_id: str,
        pending_filename: str,
        target_filename: str,
        suggested_by_id: Optional[int],
        source_message_jump_url: Optional[str],
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.emotion_id = emotion_id
        self.pending_filename = pending_filename
        self.target_filename = target_filename
        self.suggested_by_id = suggested_by_id
        self.source_message_jump_url = source_message_jump_url

        self.action_row = ActionRow()
        approve_btn = Button(
            label="✅ Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"emotion_admin_approve:{emotion_id}:{pending_filename}",
        )
        approve_btn.callback = self._on_approve
        self.action_row.add_item(approve_btn)

        reject_btn = Button(
            label="❌ Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"emotion_admin_reject:{emotion_id}",
        )
        reject_btn.callback = self._on_reject
        self.action_row.add_item(reject_btn)

    def _disable(self) -> None:
        for item in self.action_row.children:
            if isinstance(item, Button):
                item.disabled = True

    async def _on_approve(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        source_path = EMOTION_DIR / "pending" / self.pending_filename
        target_path = EMOTION_DIR / self.target_filename
        if not source_path.exists() or not source_path.is_file():
            logger.error(f"Source emotion missing on disk: {source_path}")
            self._disable()
            err_container = Container(
                TextDisplay(
                    f"❌ Source file `data/emotion/pending/{self.pending_filename}` is missing on disk."
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

        try:
            shutil.copy2(str(source_path), str(target_path))
            # Remove the pending file so it doesn't get re-listed
            try:
                source_path.unlink()
            except OSError:
                pass
        except Exception as e:
            logger.error(f"Failed to copy emotion for emotion_id {self.emotion_id}: {e}")
            self._disable()
            err_container = Container(
                TextDisplay(
                    f"❌ Failed to copy `{self.pending_filename}` → "
                    f"`data/emotion/{self.target_filename}`: {e}"
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

        # Mark as seen so we don't re-prompt
        self.cog.seen_emotion_ids.add(self.emotion_id)
        self.cog.save_config()

        summary_lines = [
            f"✅ **Approved** by {interaction.user.mention}",
            "",
            f"• `emotion_id`: **{self.emotion_id}**",
            f"• Saved to: `data/emotion/{self.target_filename}`",
        ]
        if self.suggested_by_id:
            summary_lines.append(f"• Suggested by: <@{self.suggested_by_id}>")
        if self.source_message_jump_url:
            summary_lines.append(f"• Original message: {self.source_message_jump_url}")
        summary_text = "\n".join(summary_lines)

        try:
            ok_container = Container(
                TextDisplay(summary_text),
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
            f"✅ Emotion approved: emotion_id={self.emotion_id} → {self.target_filename}"
        )

    async def _on_reject(self, interaction: discord.Interaction):
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)

        # Remove the pending file
        try:
            source_path = EMOTION_DIR / "pending" / self.pending_filename
            if source_path.exists():
                try:
                    source_path.unlink()
                except OSError:
                    pass
        except Exception:
            pass

        # Remove from seen_emotion_ids so we can re-prompt
        self.cog.seen_emotion_ids.discard(self.emotion_id)
        self.cog.save_config()

        summary_lines = [
            f"❌ **Rejected** by {interaction.user.mention}",
            "",
            f"• `emotion_id`: **{self.emotion_id}**",
            f"• Rejected file: `{self.pending_filename}`",
        ]
        if self.suggested_by_id:
            summary_lines.append(f"• Suggested by: <@{self.suggested_by_id}>")
        if self.source_message_jump_url:
            summary_lines.append(f"• Original message: {self.source_message_jump_url}")
        summary_text = "\n".join(summary_lines)

        try:
            reject_container = Container(
                TextDisplay(summary_text),
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
        logger.info(f"❌ Emotion rejected: emotion_id={self.emotion_id}")


class EmotionUploadView(LayoutView):
    """Wraps a normal chat message and adds a "Set Emote" button for unmapped emotions.

    Times out after 180 seconds. When clicked, opens the UploadEmotionModal.
    """

    PICKER_TIMEOUT = 180.0

    def __init__(
        self,
        *,
        base_view: ChatMessageView,
        emotion_id: str,
        sender_nickname: str,
        sender_pid: Optional[str],
    ):
        super().__init__(timeout=self.PICKER_TIMEOUT)
        self.base_view = base_view
        self.emotion_id = str(emotion_id)
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid

        for item in list(base_view.children):
            self.add_item(item)

        action_row = ActionRow()
        button = Button(
            label="😀 Set Emote",
            style=discord.ButtonStyle.primary,
            custom_id=f"emotion_upload:{self.emotion_id}",
        )
        button.callback = self._on_click
        action_row.add_item(button)
        self.add_item(action_row)

        self._files: List[discord.File] = list(getattr(base_view, "_files", []))

    def _resolve_files(self) -> List[discord.File]:
        return list(self._files)

    async def _on_click(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("LiveChatCog")
        if cog is None:
            await interaction.response.send_message("❌ LiveChatCog is not loaded.", ephemeral=True)
            return

        await interaction.response.send_modal(
            UploadEmotionModal(
                cog=cog,
                emotion_id=self.emotion_id,
                sender_nickname=self.sender_nickname,
                sender_pid=self.sender_pid,
            )
        )

    async def on_timeout(self) -> None:
        bt_part = self.emotion_id
        target_custom_id = f"emotion_upload:{bt_part}"
        rows_to_remove = [
            child for child in self.children
            if isinstance(child, ActionRow)
            and any(isinstance(item, Button) and item.custom_id == target_custom_id
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


class DuplicateCheckView(LayoutView):
    """Ephemeral Components V2 view that shows an uploaded file vs. an existing local file
    side-by-side and asks the user if they are the same.

    If the user confirms they are the same, the existing file is treated as sufficient
    and the upload is skipped (no duplicate created). If the user says they are different,
    the normal upload+approval flow proceeds.
    """

    def __init__(
        self,
        *,
        cog: "LiveChatCog",
        uploaded_data: bytes,
        uploaded_filename: str,
        existing_filename: str,
        head_id: str,
        body_type: Optional[int],
        suggested_by: discord.abc.User,
        sender_nickname: str,
        sender_pid: Optional[str],
        source_message_jump_url: Optional[str],
        subfolder: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.uploaded_data = uploaded_data
        self.uploaded_filename = uploaded_filename
        self.existing_filename = existing_filename
        self.head_id = head_id
        self.body_type = body_type
        self.suggested_by = suggested_by
        self.sender_nickname = sender_nickname
        self.sender_pid = sender_pid
        self.source_message_jump_url = source_message_jump_url
        self.subfolder = subfolder
        self._build()

    def _build(self) -> None:
        self.clear_items()

        existing_path = AVATARS_DIR / self.existing_filename
        existing_basename = existing_path.name if existing_path.exists() else self.existing_filename

        inner: list = [
            TextDisplay(
                f"# 🔍 Possible Duplicate Detected\n\n"
                f"A file with a similar name already exists in `data/avatars/{self.subfolder}/`.\n\n"
                f"**Uploaded:** `{self.uploaded_filename}`\n"
                f"**Existing:** `{existing_basename}`\n\n"
                f"Are these two files the same image? If so, the duplicate can be skipped."
            ),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay("**Uploaded file (left) — Existing file (right):**"),
        ]

        # Gallery with both images side by side: uploaded first, existing second
        gallery = MediaGallery()
        # Uploaded attachment name (ephemeral view will send both as attachments)
        gallery.add_item(
            media=f"attachment://dupe_uploaded_{self.uploaded_filename}",
            description=f"Uploaded: {self.uploaded_filename}",
        )
        if existing_path.exists():
            gallery.add_item(
                media=f"attachment://dupe_existing_{existing_basename}",
                description=f"Existing: {existing_basename}",
            )
        inner.append(gallery)
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Action buttons
        action_row = ActionRow()
        yes_btn = Button(
            label="✅ Yes, same — skip duplicate",
            style=discord.ButtonStyle.success,
            custom_id="duplicate_yes_same",
        )
        yes_btn.callback = self._on_yes_same
        action_row.add_item(yes_btn)

        no_btn = Button(
            label="❌ No, different — proceed upload",
            style=discord.ButtonStyle.primary,
            custom_id="duplicate_no_different",
        )
        no_btn.callback = self._on_no_different
        action_row.add_item(no_btn)

        inner.append(action_row)
        container = Container(*inner, accent_color=0xE67E22)
        self.add_item(container)

    def _resolve_files(self) -> List[discord.File]:
        """Return both the uploaded and existing file as discord.File objects for attachment."""
        files: List[discord.File] = []
        # Uploaded file bytes
        files.append(
            discord.File(
                io.BytesIO(self.uploaded_data),
                filename=f"dupe_uploaded_{self.uploaded_filename}",
            )
        )
        # Existing file from disk
        existing_path = AVATARS_DIR / self.existing_filename
        if existing_path.exists():
            files.append(
                discord.File(
                    str(existing_path),
                    filename=f"dupe_existing_{existing_path.name}",
                )
            )
        return files

    async def _on_yes_same(self, interaction: discord.Interaction):
        """User confirmed the files are the same — map the existing local file instead of uploading a duplicate."""
        # Disable all controls
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, Button):
                        item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # The existing file is already on disk; send it to admin approval for mapping
        await self.cog._send_admin_avatar_approval(
            head_id=self.head_id,
            chosen_filename=self.existing_filename,
            body_type=self.body_type,
            suggested_by=self.suggested_by,
            source_message_jump_url=self.source_message_jump_url,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
        )

        # Mark (head_id, body_type) as seen so the picker won't re-prompt
        if self.head_id and self.body_type is not None:
            self.cog.seen_head_map.add((self.head_id, self.body_type))
            self.cog.save_config()

        try:
            await interaction.followup.send(
                f"✅ Confirmed as duplicate. The existing file `{self.existing_filename}` "
                f"has been sent to admins for approval for head_id `{self.head_id}`.",
                ephemeral=True,
            )
        except Exception:
            pass
        self.stop()

    async def _on_no_different(self, interaction: discord.Interaction):
        """User said the files are different — proceed with normal upload + admin approval."""
        # Disable all controls
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, Button):
                        item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # Continue with the normal upload flow: save the file and send for admin approval
        target_dir = AVATARS_DIR / self.subfolder
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to mkdir {target_dir}: {e}")
            try:
                await interaction.followup.send(
                    f"❌ Could not create target folder: {e}", ephemeral=True
                )
            except Exception:
                pass
            return

        # Determine extension from the uploaded filename
        ext = ".png"
        if self.uploaded_filename.lower().endswith(".webp"):
            ext = ".webp"
        raw_name = _re.sub(r"[^A-Za-z0-9._-]", "_", self.uploaded_filename)
        if not raw_name.lower().endswith(ext):
            raw_name = raw_name + ext

        target_path = target_dir / raw_name
        if target_path.exists():
            stem, ext_only = os.path.splitext(raw_name)
            n = 1
            while True:
                cand = target_dir / f"{stem}_{n}{ext_only}"
                if not cand.exists():
                    target_path = cand
                    raw_name = cand.name
                    break
                n += 1

        try:
            target_path.write_bytes(self.uploaded_data)
        except Exception as e:
            logger.error(f"Failed to write uploaded file {target_path}: {e}", exc_info=True)
            try:
                await interaction.followup.send(
                    f"❌ Could not write the file to disk: {e}", ephemeral=True
                )
            except Exception:
                pass
            return

        chosen_relative = f"{self.subfolder}/{raw_name}"
        await self.cog._send_admin_avatar_approval(
            head_id=self.head_id,
            chosen_filename=chosen_relative,
            body_type=self.body_type,
            suggested_by=self.suggested_by,
            source_message_jump_url=self.source_message_jump_url,
            sender_nickname=self.sender_nickname,
            sender_pid=self.sender_pid,
        )

        try:
            await interaction.followup.send(
                f"✅ Saved `{raw_name}` to `data/avatars/{self.subfolder}/` and "
                f"sent to admins for approval for head_id `{self.head_id}`.",
                ephemeral=True,
            )
        except Exception:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, Button):
                        item.disabled = True
        self.stop()


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
        # (head_id_str, body_type) pairs we've already prompted the user to map.
        # Keyed by gender so a male sender with head_id X and a female sender
        # with the same head_id X can each be prompted once independently.
        # (`None` body_type is also a valid key for messages missing the field.)
        self.seen_head_map: Set[Tuple[str, Optional[int]]] = set()
        # Cached list of avatar filenames for the picker UI, keyed by body_type
        # (0/1/None). The first time a particular body_type is requested we
        # populate it; subsequent calls reuse it unless `force_refresh=True`.
        self._avatar_files_cache: dict = {}  # body_type -> Optional[List[str]]
        # emotion_id strings we've already prompted the user to map.
        self.seen_emotion_ids: Set[str] = set()

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
        self.DISCORD_FORWARD_KEYWORD = "#discord"      # Forward message to teamup channel

        # Ensure data directory exists
        os.makedirs("data", exist_ok=True)
        # Make sure the emotion dirs exist
        try:
            EMOTION_DIR.mkdir(parents=True, exist_ok=True)
            EMOTION_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create emotion subfolder: {e}")
        # Make sure the 6 avatar source subfolders + the 6 mapped subfolders
        # exist on disk so the picker and lookup logic never trip over a
        # missing directory.
        for d in (
            AVATARS_STILL_MALE_DIR, AVATARS_ANIMATED_MALE_DIR,
            AVATARS_STILL_FEMALE_DIR, AVATARS_ANIMATED_FEMALE_DIR,
            AVATARS_STILL_SHARED_DIR, AVATARS_ANIMATED_SHARED_DIR,
            AVATARS_MAPPED_STILL_MALE_DIR, AVATARS_MAPPED_ANIMATED_MALE_DIR,
            AVATARS_MAPPED_STILL_FEMALE_DIR, AVATARS_MAPPED_ANIMATED_FEMALE_DIR,
            AVATARS_MAPPED_STILL_SHARED_DIR, AVATARS_MAPPED_ANIMATED_SHARED_DIR,
        ):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not create avatar subfolder {d}: {e}")

        
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
            chat_result = await get_club_chat(self.CLUB_ID, self.HOSTNUM)
            
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
                self.ranks = await get_custom_guild_info(self.CLUB_ID, self.HOSTNUM, {'members': ['custom_posts']})
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
                translation = self.translator.translate(text, src=src, dest=dest)
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
                film_data = await get_film_plan(plan_id)
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
                team_data = await get_teams_info(team_hostnum, team_id)
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
                        bulk_result = await get_bulk_players_info(pids, ["base", "kongfu"], m_hostnum)
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
                bulk_data = await get_bulk_players_info([sender_pid], ["base", "team"], sender_hostnum)
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
                            team_result = await get_teams_info(team_hostnum, team_id)
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
                                        bulk_result = await get_bulk_players_info(pids, ["base", "kongfu"], m_hostnum)
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

    async def discord_forward_alert(
        self,
        teamup_channel: discord.TextChannel,
        view: LayoutView,
        files: List[discord.File],
    ) -> None:
        """Forward live chat message to teamup channel when #discord keyword is used."""
        try:
            await teamup_channel.send(
                view=view,
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.debug(f"Forwarded #discord message to teamup channel")
        except Exception as e:
            logger.error(f"Failed to forward #discord message: {e}")

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
        """Load saved configuration from file.

        Migrates the legacy `seen_head_ids: List[str]` field (pre body-type
        support) into the new `seen_head_map: Set[Tuple[str, Optional[int]]]`
        by registering each flat entry for BOTH body_type 0 and 1 — the
        conservative assumption is that the original mapping was intended as
        "shared" (works for both genders), so we just don't re-prompt.
        """
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.CHANNEL_ID = config.get('channel_id')
                    self.is_running = config.get('enabled', False)
                    self.last_seen_msg_ids = set(config.get('last_msg_ids', []))
                    self._last_seen_npc_ts = config.get('last_npc_ts', 0)
                    self._last_seen_max_ts = config.get('last_max_ts', 0)

                    # --- Migrate seen_head_ids → seen_head_map ---
                    # 1) Read the legacy flat list (Set[str]). The old entries had
                    #    no gender information, so we register them as (head_id, None)
                    #    which means "may have been handled but we don't know which
                    #    gender" — these do NOT suppress the picker for any gender.
                    legacy_seen = config.get('seen_head_ids', []) or []
                    for h in legacy_seen:
                        head_str = str(h)
                        if head_str:
                            self.seen_head_map.add((head_str, None))
                    # 2) Read the new (head_id, body_type) list, format is
                    #    "head_id_str|body_type_int" (body_type_int may be
                    #    the string "0", "1", or "" for None).
                    for entry in config.get('seen_head_map', []) or []:
                        try:
                            if '|' in entry:
                                head_part, bt_part = entry.split('|', 1)
                                bt: Optional[int] = int(bt_part) if bt_part not in ("", "None") else None
                            else:
                                # No separator — treat as legacy flat entry.
                                head_part, bt = str(entry), None
                            if head_part:
                                self.seen_head_map.add((str(head_part), bt))
                        except (TypeError, ValueError):
                            continue
                    # ── Cleanup: remove spurious gender entries ──
                    # The legacy migration used to add BOTH genders for each old
                    # flat head_id entry. If a head_id now has both (0) and (1)
                    # in seen_head_map but only one gender actually has a mapped
                    # avatar file on disk, drop the incorrect entry so the
                    # missing-gender picker button can appear again.
                    head_ids_with_both = {}
                    for h, bt in self.seen_head_map:
                        if bt not in (0, 1):
                            continue
                        head_ids_with_both.setdefault(h, set()).add(bt)
                    for h, bts in head_ids_with_both.items():
                        if bts != {0, 1}:
                            continue
                        # Both genders present — check which ones actually have files
                        has_female = self._avatar_path(h, BODY_TYPE_FEMALE) is not None
                        has_male = self._avatar_path(h, BODY_TYPE_MALE) is not None
                        if has_female and not has_male:
                            self.seen_head_map.discard((h, BODY_TYPE_MALE))
                        elif has_male and not has_female:
                            self.seen_head_map.discard((h, BODY_TYPE_FEMALE))
                        # If neither or both have files, leave as-is (both mapped or both missing)
                    # 3) Read seen_emotion_ids (flat list of emotion_id strings).
                    for eid in config.get('seen_emotion_ids', []) or []:
                        eid_str = str(eid).strip()
                        if eid_str:
                            self.seen_emotion_ids.add(eid_str)
                logger.debug(
                    f"Loaded live chat config: enabled={self.is_running}, "
                    f"channel={self.CHANNEL_ID}, last_max_ts={self._last_seen_max_ts}, "
                    f"seen_head_map={len(self.seen_head_map)}, "
                    f"seen_emotion_ids={len(self.seen_emotion_ids)}"
                )
            except Exception as e:
                logger.error(f"Failed to load live chat config: {str(e)}")

    def save_config(self):
        """Save current configuration to file.

        Persists the new `seen_head_map` (as a list of "head_id|body_type"
        strings) AND keeps writing the legacy `seen_head_ids` list (flat
        head_ids only) so any external tools that read it keep working.
        """
        try:
            # Strip legacy (head_id, None) placeholders before persisting — they
            # were only meaningful during migration and should not accumulate.
            concrete_entries = {(h, bt) for (h, bt) in self.seen_head_map if bt is not None}
            # Flat view of seen_head_map for the legacy field.
            legacy_flat = sorted({h for (h, _bt) in concrete_entries})
            seen_head_map_serialized = sorted(
                f"{h}|{'' if bt is None else bt}" for (h, bt) in concrete_entries
            )
            config = {
                'channel_id': self.CHANNEL_ID,
                'enabled': self.is_running,
                'last_msg_ids': list(self.last_seen_msg_ids)[-300:],
                'last_npc_ts': self._last_seen_npc_ts,
                'last_max_ts': self._last_seen_max_ts,
                'seen_head_ids': legacy_flat,
                'seen_head_map': seen_head_map_serialized,
                'seen_emotion_ids': sorted(self.seen_emotion_ids),
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
        local file yet) and the @teamup alert dispatch. The sender's
        `body_type` (0=female, 1=male) is threaded through to the head
        avatar lookup + picker offer so male and female senders with the
        same `head_id` are handled independently.
        """
        ext = msg.get("ext", {}) or {}
        msg_type = ext.get("msg_type", "msg_normal")
        msg_label = (msg.get("msg", "") or "").strip()
        ts = int(msg.get("ts", 0) or 0)
        nickname = msg.get("nickname", "Unknown")
        level = msg.get("level", 0) or 0
        sender_pid = msg.get("from_pid", None)
        head_id = msg.get("head_id", None)
        # Body type of the sender (0=female, 1=male). Falls back to None
        # if the API doesn't return it for this message shape.
        raw_body_type = msg.get("body_type", None)
        body_type: Optional[int] = raw_body_type if raw_body_type in (0, 1) else None

        rank_name = self._get_rank_name(sender_pid)
        author_name = (
            f"{nickname} ({rank_name}) (Lv.{level})"
            if rank_name != "Unknown"
            else f"{nickname} (Lv.{level})"
        )
        discord_mention = self._get_discord_mention(sender_pid)
        # Body-type aware lookup: male/female senders may have different
        # avatar mappings under their respective subfolders.
        head_avatar_path = self._avatar_path(head_id, body_type)
        channel_type = msg.get("channel", "club_chat")
        accent_color = {
            "club_chat": 0x2ECC71,
            "officer_chat": 0xE67E22,
            "private": 0x9B59B6,
        }.get(channel_type, 0x3498DB)

        view: Optional[LayoutView] = None
        files: List[discord.File] = []
        handled_separately = False  # True if emotion / exhibition took ownership

        # ── Emotion messages (custom emote PNG/WEBP) ──
        if msg_type == "msg_emotion":
            emotion_id = ext.get("emotion_id")
            if emotion_id:
                emotion_path = None
                for ext_try in (".png", ".webp"):
                    candidate = f"data/emotion/{emotion_id}{ext_try}"
                    if os.path.exists(candidate):
                        emotion_path = candidate
                        break
                if emotion_path:
                    view = EmotionMessageView(
                        author_name=author_name,
                        ts=ts,
                        discord_mention=discord_mention,
                        emotion_id=emotion_id,
                        emotion_path=emotion_path,
                        # Pass head_id/avatar so emotes also display the
                        # mapped avatar thumbnail in a Section, matching
                        # the ChatMessageView layout.
                        head_id=str(head_id) if head_id is not None else None,
                        head_avatar_path=head_avatar_path,
                    )
                    files = view._resolve_files()
                    handled_separately = True
                elif self._should_offer_emotion_upload(str(emotion_id)):
                    # Unmapped emotion → offer the upload button
                    base_view = ChatMessageView(
                        author_name=author_name,
                        body_text=(ext.get("emotion_msg") or "").strip() or f"*[Emotion {emotion_id}]*",
                        ts=ts,
                        discord_mention=discord_mention,
                        head_id=str(head_id) if head_id is not None else None,
                        head_avatar_path=head_avatar_path,
                        accent_color=0x9B59B6,
                    )
                    view = EmotionUploadView(
                        base_view=base_view,
                        emotion_id=str(emotion_id),
                        sender_nickname=nickname,
                        sender_pid=sender_pid,
                    )
                    files = view._resolve_files()
                    handled_separately = True

        # ── Exhibition (dance video) messages ──
        if not handled_separately and msg_type == "msg_artwork_card" and msg_label == "[Exhibition]":
            artwork_data = ext.get("extra_data", {}).get("artwork_data", {}) or {}
            plan_id = artwork_data.get("plan_id", "") or ""
            if plan_id:
                film_data = await get_film_plan(plan_id)
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
            # Skip messages with no meaningful content (e.g. empty
            # hongbao_auto_reply_msg where body_text is stripped to "").
            if not body_text and not picture_url:
                return
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
            and self._should_offer_avatar_picker(head_id, body_type)
            and isinstance(view, ChatMessageView)
        ):
            view = HeadPickerRequestView(
                base_view=view,
                head_id=head_id,
                sender_nickname=nickname,
                sender_pid=sender_pid,
                body_type=body_type,
            )
            files = view._resolve_files()

        try:
            sent_message = await channel.send(
                view=view,
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Stash a reference to the live message on views that need to
            # edit themselves on timeout (button removal).
            if isinstance(view, HeadPickerRequestView):
                view.message = sent_message
            if isinstance(view, EmotionUploadView):
                view.message = sent_message
        except Exception as e:
            logger.error(f"Failed to send V2 message: {e}", exc_info=True)
            return

        # Check for @teamup keyword
        raw_msg = (msg.get("msg", "") or "").strip().lower()
        if teamup_channel is not None and self.TEAMUP_KEYWORD in raw_msg:
            await self.send_teamup_alert(msg, teamup_channel)

        # Check for #discord keyword - forward the live message to teamup channel
        if teamup_channel is not None and self.DISCORD_FORWARD_KEYWORD in raw_msg:
            # Re-create File objects since they are consumed by channel.send() —
            # discord.File reads from its fp during send and the underlying
            # stream is exhausted. After send, f.fp is a closed BufferedReader
            # whose .name attribute still holds the original file path string.
            fresh_files: List[discord.File] = []
            for f in files:
                fp = f.fp
                # After send, fp is a closed BufferedReader; its .name is the
                # original path string we passed to discord.File(path, ...).
                if fp is not None and hasattr(fp, "name") and isinstance(fp.name, str):
                    try:
                        fresh_files.append(discord.File(fp.name, filename=f.filename))
                    except Exception:
                        fresh_files.append(f)
                else:
                    fresh_files.append(f)
            await self.discord_forward_alert(teamup_channel, view, fresh_files)

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

    def _avatar_path(self, head_id, body_type: Optional[int] = None) -> Optional[str]:
        """Return absolute path to a local avatar for the given (head_id, body_type), or None.

        Lookup priority (most preferred → least preferred):
            body_type=0 (female): still_female → animated_female → still_shared → animated_shared
            body_type=1 (male):   still_male   → animated_male   → still_shared → animated_shared
            body_type=None:       still_shared → animated_shared

        Legacy fallback: if no file is found in any of the 6 mapped subfolders,
        also check the flat `data/avatars/mapped/{head_id}.{ext}` path so any
        pre-existing flat-style mappings keep working until the user migrates
        them into the new subfolders.
        """
        if head_id is None:
            return None
        head_str = str(head_id).strip()
        if not head_str:
            return None

        # Determine which subfolders to search, in priority order.
        if body_type in (0, 1):
            search_dirs = AVATARS_MAPPED_LOOKUP_ORDER_BY_BODY_TYPE[body_type]
        else:
            # Unknown / missing body_type → only shared.
            search_dirs = [
                AVATARS_MAPPED_STILL_SHARED_DIR,
                AVATARS_MAPPED_ANIMATED_SHARED_DIR,
            ]

        for d in search_dirs:
            for ext in (".png", ".webp"):
                candidate = d / f"{head_str}{ext}"
                if candidate.exists() and candidate.is_file():
                    return str(candidate)

        # Legacy flat-file fallback. Existing files like
        # data/avatars/mapped/1037.webp keep working until you move them
        # into the appropriate subfolder.
        for ext in (".png", ".webp"):
            candidate = AVATARS_MAPPED_DIR / f"{head_str}{ext}"
            if candidate.exists() and candidate.is_file():
                return str(candidate)

        return None

    def _list_avatar_files(
        self,
        body_type: Optional[int] = None,
        force_refresh: bool = False,
    ) -> List[str]:
        """Return a sorted list of source-file *relative paths* (e.g. `still_male/foo.png`)
        across the 6 avatar subfolders, ordered to prefer fast PNGs before
        animated WEBPs, and the sender's gender-specific subfolders before
        the shared ones.

        Returns *relative paths* under `AVATARS_DIR` so the picker can show
        them with their subfolder prefix in the dropdown labels and so the
        approve flow can infer which `mapped/<subfolder>/` destination to use.
        """
        cache_key = body_type  # 0/1/None
        if not force_refresh and self._avatar_files_cache.get(cache_key) is not None:
            return self._avatar_files_cache[cache_key]

        # Pick which subfolders to scan, in display order.
        if body_type in (0, 1):
            subdirs = AVATARS_SOURCE_SUBFOLDERS_BY_BODY_TYPE[body_type]
        else:
            # Unknown body_type → only the still/animated shared subfolders.
            subdirs = [AVATARS_STILL_SHARED_DIR, AVATARS_ANIMATED_SHARED_DIR]

        files: List[str] = []
        for d in subdirs:
            if not d.is_dir():
                continue
            try:
                png_files: List[str] = []
                webp_files: List[str] = []
                for f in os.listdir(str(d)):
                    if f.startswith("."):
                        continue  # .done_*, .DS_Store, etc.
                    name_lower = f.lower()
                    if name_lower.endswith('.png'):
                        if (d / f".done_{f}").exists():
                            continue
                        png_files.append(f)
                    elif name_lower.endswith('.webp'):
                        if (d / f".done_{f}").exists():
                            continue
                        webp_files.append(f)
                png_files.sort(key=str.lower)
                webp_files.sort(key=str.lower)
                # Prefix the subfolder name to make each entry a relative path
                # like `still_male/foo.png`. This is what the picker stores
                # in its `avatar_files` list and what `_load_page_files` later
                # resolves back to a full path via AVATARS_DIR / entry.
                subdir_name = d.name
                files.extend(f"{subdir_name}/{f}" for f in png_files)
                files.extend(f"{subdir_name}/{f}" for f in webp_files)
            except Exception as e:
                logger.error(f"Failed to list avatar files in {d}: {e}")
                continue

        self._avatar_files_cache[cache_key] = files
        return files


    def _list_avatar_files_in_subfolder(
        self,
        subfolder: str,
        force_refresh: bool = False,
    ) -> List[str]:
        """Return a sorted list of source-file *relative paths* from a SINGLE
        avatar subfolder (e.g. ``still_male``).

        This is the "category picker" counterpart of ``_list_avatar_files``:
        instead of scanning every subfolder, it only scans the one the
        user picked from the CategoryPickerView dropdown. That way the
        Set Avatar button never makes Discord pay for preloading images
        the user isn't going to look at.

        Returns *relative paths* under ``AVATARS_DIR`` in the same
        ``<subfolder>/<name>.<ext>`` format used everywhere else in the
        picker / approval flow.
        """
        if subfolder not in AVATAR_VALID_SUBFOLDERS:
            return []
        # Per-subfolder cache so subsequent re-opens (e.g. user switches
        # back to the same category) are instant. Keyed by the subfolder
        # name itself so it never collides with the body_type-keyed cache.
        per_sub_cache_attr = "_avatar_files_by_subfolder"
        per_sub_cache: dict = getattr(self, per_sub_cache_attr, None)
        if per_sub_cache is None:
            per_sub_cache = {}
            setattr(self, per_sub_cache_attr, per_sub_cache)

        if not force_refresh and per_sub_cache.get(subfolder) is not None:
            return per_sub_cache[subfolder]

        # Map subfolder string to its directory constant.
        subfolder_dir_map = {
            "still_male": AVATARS_STILL_MALE_DIR,
            "animated_male": AVATARS_ANIMATED_MALE_DIR,
            "still_female": AVATARS_STILL_FEMALE_DIR,
            "animated_female": AVATARS_ANIMATED_FEMALE_DIR,
            "still_shared": AVATARS_STILL_SHARED_DIR,
            "animated_shared": AVATARS_ANIMATED_SHARED_DIR,
        }
        d = subfolder_dir_map.get(subfolder)
        if d is None or not d.is_dir():
            per_sub_cache[subfolder] = []
            return []

        files: List[str] = []
        try:
            png_files: List[str] = []
            webp_files: List[str] = []
            for f in os.listdir(str(d)):
                if f.startswith("."):
                    continue
                name_lower = f.lower()
                if name_lower.endswith('.png'):
                    if (d / f".done_{f}").exists():
                        continue
                    png_files.append(f)
                elif name_lower.endswith('.webp'):
                    if (d / f".done_{f}").exists():
                        continue
                    webp_files.append(f)
            png_files.sort(key=str.lower)
            webp_files.sort(key=str.lower)
            files.extend(f"{subfolder}/{f}" for f in png_files)
            files.extend(f"{subfolder}/{f}" for f in webp_files)
        except Exception as e:
            logger.error(f"Failed to list avatar files in {d}: {e}")

        per_sub_cache[subfolder] = files
        return files


    async def _cleanup_done_markers(self):
        """Background task: try to delete source avatars that have .done_ markers.

        Walks the legacy `data/avatars/` root (for any pre-existing flat files)
        AND the 6 new source subfolders. Once the OS releases its file lock
        (e.g. Explorer finishes indexing), the file can finally be removed.
        Also tries to remove the marker itself. Runs every 30 seconds.
        """
        # Build the list of directories to scan: legacy root + the 6 subfolders.
        scan_dirs: List["Path"] = [AVATARS_DIR]
        scan_dirs.extend(AVATARS_ALL_SOURCE_SUBFOLDERS)
        for d in scan_dirs:
            try:
                if not d.is_dir():
                    continue
                for f in os.listdir(str(d)):
                    if not f.startswith(".done_"):
                        continue
                    # Derive the original source filename from the marker name
                    source_name = f[6:]  # strip ".done_" prefix
                    source_path = d / source_name
                    done_path = d / f
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
                    logger.debug(f"Cleaned up approved avatar: {d.name}/{source_name}")
            except Exception:
                # Swallow per-folder errors so one bad folder doesn't stop the loop
                continue

    @staticmethod
    def _normalize_filename_for_comparison(filename: str) -> str:
        """Normalize a filename for fuzzy comparison.

        Converts spaces/underscores/hyphens to a consistent separator,
        lowercases, and strips the extension. This catches cases where
        Discord auto-replaces spaces with underscores on upload.
        """
        name = os.path.splitext(filename)[0]
        normalized = name.lower()
        normalized = _re.sub(r'[\s_\-]+', '_', normalized)
        return normalized

    @staticmethod
    def _find_similar_existing_file(
        uploaded_filename: str,
        subfolder: str,
        filenames_from_subfolder: Optional[List[str]] = None,
        similarity_threshold: float = 0.7,
    ) -> Optional[str]:
        """Check if an uploaded filename is similar to any existing file in the subfolder.

        Uses normalized comparison (space->underscore via
        ``_normalize_filename_for_comparison``) and falls back to
        ``difflib.SequenceMatcher`` ratio for fuzzy matching.
        """
        up_normalized = LiveChatCog._normalize_filename_for_comparison(uploaded_filename)

        # First, try exact match after normalization (catches space vs underscore)
        for existing in (filenames_from_subfolder or []):
            ex_normalized = LiveChatCog._normalize_filename_for_comparison(os.path.basename(existing))
            if up_normalized == ex_normalized:
                return existing

        # Then try fuzzy ratio matching
        for existing in (filenames_from_subfolder or []):
            ex_normalized = LiveChatCog._normalize_filename_for_comparison(os.path.basename(existing))
            ratio = difflib.SequenceMatcher(None, up_normalized, ex_normalized).ratio()
            if ratio >= similarity_threshold:
                return existing

        return None

    def _emotion_path(self, emotion_id: str) -> Optional[str]:
        """Return absolute path to a local emotion file, or None."""
        if not emotion_id:
            return None
        eid = str(emotion_id).strip()
        if not eid:
            return None
        for ext in (".png", ".webp"):
            candidate = EMOTION_DIR / f"{eid}{ext}"
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        return None

    def _should_offer_emotion_upload(self, emotion_id: str) -> bool:
        """True if this is an unmapped emotion_id worth offering the upload for."""
        if not emotion_id:
            return False
        eid = str(emotion_id).strip()
        if not eid:
            return False
        # Already on disk
        if self._emotion_path(eid) is not None:
            return False
        # Already prompted
        if eid in self.seen_emotion_ids:
            return False
        return True

    async def _stage_emotion_upload(
        self,
        *,
        attachment: discord.Attachment,
        emotion_id: str,
        suggested_by: discord.abc.User,
        sender_nickname: str,
        sender_pid: Optional[str],
        source_message_jump_url: Optional[str],
    ) -> Tuple[bool, str]:
        """Stage an uploaded emotion file and route to admin approval.

        Returns `(True, saved_filename)` on success or `(False, error_message)`.
        """
        eid = str(emotion_id).strip()
        if not eid:
            return False, "Invalid emotion_id"

        try:
            data = await attachment.read()
        except Exception as e:
            logger.error(f"Failed to read emotion upload: {e}", exc_info=True)
            return False, f"Failed to read file: {e}"
        if len(data) > 10 * 1024 * 1024:
            return False, "File too large (max 10 MB)"

        ext = None
        fn_lower = attachment.filename.lower()
        if fn_lower.endswith(".png"):
            ext = ".png"
        elif fn_lower.endswith(".webp"):
            ext = ".webp"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        if ext is None:
            return False, "Not a recognized PNG/WEBP"

        target_filename = f"{eid}{ext}"
        target_path = EMOTION_DIR / "pending" / target_filename
        try:
            EMOTION_PENDING_DIR.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(data)
        except Exception as e:
            logger.error(f"Failed to write emotion file {target_path}: {e}", exc_info=True)
            return False, f"Could not save file: {e}"

        await self._send_admin_emotion_approval(
            emotion_id=eid,
            pending_filename=target_filename,
            suggested_by=suggested_by,
            source_message_jump_url=source_message_jump_url,
            sender_nickname=sender_nickname,
            sender_pid=sender_pid,
        )
        return True, target_filename

    async def _send_admin_emotion_approval(
        self,
        *,
        emotion_id: str,
        pending_filename: str,
        suggested_by: discord.abc.User,
        source_message_jump_url: Optional[str],
        sender_nickname: str,
        sender_pid: Optional[str],
    ) -> None:
        """Send admin approval request for an emotion upload."""
        channel = self.bot.get_channel(ADMIN_AVATAR_CHANNEL_ID)
        if channel is None:
            logger.error(f"Admin channel {ADMIN_AVATAR_CHANNEL_ID} not found")
            return

        source_path = EMOTION_DIR / "pending" / pending_filename
        if not source_path.exists():
            logger.error(f"Pending emotion missing: {source_path}")
            return

        target_filename = pending_filename  # same name, just moved from pending/
        ext = os.path.splitext(pending_filename)[1] or ".png"

        confirm_view = AdminEmotionConfirmView(
            cog=self,
            emotion_id=emotion_id,
            pending_filename=pending_filename,
            target_filename=target_filename,
            suggested_by_id=suggested_by.id if suggested_by else None,
            source_message_jump_url=source_message_jump_url,
        )

        with open(str(source_path), "rb") as f:
            file_bytes = f.read()
        file = discord.File(io.BytesIO(file_bytes), filename=pending_filename)

        info_lines = [
            f"**New emotion_id upload request**",
            f"• `emotion_id`: **{emotion_id}**",
            f"• Suggested by: {suggested_by.mention if suggested_by else 'unknown'}",
            f"• Sender nickname: **{sender_nickname}**" + (f" (PID: `{sender_pid}`)" if sender_pid else ""),
        ]
        if source_message_jump_url:
            info_lines.append(f"• Original message: {source_message_jump_url}")
        info_lines.append(
            f"\n📎 File: `{pending_filename}`"
            f"\n✅ Approve will copy to `data/emotion/{target_filename}`."
        )

        container = Container(
            TextDisplay("\n".join(info_lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay("**Proposed emotion:**"),
            accent_color=0xE67E22,
        )
        container.add_item(MediaGallery())
        container.children[-1].add_item(
            media=f"attachment://{pending_filename}",
            description=f"Proposed emotion for emotion_id {emotion_id}",
        )

        view = LayoutView(timeout=None)
        view.add_item(container)
        view.add_item(confirm_view.action_row)
        view._files = [file]

        try:
            await channel.send(
                view=view,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.info(f"📤 Emotion approval sent for emotion_id={emotion_id}")
        except Exception as e:
            logger.error(f"Failed to send emotion approval: {e}")

    def _should_offer_avatar_picker(self, head_id, body_type: Optional[int] = None) -> bool:
        """True if this is a new (unmapped) (head_id, body_type) worth offering the picker for."""
        if head_id is None or body_type not in (0, 1):
            return False
        head_str = str(head_id).strip()
        if not head_str:
            return False
        # Already on disk (in any of the 6 mapped subfolders, or via legacy fallback) -> nothing to do
        if self._avatar_path(head_str, body_type) is not None:
            return False
        # Already prompted once for this (head_id, body_type) -> don't keep spamming the button
        if (head_str, body_type) in self.seen_head_map:
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
        body_type: Optional[int] = None,
    ) -> None:
        """Send the admin-confirmation LayoutView to the configured mod channel.

        `chosen_filename` may be either a flat filename (`foo.png`) for legacy
        files in the `data/avatars/` root, OR a relative path
        (`still_male/foo.png`) for files inside one of the 6 source subfolders.
        The admin's `approve` action infers the destination `mapped/<sub>/`
        from the source's parent directory, so the file is stored in the
        right gender/format bucket for the body-type-aware resolver.
        """
        channel = self.bot.get_channel(ADMIN_AVATAR_CHANNEL_ID)
        if channel is None:
            logger.error(f"Admin avatar channel {ADMIN_AVATAR_CHANNEL_ID} not found")
            return

        # Resolve the source path. `chosen_filename` is a relative path
        # under AVATARS_DIR (e.g. "still_male/foo.png" or "foo.png").
        source_path = AVATARS_DIR / chosen_filename
        if not source_path.exists() or not source_path.is_file():
            logger.error(f"Chosen avatar file missing on disk: {source_path}")
            try:
                await channel.send(
                    f"❌ Avatar approval failed: file `{chosen_filename}` is missing on disk."
                )
            except Exception:
                pass
            return

        # Determine target extension from the SOURCE filename (e.g. `.png` or
        # `.webp`). Note: this strips the subfolder prefix if present.
        source_basename = source_path.name
        target_ext = ".png"
        if source_basename.lower().endswith(".webp"):
            target_ext = ".webp"
        head_filename = f"{head_id}{target_ext}"

        # The display name of the file we attach to the admin message. We
        # strip the subfolder prefix for a cleaner attachment name in the
        # admin's UI.
        attach_filename = source_basename

        # Compute the destination display string for the info embed.
        source_subfolder = AdminConfirmView._infer_source_subfolder(chosen_filename)
        if source_subfolder and source_subfolder in AVATAR_VALID_SUBFOLDERS:
            target_display = f"mapped/{source_subfolder}/{head_filename}"
        else:
            # Legacy flat file → destination is the mapped/ root.
            target_display = f"mapped/{head_filename}"

        confirm_view = AdminConfirmView(
            cog=self,
            head_id=head_id,
            head_target_filename=head_filename,
            chosen_filename=chosen_filename,
            suggested_by_id=suggested_by.id if suggested_by else None,
            source_message_jump_url=source_message_jump_url,
            body_type=body_type,
        )

        # Read the file into a BytesIO so the disk file is closed immediately.
        # This is important because on Windows the file lock would otherwise
        # block the copy later in the approve flow.
        with open(str(source_path), "rb") as _f:
            file_bytes = _f.read()
        file = discord.File(io.BytesIO(file_bytes), filename=attach_filename)

        # Build info lines
        info_lines = [
            f"**New head_id avatar request**",
            f"• `head_id`: **{head_id}**",
        ]
        if body_type is not None:
            info_lines.append(
                f"• `body_type`: **{'Male' if body_type == 1 else 'Female'}**"
            )
        info_lines.append(f"• Suggested by: {suggested_by.mention if suggested_by else 'unknown'}")
        info_lines.append(
            f"• Sender nickname: **{sender_nickname}**"
            + (f" (PID: `{sender_pid}`)" if sender_pid else "")
        )
        if source_message_jump_url:
            info_lines.append(f"• Original message: {source_message_jump_url}")
        info_lines.append(
            f"\n📎 Chosen file: `data/avatars/{chosen_filename}`"
            f"\n✅ Approve will copy it to `data/avatars/{target_display}`."
        )

        container = Container(
            TextDisplay("\n".join(info_lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay("**Proposed avatar:**"),
            accent_color=0xE67E22,
        )
        container.add_item(MediaGallery())
        container.children[-1].add_item(
            media=f"attachment://{attach_filename}",
            description=f"Proposed avatar for head_id {head_id}",
        )

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
                f"📤 Avatar approval request sent to {channel} for "
                f"head_id={head_id} (body_type={body_type}), file={chosen_filename}"
            )
        except Exception as e:
            logger.error(f"Failed to send admin avatar approval message: {e}")

    async def _stage_uploaded_avatar(
        self,
        *,
        attachment: discord.Attachment,
        subfolder: str,
        head_id: str,
        body_type: Optional[int],
        suggested_by: discord.abc.User,
        sender_nickname: str,
        sender_pid: Optional[str],
        source_message_jump_url: Optional[str],
    ) -> Tuple[bool, str]:
        """Stage an uploaded file and route it to the admin-approval flow.

        Called by `UploadAvatarModal.on_submit` after the user picks a file
        from their computer and chooses a subfolder. The file is saved to
        `data/avatars/<subfolder>/<sanitized-name>`, then we reuse
        `_send_admin_avatar_approval` so the existing admin Approve / Reject
        flow kicks in.

        Returns `(True, saved_filename)` on success or `(False, error_message)`
        on failure. The caller (the Modal's `on_submit`) handles the
        user-visible error.
        """
        # Validate subfolder (defensive — Modal already validates).
        if subfolder not in AVATAR_VALID_SUBFOLDERS:
            return False, f"Invalid subfolder `{subfolder}`."

        # 1. Read attachment bytes.
        try:
            data = await attachment.read()
        except Exception as e:
            logger.error(f"Failed to read uploaded attachment: {e}", exc_info=True)
            return False, f"Failed to read the uploaded file: {e}"
        if len(data) > 10 * 1024 * 1024:
            return False, "File too large (max 10 MB)."

        # 2. Determine extension from filename + magic bytes (fallback).
        ext = None
        fname_lower = attachment.filename.lower()
        if fname_lower.endswith(".png"):
            ext = ".png"
        elif fname_lower.endswith(".webp"):
            ext = ".webp"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        if ext is None:
            return False, "File is not a recognized PNG or WEBP."

        # 3. Sanitize the filename (strip path traversal / weird chars).
        raw_name = _re.sub(r"[^A-Za-z0-9._-]", "_", attachment.filename)
        if not raw_name.lower().endswith(ext):
            raw_name = raw_name + ext

        # 4. Check for similar existing files in the target subfolder before writing.
        target_dir = AVATARS_DIR / subfolder
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to mkdir {target_dir}: {e}")
            return False, f"Could not create target folder: {e}"

        # List existing files in the subfolder (just filenames, not relative paths)
        existing_fnames: List[str] = []
        try:
            for f in os.listdir(str(target_dir)):
                if not f.startswith("."):
                    existing_fnames.append(f)
        except Exception:
            pass

        similar = self._find_similar_existing_file(raw_name, subfolder, existing_fnames, similarity_threshold=0.7)
        if similar is not None:
            # Duplicate candidate found — return the existing relative path so the
            # caller can show the DuplicateCheckView to the user. Do NOT write the
            # file yet; only proceed if the user confirms they are different.
            similar_relative = f"{subfolder}/{similar}"
            return False, f"duplicate:{similar_relative}"

        # 5. Write the file.
        target_path = target_dir / raw_name
        if target_path.exists():
            stem, ext_only = os.path.splitext(raw_name)
            n = 1
            while True:
                cand = target_dir / f"{stem}_{n}{ext_only}"
                if not cand.exists():
                    target_path = cand
                    raw_name = cand.name
                    break
                n += 1
        try:
            target_path.write_bytes(data)
        except Exception as e:
            logger.error(f"Failed to write uploaded file {target_path}: {e}", exc_info=True)
            return False, f"Could not write the file to disk: {e}"

        # 6. Build the relative path (under AVATARS_DIR) for the
        # approval flow.
        chosen_relative = f"{subfolder}/{raw_name}"

        # 7. Route to the existing admin-approval flow.
        await self._send_admin_avatar_approval(
            head_id=head_id,
            chosen_filename=chosen_relative,
            body_type=body_type,
            suggested_by=suggested_by,
            source_message_jump_url=source_message_jump_url,
            sender_nickname=sender_nickname,
            sender_pid=sender_pid,
        )
        return True, raw_name



async def setup(bot: commands.Bot):
    await bot.add_cog(LiveChatCog(bot))