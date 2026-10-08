"""
Ranking Cog — HR / ST leaderboards using extracted game names and IDs.

Why this cog exists
-------------------
The leaderboard API identifies a board purely by ``rank_name``:

    HR          -> rank_team10_dungeon_{id}      (Hero's Realm, 10-man)
    ST          -> rank_team_dungeon_{id}        (Sword Trial)
    Cutie Clash -> rank_petbattle_3v3            (disabled for now)

so people previously had to memorise which numeric ID belongs to which dungeon.
Viewing uses the bot-local extracted leaderboard manifest for names and IDs.
The legacy ``data/ranking_map.db`` remains available for registry management
but does not supply view names, aliases, autocomplete or artwork.

Permissions (community-maintained):
  * ``/ranking map add``  — anyone may add a mapping (rate limited + audited,
    and the ID is probed against the live API so garbage is flagged).
  * ``/ranking map edit`` / ``remove`` / ``verify`` / ``scan`` — admin/staff only.
  * ``/ranking map list`` / ``/ranking types`` — everyone (read-only).

How the leaderboard payload works (``utility.wwm.get_rank_list``)
-----------------------------------------------------------------
    url     = RANK_GET_RANKLIST_URL + rank_name        # rank_name is in the path
    payload = {
        "fields": ["base", "head"],   # player fields the service should hydrate
        "pid": pid or "",             # optional -> returns my_data / my_rank
        "hostnum": 10001,             # static, the rank service is cross-server
        "fuzzy_info": {},
        "rank_name": rank_name,       # also duplicated in the body
        "page": page,                 # 1-indexed
        "params": {},
        "start": (page - 1) * 20,     # 20 entries per page, derived from page
        "end": start + 20,            # exclusive
    }
    result  = {rank_list, my_data, my_rank, rank_total_len, start, end, page}

  * HR/ST scores are *negative seconds* (e.g. -450.119 -> "7:30.119");
    Cutie Clash scores are points.
  * ``my_rank`` of -1 means "the requested pid is not on this board".
  * Team boards expose ``ud.leader_id`` / ``ud.members`` / per-pid ``hostnum``.
"""
import asyncio
import math
import re
import time
from typing import Dict, List, Optional, Tuple

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import (ActionRow, Button, Container, LayoutView, Section,
                        Select, Separator, TextDisplay, MediaGallery)

import settings
from settings import BASE_DIR, logger
from utility.dungeon_assets import DungeonAssets
from utility.api_constants import SCHOOL_NAMES
from utility.wwm import get_bulk_players_info_multi_hostnum, get_rank_list

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RANKING_ACCENT = 0xE67E22          # Orange, matches the old ranking UI
BLURPLE = 0x5865F2

DB_PATH = BASE_DIR / "data" / "ranking_map.db"
VERIFICATION_DB_PATH = BASE_DIR / "data" / "guild_verification.db"

ITEMS_PER_PAGE = 20                # API pages are fixed at 20 entries
MAX_AUTOCOMPLETE_CHOICES = 25      # Discord hard limit
CHOICE_LABEL_LIMIT = 100           # Discord hard limit for choice labels
MAX_NAME_LENGTH = 80
MAX_ALIASES_LENGTH = 200
ADD_COOLDOWN_WINDOW = 600          # seconds
ADD_COOLDOWN_MAX = 5               # mappings per user per window
SCAN_MAX_RANGE = 200               # max IDs per /ranking map scan run
PROBE_DELAY = 0.25                 # seconds between probe calls (be nice to the API)
OWNER_DENY_MSG = "❌ Only the user who used the command can interact with these buttons."

# ``enabled`` gates whether the type is offered in commands/autocomplete.
RANKING_TYPES: Dict[str, dict] = {
    "hr": {
        "label": "HR",
        "long": "Hero's Realm (10-man)",
        "emoji": "⚔️",
        "prefix": "rank_team10_dungeon_",
        "enabled": True,
    },
    "st": {
        "label": "ST",
        "long": "Sword Trial",
        "emoji": "⏱️",
        "prefix": "rank_team_dungeon_",
        "enabled": True,
    },
    # Kept fully wired so Cutie Clash can be re-enabled with a single flag flip.
    "pet": {
        "label": "CC",
        "long": "Cutie Clash 3v3",
        "emoji": "🐾",
        "prefix": None,
        "fixed": "rank_petbattle_3v3",
        "enabled": False,
    },
}

TYPE_CHOICES = [
    app_commands.Choice(name=f"{meta['label']} — {meta['long']}", value=key)
    for key, meta in RANKING_TYPES.items()
    if meta.get("enabled")
]

# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no Discord / DB / network)
# ---------------------------------------------------------------------------
def _build_rank_name(rank_type: str, dungeon_id: Optional[int]) -> str:
    """Build the API ``rank_name`` for a type + dungeon ID."""
    meta = RANKING_TYPES.get(rank_type)
    if meta is None:
        raise ValueError(f"Unknown ranking type: {rank_type!r}")
    fixed = meta.get("fixed")
    if fixed:
        return fixed
    if dungeon_id is None:
        raise ValueError("A numeric dungeon ID is required for this ranking type")
    return f"{meta['prefix']}{int(dungeon_id)}"


def _format_rank_time(score) -> str:
    """Format a negative time-based score (in seconds) as M:SS.sss.

    Example: -450.11907958984375 -> '7:30.119'
    """
    if score is None:
        return "N/A"
    try:
        seconds = abs(float(score))
        if not math.isfinite(seconds):
            return "N/A"
        milliseconds = round(seconds * 1000)
        minutes, remainder = divmod(milliseconds, 60000)
        return f"{minutes}:{remainder / 1000:06.3f}"
    except (ValueError, TypeError, OverflowError):
        return "N/A"



def _format_rank_points(score) -> str:
    """Format a points-based score with thousands separators."""
    if score is None:
        return "N/A"
    try:
        return f"{int(score):,}"
    except (ValueError, TypeError, OverflowError):
        return "N/A"


def _rank_name_to_display(rank_name: str) -> str:
    """Convert a rank_name like 'rank_team10_dungeon_22' to a friendly label."""
    if rank_name.startswith("rank_team10_dungeon_"):
        return f"HR Dungeon {rank_name.split('_')[-1]}"
    if rank_name.startswith("rank_team_dungeon_"):
        return f"ST Dungeon {rank_name.split('_')[-1]}"
    if rank_name == "rank_petbattle_3v3":
        return "Cutie Clash 3v3"
    return rank_name


def _tag(rank_type: str, dungeon_id: Optional[int]) -> str:
    """Short machine tag for a mapping, e.g. 'hr:22'."""
    return f"{rank_type}:{dungeon_id}" if dungeon_id is not None else rank_type


def _type_label(rank_type: str) -> str:
    meta = RANKING_TYPES.get(rank_type)
    return meta["label"] if meta else str(rank_type).upper()


def _entry_label(rank_type: str, dungeon_id: Optional[int], name: Optional[str] = None) -> str:
    """Build a Discord choice label for a dungeon entry (<= 100 chars)."""
    label = f"[{_type_label(rank_type)}] {name or 'unnamed'}"
    if dungeon_id is not None:
        label += f" · id {dungeon_id}"
    return label[:CHOICE_LABEL_LIMIT]


def _parse_aliases(raw: Optional[str]) -> List[str]:
    """Split a comma-separated alias string into a clean, de-duplicated list."""
    if not raw:
        return []
    parts = [p.strip().lower() for p in str(raw).split(",")]
    seen: List[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return seen


def _art_images(banner):
    if not banner or not banner.get("path"):
        return []
    return banner.get("images") or [banner]


def _art_filename(index):
    return "dungeon-banner.png" if index == 0 else f"dungeon-boss-{index + 1}.png"


def _art_gallery(banner):
    return MediaGallery(*(discord.MediaGalleryItem(
        "attachment://" + _art_filename(i),
        description="Original game dungeon artwork: " + image.get("boss_name", "Dungeon selection card"))
        for i, image in enumerate(_art_images(banner))))


def _split_type_prefix(query: str) -> Tuple[Optional[str], str]:
    """Pull an optional leading type token off a free-typed query.

    'st 26' -> ('st', '26')      'st' -> ('st', '')
    'storm' -> (None, 'storm')   'St fro' -> ('st', 'fro')
    """
    text = (query or "").strip()
    lowered = text.lower()
    if lowered in RANKING_TYPES:
        return lowered, ""
    match = re.match(r"^([a-zA-Z]{2,4})\s*[:_\-\s]\s*(.*)$", text)
    if match and match.group(1).lower() in RANKING_TYPES:
        return match.group(1).lower(), match.group(2).strip()
    return None, text


def _score_entry(entry: dict, query: str) -> Optional[Tuple[int, int]]:
    """Return a match score (lower is better) for ``entry`` vs ``query``, or None.

    Ordering: exact tag/ID > exact name > exact alias > name prefix >
    alias prefix > name substring > alias substring > ID prefix.
    """
    q = (query or "").strip().lower()
    if not q:
        return (9, 0)

    rank_type = entry.get("rank_type")
    dungeon_id = entry.get("dungeon_id")
    name = (entry.get("name") or "").lower()
    aliases = [a.lower() for a in (entry.get("aliases") or [])]
    id_str = str(dungeon_id) if dungeon_id is not None else ""

    if q == _tag(rank_type, dungeon_id) or (id_str and q == id_str):
        return (0, 0)
    if name and name == q:
        return (1, 0)
    if aliases and q in aliases:
        return (2, 0)
    if name and name.startswith(q):
        return (3, 0)
    if any(a.startswith(q) for a in aliases):
        return (4, 0)
    if name and q in name:
        return (5, 0)
    if any(q in a for a in aliases):
        return (6, 0)
    if id_str and id_str.startswith(q):
        return (7, 0)
    return None


def _autocomplete_sort_key(entry: dict, score: Tuple[int, int], usage: int) -> Tuple:
    """Stable ordering: match quality, popularity, verified, type, ID."""
    return (
        score[0],
        -int(usage or 0),
        0 if entry.get("verified") else 1,
        entry.get("rank_type") or "",
        entry.get("dungeon_id") or 0,
    )


def _extract_nickname(player_info) -> str:
    """Extract a nickname from either shape of ``player_info``."""
    if not isinstance(player_info, dict):
        return "Unknown"
    # Shape B: {"base": {...}, "head": {...}}
    if isinstance(player_info.get("base"), dict):
        return player_info["base"].get("nickname", "Unknown")
    # Shape A: {pid: {"base": {...}}}
    for pdata in player_info.values():
        if isinstance(pdata, dict) and isinstance(pdata.get("base"), dict):
            return pdata["base"].get("nickname", "Unknown")
    return "Unknown"


def _entry_player_base(entry: dict) -> dict:
    """Extract the ``base`` dict from a rank_list entry, handling both payload shapes."""
    player_info = entry.get("player_info", {}) if isinstance(entry, dict) else {}
    if not isinstance(player_info, dict):
        return {}
    # Shape B: player_info is directly {base: {...}, head: {...}, id: ...}
    if isinstance(player_info.get("base"), dict):
        return player_info["base"]
    # Shape A: player_info is keyed by PID -> {base: {...}, ...}
    for pdata in player_info.values():
        if isinstance(pdata, dict) and isinstance(pdata.get("base"), dict):
            return pdata["base"]
    return {}


async def ensure_owner(interaction: discord.Interaction, view) -> bool:
    """True if the interacting user owns ``view``; otherwise reply and return False."""
    owner_id = getattr(view, "owner_id", None)
    if owner_id is None or interaction.user.id == owner_id:
        return True
    if interaction.response.is_done():
        await interaction.followup.send(OWNER_DENY_MSG, ephemeral=True)
    else:
        await interaction.response.send_message(OWNER_DENY_MSG, ephemeral=True)
    return False


async def is_admin_or_staff(interaction: discord.Interaction) -> bool:
    """Administrator or holder of any staff role from settings (dev-branch safe)."""
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
    """Command check: administrator or staff role holder only."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not await is_admin_or_staff(interaction):
            raise app_commands.MissingPermissions(["administrator"])
        return True
    return app_commands.check(predicate)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class DungeonIDModal(discord.ui.Modal, title="Enter Dungeon ID"):
    """Fallback for brand-new / unmapped leaderboard IDs."""

    def __init__(self, cog, rank_type: str, target_pid: Optional[str] = None, owner_id: Optional[int] = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.rank_type = rank_type
        self.target_pid = target_pid
        self.owner_id = owner_id

    dungeon_id = discord.ui.TextInput(
        label="Dungeon ID",
        placeholder="e.g. 22",
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        dungeon_id = (self.dungeon_id.value or "").strip()
        if not dungeon_id.isdigit():
            await interaction.followup.send("❌ Dungeon ID must be a number.", ephemeral=True)
            return
        await self.cog.show_ranking_results(
            interaction, self.rank_type, int(dungeon_id), target_pid=self.target_pid
        )


class RankingEntryChoiceView(LayoutView):
    """Disambiguation / type picker: choose which entry to open."""

    def __init__(self, cog, title: str, candidates: List[dict], target_pid: Optional[str] = None,
                 owner_id: Optional[int] = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.candidates = candidates
        self.target_pid = target_pid
        self.owner_id = owner_id

        body_lines = [f"# 🔎 {title}", "Multiple leaderboards match — pick one:"]
        inner = [TextDisplay("\n".join(body_lines)), Separator(spacing=discord.SeparatorSpacing.small)]

        options = []
        for cand in candidates[:MAX_AUTOCOMPLETE_CHOICES]:
            label = _entry_label(cand.get("rank_type"), cand.get("dungeon_id"), cand.get("name"))
            if not cand.get("name"):
                label += " (unmapped)"
            options.append(discord.SelectOption(label=label[:CHOICE_LABEL_LIMIT],
                                                value=_tag(cand["rank_type"], cand["dungeon_id"])))
        row = ActionRow()
        select = Select(placeholder="Choose a leaderboard...", options=options, custom_id="ranking_entry_choice")
        select.callback = self._handle_choice
        row.add_item(select)
        inner.append(row)

        self.add_item(Container(*inner, accent_color=RANKING_ACCENT))

    async def _handle_choice(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        tag = (interaction.data.get("values") or [""])[0]
        rank_type, _, raw_id = tag.partition(":")
        try:
            dungeon_id = int(raw_id) if raw_id else None
        except (TypeError, ValueError):
            await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
            return
        entry = next((c for c in self.candidates
                      if c["rank_type"] == rank_type and c["dungeon_id"] == dungeon_id), None)
        await self.cog.show_ranking_results(
            interaction, rank_type, dungeon_id, target_pid=self.target_pid,
            dungeon_label=(entry or {}).get("name"),
        )

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            item.disabled = True


class DungeonPickerView(LayoutView):
    """Discovery view: browse mapped dungeons for a type, or type a raw ID."""

    ITEMS_PER_PAGE = 25

    def __init__(self, cog, entries: List[dict], rank_type: str = "hr", page: int = 0,
                 target_pid: Optional[str] = None, owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.entries = sorted(entries or [], key=lambda e: (e.get("dungeon_id") or 0))
        self.rank_type = rank_type if rank_type in RANKING_TYPES else "hr"
        self.page = page
        self.target_pid = target_pid
        self.owner_id = owner_id
        self._rebuild()

    def _type_entries(self) -> List[dict]:
        return [e for e in self.entries if e.get("rank_type") == self.rank_type]

    def _rebuild(self):
        self.clear_items()
        meta = RANKING_TYPES.get(self.rank_type, {})
        type_entries = self._type_entries()
        total_pages = max(1, -(-len(type_entries) // self.ITEMS_PER_PAGE))
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * self.ITEMS_PER_PAGE
        page_items = type_entries[start:start + self.ITEMS_PER_PAGE]

        header_lines = [
            f"# 🏆 {meta.get('emoji', '')} Leaderboard Lookup",
            "Type a dungeon name straight into `/ranking view` (autocomplete), or pick one below.",
        ]
        if self.target_pid:
            header_lines.append("🎯 **Target queued:** will jump to their rank when found.")

        inner = [
            TextDisplay("\n".join(header_lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        select_row = ActionRow()
        if page_items:
            options = []
            for entry in page_items:
                label = _entry_label(entry["rank_type"], entry["dungeon_id"], entry.get("name"))
                if not entry.get("verified"):
                    label = f"⚠️ {label}"
                options.append(discord.SelectOption(
                    label=label[:CHOICE_LABEL_LIMIT],
                    value=_tag(entry["rank_type"], entry["dungeon_id"]),
                ))
            picker = Select(
                placeholder=(f"{meta.get('label', '')} dungeons — {len(type_entries)} extracted "
                             f"(page {self.page + 1}/{total_pages})"),
                options=options,
                custom_id="ranking_picker_select",
            )
            picker.callback = self._handle_pick
            select_row.add_item(picker)
        else:
            select_row = None
            inner.append(TextDisplay(
                f"ℹ️ No **{meta.get('label', self.rank_type)}** dungeons extracted yet.\n"
                "Refresh the extracted game data, or use **⌨️ Type an ID** below."
            ))
            inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        type_row = ActionRow()
        for key, tmeta in RANKING_TYPES.items():
            if not tmeta.get("enabled"):
                continue
            btn = Button(
                label=f"{tmeta.get('emoji', '')} {tmeta['label']} — {tmeta['long']}",
                style=discord.ButtonStyle.primary if key == self.rank_type else discord.ButtonStyle.secondary,
                custom_id=f"ranking_picker_type_{key}",
                disabled=key == self.rank_type,
            )
            btn.callback = self._make_type_callback(key)
            type_row.add_item(btn)
        inner.append(type_row)

        nav_row = ActionRow()
        prev_btn = Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                          custom_id="ranking_picker_prev", disabled=self.page <= 0)
        prev_btn.callback = self._handle_prev
        nav_row.add_item(prev_btn)

        id_btn = Button(label="⌨️ Type an ID", style=discord.ButtonStyle.secondary,
                        custom_id="ranking_picker_type_id")
        id_btn.callback = self._handle_type_id
        nav_row.add_item(id_btn)

        next_btn = Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                          custom_id="ranking_picker_next", disabled=self.page >= total_pages - 1)
        next_btn.callback = self._handle_next
        nav_row.add_item(next_btn)
        inner.append(nav_row)

        if select_row is not None:
            inner.append(select_row)

        self.add_item(Container(*inner, accent_color=RANKING_ACCENT))

    def _make_type_callback(self, rank_type: str):
        async def callback(interaction: discord.Interaction):
            if not await ensure_owner(interaction, self):
                return
            await interaction.response.defer()
            await self.cog.show_dungeon_picker(
                interaction, rank_type=rank_type, target_pid=self.target_pid, page=0
            )
        return callback

    async def _handle_pick(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        tag = (interaction.data.get("values") or [""])[0]
        rank_type, _, raw_id = tag.partition(":")
        try:
            dungeon_id = int(raw_id)
        except (TypeError, ValueError):
            await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
            return
        entry = next((e for e in self.entries
                      if e["rank_type"] == rank_type and e["dungeon_id"] == dungeon_id), None)
        await self.cog.show_ranking_results(
            interaction, rank_type, dungeon_id, target_pid=self.target_pid,
            dungeon_label=(entry or {}).get("name"),
        )

    async def _handle_prev(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_next(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        total_pages = max(1, -(-len(self._type_entries()) // self.ITEMS_PER_PAGE))
        if self.page < total_pages - 1:
            self.page += 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_type_id(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.send_modal(
            DungeonIDModal(self.cog, self.rank_type, self.target_pid, owner_id=self.owner_id)
        )

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            item.disabled = True
class RankingResultsView(LayoutView):
    """Leaderboard page with pagination, personal rank, and team/player drill-down."""

    ITEMS_PER_PAGE = 20

    def __init__(self, cog, rank_type: str, dungeon_id, rank_name: str, page: int = 1,
                 dungeon_label: Optional[str] = None, target_pid: Optional[str] = None,
                 owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.rank_type = rank_type
        self.dungeon_id = dungeon_id
        self.rank_name = rank_name
        self.dungeon_label = dungeon_label
        self.page = page
        self.target_pid = target_pid
        self.owner_id = owner_id
        self.target_nickname = None
        self.total_pages = 1
        self.total_entries = 0
        self.my_rank = None
        self.my_score = None
        self.my_nickname = None
        self.rank_list = []
        self.last_place_score = None
        self.last_place_nickname = None
        self.page_entries = []
        self.banner = cog.dungeon_assets.get(rank_type, dungeon_id) if hasattr(cog, 'dungeon_assets') else None

        self._rebuild()

    def _title(self) -> str:
        meta = RANKING_TYPES.get(self.rank_type, {})
        name = discord.utils.escape_mentions(discord.utils.escape_markdown(str(self.dungeon_label or _rank_name_to_display(self.rank_name))))
        title = f"# 🏆 {meta.get('emoji', '')} {meta.get('label', self.rank_type.upper())} — {name}"
        if self.dungeon_id is not None:
            title += f"\n`id {self.dungeon_id}` · `{self.rank_name}`"
        return title

    def _rebuild(self):
        self.clear_items()

        is_time_based = self.rank_type in ("hr", "st")

        header_lines = [self._title()]
        if self.total_entries:
            header_lines.append(f"📊 **Total Entries:** {self.total_entries}")
        header_lines.append(f"📍 **Page {self.page}/{self.total_pages}**")

        inner_items = [
            TextDisplay("\n".join(header_lines)),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        if _art_images(self.banner):
            inner_items.insert(1, _art_gallery(self.banner))
            inner_items.insert(2, TextDisplay("-# Original dungeon selection artwork"))

        if self.rank_list:
            lines = []
            for idx, entry in enumerate(self.rank_list):
                rank_num = (self.page - 1) * self.ITEMS_PER_PAGE + idx + 1
                base = _entry_player_base(entry)
                nickname = discord.utils.escape_mentions(discord.utils.escape_markdown(str(base.get('nickname') or 'Unknown')[:48]))
                score = entry.get('score')
                attempt_ts = (entry.get('ud') or {}).get('ts', 0)

                if rank_num == 1:
                    prefix = "🥇"
                elif rank_num == 2:
                    prefix = "🥈"
                elif rank_num == 3:
                    prefix = "🥉"
                else:
                    prefix = f"{rank_num}."

                score_str = _format_rank_time(score) if is_time_based else _format_rank_points(score)
                time_str = f" — <t:{int(attempt_ts)}:R>" if type(attempt_ts) in (int, float) and math.isfinite(attempt_ts) and attempt_ts > 0 else ""
                lines.append(f"{prefix} **{nickname}** — {score_str}{time_str}")

            inner_items.append(TextDisplay("\n".join(lines)))
        else:
            inner_items.append(TextDisplay("*No entries on this page.*"))

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # My rank (or the target player's rank)
        if self.my_rank is not None and self.my_rank >= 0:
            my_rank_display = self.my_rank + 1
            my_score_str = _format_rank_time(self.my_score) if is_time_based else _format_rank_points(self.my_score)
            if self.target_nickname:
                inner_items.append(TextDisplay(
                    f"🎯 **{self.target_nickname}'s Rank:** #{my_rank_display}  |  **Score:** {my_score_str}"
                ))
            else:
                inner_items.append(TextDisplay(
                    f"🎯 **Your Rank:** #{my_rank_display}  |  **Your Score:** {my_score_str}"
                ))
        else:
            if self.target_nickname:
                inner_items.append(TextDisplay(f"🎯 **{self.target_nickname} is not on this leaderboard.**"))
            elif self.target_pid:
                inner_items.append(TextDisplay("🎯 **The requested player is not on this leaderboard.**"))
            elif getattr(self, 'queried_pid', None):
                inner_items.append(TextDisplay("🎯 **You are not on this leaderboard.**"))
            else:
                inner_items.append(TextDisplay("🎯 Link your game account or specify a player to see a personal rank."))

        # Last place / score needed to break in
        if self.last_place_score is not None:
            if is_time_based:
                last_str = _format_rank_time(self.last_place_score)
                target_str = f"⏱️ **Target Time to Break In:** < {last_str}"
            else:
                last_str = _format_rank_points(self.last_place_score)
                target_str = f"🎯 **Target Score to Break In:** > {last_str}"
            last_name = self.last_place_nickname or "Unknown"
            inner_items.append(TextDisplay(
                f"⚠️ **Last Place (#{self.total_entries}):** {last_name} — {last_str}\n{target_str}"
            ))

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Team/player drill-down dropdown
        self.page_entries = []
        if self.rank_list:
            options = []
            for idx, entry in enumerate(self.rank_list):
                rank_num = (self.page - 1) * self.ITEMS_PER_PAGE + idx + 1
                base = _entry_player_base(entry)
                nickname = base.get('nickname', 'Unknown')
                self.page_entries.append({'rank': rank_num, 'nickname': nickname, 'entry': entry})
                options.append(discord.SelectOption(
                    label=f"{rank_num}. {nickname}"[:CHOICE_LABEL_LIMIT], value=str(idx)
                ))

            if options:
                placeholder = "👥 View Team" if self.rank_type in ("hr", "st") else "👤 View Player"
                select_row = ActionRow()
                view_select = Select(
                    placeholder=placeholder,
                    options=options[:MAX_AUTOCOMPLETE_CHOICES],
                    custom_id="ranking_view_entry",
                )
                view_select.callback = self._handle_view_entry
                select_row.add_item(view_select)
                inner_items.append(select_row)

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Pagination
        nav_row = ActionRow()
        prev_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="◀ Prev",
            custom_id="ranking_prev",
            disabled=self.page <= 1,
        )
        prev_btn.callback = self._handle_prev
        nav_row.add_item(prev_btn)

        page_label = Button(
            style=discord.ButtonStyle.secondary,
            label=f"{self.page}/{self.total_pages}",
            custom_id="ranking_page_label",
            disabled=True,
        )
        nav_row.add_item(page_label)

        next_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="Next ▶",
            custom_id="ranking_next",
            disabled=self.page >= self.total_pages,
        )
        next_btn.callback = self._handle_next
        nav_row.add_item(next_btn)
        inner_items.append(nav_row)

        # Back to the dungeon browser
        back_row = ActionRow()
        back_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="🔙 Browse Dungeons",
            custom_id="ranking_back",
        )
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner_items.append(back_row)

        self.add_item(Container(*inner_items, accent_color=RANKING_ACCENT))

    async def _handle_prev(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        if self.page > 1:
            await self.cog.show_ranking_results(
                interaction, self.rank_type, self.dungeon_id, page=self.page - 1,
                target_pid=self.target_pid, dungeon_label=self.dungeon_label, jump_to_target=False,
            )

    async def _handle_next(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        if self.page < self.total_pages:
            await self.cog.show_ranking_results(
                interaction, self.rank_type, self.dungeon_id, page=self.page + 1,
                target_pid=self.target_pid, dungeon_label=self.dungeon_label, jump_to_target=False,
            )

    async def _handle_back(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        await self.cog.show_dungeon_picker(
            interaction, rank_type=self.rank_type, target_pid=self.target_pid
        )

    async def _handle_view_entry(self, interaction: discord.Interaction):
        """Show the selected team (HR/ST) or player (Cutie Clash) details."""
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer(ephemeral=True)
        selected = (interaction.data.get("values") or [""])[0]
        try:
            idx = int(selected)
        except (ValueError, TypeError):
            await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
            return
        if idx < 0 or idx >= len(self.page_entries):
            await interaction.followup.send("❌ Invalid selection.", ephemeral=True)
            return

        entry_data = self.page_entries[idx]
        entry = entry_data['entry']
        rank_num = entry_data['rank']

        if self.rank_type in ("hr", "st"):
            ud = entry.get('ud', {}) or {}
            members = ud.get('members', []) or []
            hostnum2pids: Dict[int, List[str]] = {}
            for pid in members:
                hostnum = (ud.get(pid, {}) or {}).get('hostnum', 10595)
                hostnum2pids.setdefault(hostnum, []).append(pid)

            try:
                bulk = await get_bulk_players_info_multi_hostnum(hostnum2pids, fields=["base"])
            except Exception as e:
                logger.error(f"Failed to fetch team members: {e}", exc_info=True)
                await interaction.followup.send(f"❌ Failed to load team: `{str(e)}`", ephemeral=True)
                return

            member_list = []
            if bulk and bulk.get('code') == 0:
                players = bulk.get('result', {})
                for pid in members:
                    base = players.get(pid, {}).get('base', {})
                    school_id = base.get('school', 0)
                    school_name = SCHOOL_NAMES.get(school_id) if school_id in SCHOOL_NAMES else None
                    member_list.append({
                        'pid': pid,
                        'hostnum': (ud.get(pid, {}) or {}).get('hostnum', 10595),
                        'nickname': base.get('nickname', 'Unknown'),
                        'level': base.get('level', 0),
                        'number_id': str(base.get('number_id', '') or ''),
                        'is_online': base.get('is_online', 0) == 1,
                        'school_name': school_name,
                    })

            if not member_list:
                await interaction.followup.send("❌ Could not load team members.", ephemeral=True)
                return

            team_view = TeamDetailView(
                cog=self.cog,
                rank_type=self.rank_type,
                rank_name=self.rank_name,
                rank_num=rank_num,
                score=entry.get('score', 0),
                members=member_list,
                back_view=self,
                owner_id=self.owner_id,
            )
            await interaction.edit_original_response(content=None, embed=None, view=team_view)
        else:
            base = _entry_player_base(entry)
            number_id = str(base.get('number_id', '') or '')
            nickname = base.get('nickname', 'Unknown')
            if not number_id:
                await interaction.followup.send(f"❌ No Number ID available for {nickname}.", ephemeral=True)
                return
            try:
                view, files = await self.cog.build_player_profile_view(number_id, interaction, ephemeral=True)
                if not view:
                    await interaction.followup.send(f"❌ Could not load profile for {nickname}", ephemeral=True)
                    return
                view._original_message = None
                await interaction.followup.send(view=view, files=files, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to show player profile from ranking: {e}", exc_info=True)
                await interaction.followup.send(f"❌ Failed to load profile: `{str(e)}`", ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            item.disabled = True
class TeamDetailView(LayoutView):
    """Shows one team's members with a per-member profile button."""

    def __init__(self, cog, rank_type: str, rank_name: str, rank_num: int, score,
                 members: list, back_view, owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.rank_type = rank_type
        self.rank_name = rank_name
        self.rank_num = rank_num
        self.score = score
        self.members = members or []
        self.back_view = back_view
        self.owner_id = owner_id

        is_time_based = self.rank_type in ("hr", "st")
        score_str = _format_rank_time(score) if is_time_based else _format_rank_points(score)

        inner_items = [
            TextDisplay(f"# 👥 Team #{self.rank_num} — {score_str}\n\n**Members:** {len(self.members)}"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        banner = getattr(self.back_view, 'banner', None)
        if _art_images(banner):
            inner_items.insert(1, _art_gallery(banner))

        for idx, m in enumerate(self.members):
            online_icon = "🟢" if m.get('is_online') else "⚫"
            school_str = f" | {m['school_name']}" if m.get('school_name') else ""
            member_text = f"{online_icon} **{m['nickname']}** Lv.{m['level']}{school_str} | ID: {m['number_id']}"
            if m.get('number_id'):
                btn = Button(
                    label="🔍 Profile",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"team_member_profile_{idx}",
                )
                btn.callback = self._make_profile_callback(m)
                inner_items.append(Section(TextDisplay(member_text), accessory=btn))
            else:
                inner_items.append(TextDisplay(member_text))

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        back_row = ActionRow()
        back_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="🔙 Back to Leaderboard",
            custom_id="team_back",
        )
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner_items.append(back_row)

        self.add_item(Container(*inner_items, accent_color=RANKING_ACCENT))

    def _make_profile_callback(self, member: dict):
        async def callback(interaction: discord.Interaction):
            if not await ensure_owner(interaction, self):
                return
            await interaction.response.defer(ephemeral=True)
            try:
                view, files = await self.cog.build_player_profile_view(
                    member['number_id'], interaction, ephemeral=True
                )
                if not view:
                    await interaction.followup.send(
                        f"❌ Could not load profile for {member['nickname']}", ephemeral=True
                    )
                    return
                view._original_message = None
                await interaction.followup.send(view=view, files=files, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to show team member profile: {e}", exc_info=True)
                await interaction.followup.send(f"❌ Failed to load profile: `{str(e)}`", ephemeral=True)
        return callback

    async def _handle_back(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        await interaction.edit_original_response(content=None, embed=None, view=self.back_view)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            item.disabled = True
                    elif isinstance(sub, Section):
                        for item in sub.children:
                            if isinstance(item, Button):
                                item.disabled = True


class RankingMapListView(LayoutView):
    """Read-only listing of the dungeon name -> ID registry."""

    ITEMS_PER_PAGE = 12

    def __init__(self, cog, entries: List[dict], rank_type: Optional[str] = None, page: int = 0,
                 owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.rank_type = rank_type
        self.page = page
        self.owner_id = owner_id
        # Sorted by leaderboard ID (the number in rank_*_dungeon_{id}), so the list
        # reads in the same order as the in-game dungeon numbering. Entries of the
        # same ID across types stay adjacent (HR first, then ST).
        self.entries = sorted(entries or [], key=lambda e: (
            e.get("dungeon_id") or 0, e.get("rank_type") or ""
        ))
        self._rebuild()

    def _filtered(self) -> List[dict]:
        if not self.rank_type:
            return self.entries
        return [e for e in self.entries if e.get("rank_type") == self.rank_type]

    def _rebuild(self):
        self.clear_items()
        rows = self._filtered()
        total_pages = max(1, -(-len(rows) // self.ITEMS_PER_PAGE))
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * self.ITEMS_PER_PAGE
        page_items = rows[start:start + self.ITEMS_PER_PAGE]

        counts = {}
        for entry in self.entries:
            counts[entry["rank_type"]] = counts.get(entry["rank_type"], 0) + 1
        count_str = " · ".join(f"{_type_label(k)}: {v}" for k, v in sorted(counts.items()))

        inner = [
            TextDisplay(f"# 🗺️ Leaderboard Name Registry\n**Total mappings:** {len(self.entries)}  ({count_str or 'none'})"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        if page_items:
            lines = []
            for entry in page_items:
                icon = "✅" if entry.get("verified") else "⚠️"
                tag = _tag(entry["rank_type"], entry["dungeon_id"])
                aliases = f"\n   ↳ aliases: {', '.join(entry['aliases'])}" if entry.get("aliases") else ""
                entries_txt = f" · {entry['last_entries']:,} entries" if entry.get("last_entries") else ""
                lines.append(f"{icon} **{entry['name']}** (`{tag}`){entries_txt}{aliases}")
            inner.append(TextDisplay("\n".join(lines)))
        else:
            inner.append(TextDisplay("ℹ️ No mappings yet — add one with `/ranking map add`."))

        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner.append(TextDisplay(f"📍 Page {self.page + 1}/{total_pages} · ✅ verified · ⚠️ unverified"))

        filter_row = ActionRow()
        all_btn = Button(
            label=f"All ({len(self.entries)})",
            style=discord.ButtonStyle.primary if self.rank_type is None else discord.ButtonStyle.secondary,
            custom_id="ranking_map_filter_all",
            disabled=self.rank_type is None,
        )
        all_btn.callback = self._make_filter_callback(None)
        filter_row.add_item(all_btn)
        for key, meta in RANKING_TYPES.items():
            if not meta.get("enabled"):
                continue
            btn = Button(
                label=f"{meta.get('emoji', '')} {meta['label']} ({counts.get(key, 0)})",
                style=discord.ButtonStyle.primary if self.rank_type == key else discord.ButtonStyle.secondary,
                custom_id=f"ranking_map_filter_{key}",
                disabled=self.rank_type == key,
            )
            btn.callback = self._make_filter_callback(key)
            filter_row.add_item(btn)
        inner.append(filter_row)

        nav_row = ActionRow()
        prev_btn = Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                          custom_id="ranking_map_prev", disabled=self.page <= 0)
        prev_btn.callback = self._handle_prev
        nav_row.add_item(prev_btn)
        next_btn = Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                          custom_id="ranking_map_next", disabled=self.page >= total_pages - 1)
        next_btn.callback = self._handle_next
        nav_row.add_item(next_btn)
        inner.append(nav_row)

        self.add_item(Container(*inner, accent_color=BLURPLE))

    def _make_filter_callback(self, rank_type: Optional[str]):
        async def callback(interaction: discord.Interaction):
            if not await ensure_owner(interaction, self):
                return
            await interaction.response.defer()
            await self.cog.show_map_list(interaction, rank_type=rank_type, page=0)
        return callback

    async def _handle_prev(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_next(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        total_pages = max(1, -(-len(self._filtered()) // self.ITEMS_PER_PAGE))
        if self.page < total_pages - 1:
            self.page += 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            item.disabled = True


class RankingCog(commands.Cog):
    """HR / ST leaderboards plus the community-maintained dungeon name registry."""

    ranking_group = app_commands.Group(
        name="ranking",
        description="View WWM leaderboards (HR, ST) and manage the name → ID registry",
    )

    map_group = app_commands.Group(
        name="map",
        description="Manage the dungeon name → leaderboard ID registry",
        parent=ranking_group,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH
        self.dungeon_assets = DungeonAssets(BASE_DIR / "data/ranking_assets")
        self._entries: List[dict] = []
        self._usage: Dict[Tuple[str, int], int] = {}
        self._cache_loaded = False
        self._cache_lock = asyncio.Lock()
        self._add_times: Dict[int, List[float]] = {}

    async def cog_load(self):
        await self._init_db()
        await self.load_cache(force=True)
        logger.info(f"RankingCog loaded — {len(self._entries)} dungeon mapping(s) available")

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    async def _init_db(self):
        (BASE_DIR / "data").mkdir(exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ranking_dungeons (
                    rank_type       TEXT    NOT NULL,
                    dungeon_id      INTEGER NOT NULL,
                    name            TEXT    NOT NULL,
                    aliases         TEXT    DEFAULT '',
                    verified        INTEGER DEFAULT 0,
                    last_entries    INTEGER DEFAULT 0,
                    last_checked_ts INTEGER DEFAULT 0,
                    created_by      INTEGER,
                    created_at      INTEGER,
                    updated_by      INTEGER,
                    updated_at      INTEGER,
                    PRIMARY KEY (rank_type, dungeon_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ranking_usage (
                    rank_type    TEXT    NOT NULL,
                    dungeon_id   INTEGER NOT NULL,
                    uses         INTEGER DEFAULT 0,
                    last_used_ts INTEGER DEFAULT 0,
                    PRIMARY KEY (rank_type, dungeon_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ranking_scan (
                    rank_type  TEXT    NOT NULL,
                    dungeon_id INTEGER NOT NULL,
                    entries    INTEGER DEFAULT 0,
                    checked_ts INTEGER DEFAULT 0,
                    PRIMARY KEY (rank_type, dungeon_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ranking_map_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    user_id INTEGER,
                    action TEXT,
                    rank_type TEXT,
                    dungeon_id INTEGER,
                    old_name TEXT,
                    new_name TEXT,
                    old_aliases TEXT,
                    new_aliases TEXT
                )
            """)
            await db.commit()

    @staticmethod
    def _row_to_entry(row) -> dict:
        return {
            "rank_type": row["rank_type"],
            "dungeon_id": int(row["dungeon_id"]),
            "name": row["name"],
            "aliases": _parse_aliases(row["aliases"]),
            "verified": int(row["verified"] or 0),
            "last_entries": int(row["last_entries"] or 0),
            "last_checked_ts": int(row["last_checked_ts"] or 0),
            "created_by": row["created_by"],
            "created_at": int(row["created_at"] or 0),
            "updated_by": row["updated_by"],
            "updated_at": int(row["updated_at"] or 0),
        }

    async def _fetch_entries(self, rank_type: Optional[str] = None) -> List[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if rank_type:
                cursor = await db.execute(
                    "SELECT * FROM ranking_dungeons WHERE rank_type = ? ORDER BY name COLLATE NOCASE",
                    (rank_type,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM ranking_dungeons ORDER BY rank_type, name COLLATE NOCASE"
                )
            rows = await cursor.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def _fetch_entry(self, rank_type: str, dungeon_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM ranking_dungeons WHERE rank_type = ? AND dungeon_id = ?",
                (rank_type, int(dungeon_id)),
            )
            row = await cursor.fetchone()
        return self._row_to_entry(row) if row else None

    async def _fetch_usage(self) -> Dict[Tuple[str, int], int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT rank_type, dungeon_id, uses FROM ranking_usage")
            rows = await cursor.fetchall()
        return {(row[0], int(row[1])): int(row[2] or 0) for row in rows}

    async def load_cache(self, force: bool = False):
        """Refresh the in-memory mapping cache (autocomplete must never hit the DB)."""
        async with self._cache_lock:
            if self._cache_loaded and not force:
                return
            self._entries = await self._fetch_entries()
            self._usage = await self._fetch_usage()
            self._cache_loaded = True

    def invalidate_cache(self):
        self._cache_loaded = False

    async def insert_entry(self, rank_type: str, dungeon_id: int, name: str, aliases: List[str],
                           user_id: Optional[int], verified: bool, last_entries: int) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO ranking_dungeons
                   (rank_type, dungeon_id, name, aliases, verified, last_entries, last_checked_ts,
                    created_by, created_at, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rank_type, int(dungeon_id), name, ",".join(aliases or []),
                 1 if verified else 0, int(last_entries or 0), now, user_id, now, user_id, now),
            )
            await db.commit()
        self.invalidate_cache()

    async def update_entry(self, rank_type: str, dungeon_id: int, *, name: Optional[str] = None,
                           aliases: Optional[List[str]] = None, user_id: Optional[int] = None,
                           verified: Optional[bool] = None, last_entries: Optional[int] = None) -> None:
        fields: List[str] = []
        params: List = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if aliases is not None:
            fields.append("aliases = ?")
            params.append(",".join(aliases))
        if verified is not None:
            fields.append("verified = ?")
            params.append(1 if verified else 0)
        if last_entries is not None:
            fields.append("last_entries = ?")
            params.append(int(last_entries))
            fields.append("last_checked_ts = ?")
            params.append(int(time.time()))
        if user_id is not None:
            fields.append("updated_by = ?")
            params.append(user_id)
            fields.append("updated_at = ?")
            params.append(int(time.time()))
        if not fields:
            return
        params.extend([rank_type, int(dungeon_id)])
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE ranking_dungeons SET {', '.join(fields)} "
                "WHERE rank_type = ? AND dungeon_id = ?",
                params,
            )
            await db.commit()
        self.invalidate_cache()

    async def delete_entry(self, rank_type: str, dungeon_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM ranking_dungeons WHERE rank_type = ? AND dungeon_id = ?",
                (rank_type, int(dungeon_id)),
            )
            await db.commit()
        self.invalidate_cache()

    async def record_usage(self, rank_type: str, dungeon_id: Optional[int]) -> None:
        """Track how often a leaderboard is opened (drives autocomplete ordering)."""
        if dungeon_id is None:
            return
        try:
            now = int(time.time())
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO ranking_usage (rank_type, dungeon_id, uses, last_used_ts)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(rank_type, dungeon_id)
                       DO UPDATE SET uses = uses + 1, last_used_ts = excluded.last_used_ts""",
                    (rank_type, int(dungeon_id), now),
                )
                await db.commit()
            key = (rank_type, int(dungeon_id))
            self._usage[key] = self._usage.get(key, 0) + 1
        except Exception as e:
            logger.debug(f"Failed to record ranking usage: {e}")

    async def record_scan(self, rank_type: str, dungeon_id: int, entries: int) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO ranking_scan (rank_type, dungeon_id, entries, checked_ts)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(rank_type, dungeon_id)
                       DO UPDATE SET entries = excluded.entries, checked_ts = excluded.checked_ts""",
                    (rank_type, int(dungeon_id), int(entries or 0), int(time.time())),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"Failed to record scan result: {e}")

    async def audit(self, user_id: Optional[int], action: str, rank_type: str,
                    dungeon_id: Optional[int], old_name: Optional[str] = None,
                    new_name: Optional[str] = None, old_aliases: Optional[str] = None,
                    new_aliases: Optional[str] = None) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO ranking_map_audit
                       (ts, user_id, action, rank_type, dungeon_id,
                        old_name, new_name, old_aliases, new_aliases)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (int(time.time()), user_id, action, rank_type,
                     int(dungeon_id) if dungeon_id is not None else None,
                     old_name, new_name, old_aliases, new_aliases),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to write ranking audit row: {e}")

    async def log_change(self, interaction: discord.Interaction, description: str) -> None:
        """Best-effort audit post to the staff log channel."""
        channel_id = getattr(settings, "MOD_CHANNEL_LOG_ID", None)
        if not channel_id:
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                return
            embed = discord.Embed(
                title="🗺️ Ranking Registry Updated",
                description=description,
                color=RANKING_ACCENT,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"by {interaction.user} ({interaction.user.id})")
            await channel.send(embed=embed)
        except Exception as e:
            logger.debug(f"Failed to post ranking registry log: {e}")

    # ------------------------------------------------------------------
    # API probing + autocomplete
    # ------------------------------------------------------------------
    async def probe_entry(self, rank_type: str, dungeon_id: int) -> Tuple[bool, int, Optional[str]]:
        """Probe the live API for one ID. Returns (verified, entry_count, error)."""
        try:
            rank_name = _build_rank_name(rank_type, dungeon_id)
        except (TypeError, ValueError) as e:
            return False, 0, str(e)
        try:
            response = await get_rank_list(rank_name, page=1)
        except Exception as e:
            logger.warning(f"Ranking probe failed for {rank_type}:{dungeon_id}: {e}")
            return False, 0, str(e)
        if not response or response.get("code") != 0:
            return False, 0, "API returned an error"
        total = int((response.get("result") or {}).get("rank_total_len", 0) or 0)
        if total <= 0:
            return False, 0, "board returned no entries"
        return True, total, None

    async def probe_range(self, rank_type: str, from_id: int, to_id: int) -> List[dict]:
        """Sequentially probe an inclusive ID range (staff-only discovery helper)."""
        results: List[dict] = []
        for dungeon_id in range(from_id, to_id + 1):
            verified, count, error = await self.probe_entry(rank_type, dungeon_id)
            results.append({
                "dungeon_id": dungeon_id,
                "verified": verified,
                "entries": count,
                "error": error,
            })
            await self.record_scan(rank_type, dungeon_id, count)
            await asyncio.sleep(PROBE_DELAY)
        return results

    async def _autocomplete_choices(self, query: str,
                                    rank_type: Optional[str] = None, *, legacy: bool = False) -> List[app_commands.Choice]:
        """Use extracted names for viewing; retain the old registry for management."""
        if legacy:
            await self.load_cache()
        entries = self._entries if legacy else self.dungeon_assets.entries()
        scored = []
        for entry in entries:
            if rank_type and entry["rank_type"] != rank_type:
                continue
            score = _score_entry(entry, query)
            if score is None:
                continue
            usage = self._usage.get((entry["rank_type"], entry["dungeon_id"]), 0) if legacy else 0
            scored.append((_autocomplete_sort_key(entry, score, usage), entry))
        scored.sort(key=lambda item: item[0])
        return [
            app_commands.Choice(
                name=_entry_label(entry["rank_type"], entry["dungeon_id"], entry["name"]),
                value=_tag(entry["rank_type"], entry["dungeon_id"]),
            )
            for _, entry in scored[:MAX_AUTOCOMPLETE_CHOICES]
        ]

    async def dungeon_acomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for /ranking view dungeon.

        A leading ``hr``/``st`` token in the typed text filters the list; the
        ``rank_type`` option is used too when the user filled it before focusing
        this option (Discord only sends options that were already chosen).
        """
        hint = getattr(interaction.namespace, "rank_type", None)
        prefix_type, term = _split_type_prefix(current)
        return await self._autocomplete_choices(term, prefix_type or hint)

    async def entry_acomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for the registry-management commands."""
        prefix_type, term = _split_type_prefix(current)
        return await self._autocomplete_choices(term, prefix_type, legacy=True)

    async def resolve_dungeon_input(self, raw: str, rank_type_hint: Optional[str] = None, *, legacy: bool = False) -> dict:
        """Resolve free-typed input into a leaderboard target.

        Accepts "hr:22" | "hr 22" | "22" | "Frost Blade" | "st frost".

        Returns {"status": "ok", "rank_type", "dungeon_id", "entry"}
        or {"status": "multiple", "candidates": [...], "reason": ...}
        or {"status": "not_found", "message": ...}
        """
        if legacy:
            await self.load_cache()
        entries = self._entries if legacy else self.dungeon_assets.entries()
        text = (raw or "").strip()
        if not text:
            return {"status": "not_found", "message": "Please provide a dungeon name or ID."}

        tag_match = re.match(r"^([a-zA-Z]{2,4})\s*[:_\-\s]\s*(\d{1,6})$", text)
        if tag_match and tag_match.group(1).lower() in RANKING_TYPES:
            rank_type = tag_match.group(1).lower()
            dungeon_id = int(tag_match.group(2))
            return {
                "status": "ok",
                "rank_type": rank_type,
                "dungeon_id": dungeon_id,
                "entry": next((e for e in entries if e["rank_type"] == rank_type and e["dungeon_id"] == dungeon_id), None),
            }

        prefix_type, term = _split_type_prefix(text)
        rank_type = prefix_type or rank_type_hint

        if term.isdigit():
            dungeon_id = int(term)
            matches = [e for e in entries
                       if e["dungeon_id"] == dungeon_id
                       and (not rank_type or e["rank_type"] == rank_type)]
            if len(matches) == 1:
                entry = matches[0]
                return {"status": "ok", "rank_type": entry["rank_type"],
                        "dungeon_id": dungeon_id, "entry": entry}
            if not matches:
                if rank_type:
                    return {"status": "ok", "rank_type": rank_type,
                            "dungeon_id": dungeon_id, "entry": None}
                return {
                    "status": "multiple",
                    "reason": "untyped_id",
                    "candidates": [{"rank_type": key, "dungeon_id": dungeon_id, "name": None}
                                   for key, meta in RANKING_TYPES.items() if meta.get("enabled")],
                }
            return {
                "status": "multiple",
                "reason": "ambiguous_id",
                "candidates": [{"rank_type": e["rank_type"], "dungeon_id": e["dungeon_id"],
                                "name": e["name"]} for e in matches],
            }

        scored = []
        for entry in entries:
            if rank_type and entry["rank_type"] != rank_type:
                continue
            score = _score_entry(entry, term)
            if score is not None:
                scored.append((score[0], entry))
        if not scored:
            return {"status": "not_found", "message": f"No extracted leaderboard name matched `{text}`."}
        scored.sort(key=lambda item: (item[0], item[1]["rank_type"], item[1]["dungeon_id"]))
        best = scored[0][0]
        best_entries = [entry for rank, entry in scored if rank <= max(1, best)]
        if len(best_entries) == 1:
            entry = best_entries[0]
            return {"status": "ok", "rank_type": entry["rank_type"],
                    "dungeon_id": entry["dungeon_id"], "entry": entry}
        return {
            "status": "multiple",
            "reason": "ambiguous_name",
            "candidates": [{"rank_type": e["rank_type"], "dungeon_id": e["dungeon_id"],
                            "name": e["name"]}
                           for e in best_entries[:MAX_AUTOCOMPLETE_CHOICES]],
        }

    # ------------------------------------------------------------------
    # Delegation to WWMCog (player profile pipeline lives there)
    # ------------------------------------------------------------------
    async def build_player_profile_view(self, identifier: str, interaction: discord.Interaction,
                                        ephemeral: bool = True):
        """Build a PlayerProfileView via WWMCog (which owns the profile machinery)."""
        wwm_cog = self.bot.get_cog("WWMCog") if self.bot else None
        if wwm_cog is None:
            logger.warning("WWMCog is not loaded — cannot build player profile views")
            return None, None
        return await wwm_cog._build_player_profile_view(identifier, interaction, ephemeral=ephemeral)

    async def resolve_player_identifier(self, identifier: str) -> tuple:
        """Resolve a number ID / nickname to (pid, hostnum, data) via WWMCog."""
        wwm_cog = self.bot.get_cog("WWMCog") if self.bot else None
        if wwm_cog is not None:
            return await wwm_cog._resolve_player_identifier(identifier)
        from utility.wwm import resolve_player_identifier as _fallback
        return await _fallback(identifier)

    async def _resolve_user_pid(self, interaction: discord.Interaction) -> Optional[str]:
        """Resolve the calling user's WWM PID from the verified_members table."""
        try:
            async with aiosqlite.connect(VERIFICATION_DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT player_pid FROM verified_members WHERE user_id = ?",
                    (interaction.user.id,),
                )
                row = await cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.warning(f"Failed to resolve user PID for {interaction.user.id}: {e}")
            return None

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str, ephemeral: bool = True) -> None:
        """Reply whether or not the interaction has already been responded to."""
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    async def show_ranking_results(self, interaction: discord.Interaction, rank_type: str,
                                   dungeon_id: Optional[int], page: int = 1,
                                   target_pid: Optional[str] = None,
                                   dungeon_label: Optional[str] = None,
                                   jump_to_target: bool = True) -> None:
        """Fetch and render one leaderboard page.

        The caller must have deferred the interaction already — the result is
        written with ``edit_original_response``.
        """
        meta = RANKING_TYPES.get(rank_type)
        if meta is None:
            await self._respond(interaction, f"❌ Unknown ranking type `{rank_type}`.")
            return
        try:
            rank_name = _build_rank_name(rank_type, dungeon_id)
        except (TypeError, ValueError):
            await self._respond(interaction, "❌ A numeric dungeon ID is required for this leaderboard.")
            return

        try:
            user_pid = await self._resolve_user_pid(interaction)

            # Jump to the target player's page when an identifier was supplied
            page = max(1, int(page or 1))
            if target_pid and jump_to_target and page == 1:
                probe_response = await get_rank_list(rank_name, page=1, pid=target_pid)
                if probe_response and probe_response.get('code') == 0:
                    probe_rank = (probe_response.get('result') or {}).get('my_rank', -1)
                    if probe_rank is not None and probe_rank >= 0:
                        target_page = probe_rank // ITEMS_PER_PAGE + 1
                        if page == 1 and target_page > 1:
                            page = target_page

            fetch_pid = target_pid if target_pid else user_pid
            response = await get_rank_list(rank_name, page=page, pid=fetch_pid)

            if not response or response.get('code') != 0:
                await self._respond(
                    interaction,
                    "❌ Failed to fetch leaderboard data. This dungeon may not exist or the API returned an error.",
                )
                return

            result = response.get('result', {}) or {}
            rank_list = result.get('rank_list', []) or []
            total_entries = int(result.get('rank_total_len', 0) or 0)
            my_data = result.get('my_data', {}) or {}
            my_rank = result.get('my_rank', -1)

            if not rank_list and not total_entries:
                await self._respond(
                    interaction,
                    "❌ No data found for this ranking. The dungeon ID may be incorrect.",
                )
                return

            total_pages = max(1, (total_entries + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
            clamped_page = max(1, min(page, total_pages))
            if clamped_page != page:
                response = await get_rank_list(rank_name, page=clamped_page, pid=fetch_pid)
                if not response or response.get('code') != 0:
                    await self._respond(interaction, "❌ Could not load the last available page. Please try again.")
                    return
                result = response.get('result') or {}
                rank_list = result.get('rank_list') or []
                my_data = result.get('my_data') or {}
                my_rank = result.get('my_rank', -1)
            page = clamped_page

            my_score = my_data.get('score') if my_data else None

            # Score needed to break into the board (last place)
            last_place_score = None
            last_place_nickname = None
            if total_entries > 0:
                if total_pages == page:
                    if rank_list:
                        last_entry = rank_list[-1]
                        last_place_score = last_entry.get('score')
                        last_place_nickname = _extract_nickname(last_entry.get('player_info', {}))
                else:
                    try:
                        last_response = await get_rank_list(rank_name, page=total_pages, pid=user_pid)
                        if last_response and last_response.get('code') == 0:
                            last_page_list = (last_response.get('result') or {}).get('rank_list', []) or []
                            if last_page_list:
                                last_entry = last_page_list[-1]
                                last_place_score = last_entry.get('score')
                                last_place_nickname = _extract_nickname(last_entry.get('player_info', {}))
                    except Exception as e:
                        logger.warning(f"Failed to fetch last place data: {e}")

            target_nickname = None
            if target_pid and my_data:
                target_nickname = _extract_nickname(my_data.get('player_info', {}))

            # Always prefer the authoritative extracted name over caller/legacy labels.
            if dungeon_id is not None:
                entry = next((e for e in self.dungeon_assets.entries()
                              if e["rank_type"] == rank_type and e["dungeon_id"] == dungeon_id), None)
                dungeon_label = entry["name"] if entry else None

            view = RankingResultsView(
                cog=self,
                rank_type=rank_type,
                dungeon_id=dungeon_id,
                rank_name=rank_name,
                page=page,
                dungeon_label=dungeon_label,
                target_pid=target_pid,
                owner_id=interaction.user.id,
            )
            view.total_pages = total_pages
            view.total_entries = total_entries
            view.my_rank = my_rank if (my_rank is not None and my_rank >= 0) else None
            view.queried_pid = fetch_pid
            view.my_score = my_score
            view.rank_list = rank_list
            view.last_place_score = last_place_score
            view.last_place_nickname = last_place_nickname
            view.target_nickname = target_nickname
            if not dungeon_label and view.banner:
                view.dungeon_label = view.banner.get('title')
            files = []
            if _art_images(view.banner):
                try:
                    for index, image in enumerate(_art_images(view.banner)):
                        files.append(discord.File(image['path'], filename=_art_filename(index)))
                except OSError:
                    logger.warning('Dungeon gallery unavailable; rendering text leaderboard')
                    for file in files:
                        file.close()
                    files = []
                    view.banner = None
            view._rebuild()
            try:
                await interaction.edit_original_response(content=None, embed=None, attachments=files, view=view, allowed_mentions=discord.AllowedMentions.none())
            finally:
                for file in files:
                    file.close()
            try:
                await self.record_usage(rank_type, dungeon_id)
            except Exception:
                logger.warning('Leaderboard displayed but usage counter could not be updated', exc_info=True)
        except Exception as e:
            logger.error(f"Ranking results failed: {str(e)}", exc_info=True)
            try:
                await interaction.edit_original_response(
                    content=None, embed=None, attachments=[],
                    view=LayoutView().add_item(Container(TextDisplay("❌ Failed to load ranking data. Please try again.")))
                )
            except Exception:
                pass

    async def show_dungeon_picker(self, interaction: discord.Interaction, rank_type: str = "hr",
                                  target_pid: Optional[str] = None, page: int = 0) -> None:
        """Render (or replace the message with) the dungeon browser."""
        entries = self.dungeon_assets.entries()
        view = DungeonPickerView(self, entries, rank_type=rank_type, page=page,
                                 target_pid=target_pid, owner_id=interaction.user.id)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=None, attachments=[], view=view)
        else:
            await interaction.response.send_message(content=None, view=view)

    async def show_map_list(self, interaction: discord.Interaction, rank_type: Optional[str] = None,
                            page: int = 0) -> None:
        """Render (or replace the message with) the registry listing."""
        entries = await self._fetch_entries()
        view = RankingMapListView(self, entries, rank_type=rank_type, page=page,
                                  owner_id=interaction.user.id)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=None, attachments=[], view=view)
        else:
            await interaction.response.send_message(content=None, view=view)

    # ------------------------------------------------------------------
    # /ranking view
    # ------------------------------------------------------------------
    @ranking_group.command(
        name="view",
        description="View an HR/ST leaderboard by dungeon name (autocomplete) or ID.",
    )
    @app_commands.describe(
        dungeon="Type a dungeon name (autocomplete), or an ID like '26' / 'hr:26'",
        player="Optional: player's 10-digit Number ID or nickname, to jump to their rank",
        page="Optional page number (20 entries per page)",
        rank_type="Optional: narrow the lookup to one leaderboard type",
    )
    @app_commands.choices(rank_type=TYPE_CHOICES)
    @app_commands.autocomplete(dungeon=dungeon_acomplete)
    async def ranking_view(self, interaction: discord.Interaction, dungeon: str,
                           player: Optional[str] = None, page: int = 1,
                           rank_type: Optional[str] = None):
        """Open a leaderboard: pick a mapped dungeon by name, or pass a raw ID."""
        await interaction.response.defer()

        target_pid = None
        if player and player.strip():
            pid, _, _ = await self.resolve_player_identifier(player.strip())
            if not pid:
                await interaction.edit_original_response(
                    content=f"❌ No player found matching `{player}`. "
                            "Try a 10-digit Number ID or an exact nickname."
                )
                return
            target_pid = pid

        resolved = await self.resolve_dungeon_input(dungeon, rank_type)

        if resolved["status"] == "multiple":
            view = RankingEntryChoiceView(
                self,
                title="Which leaderboard?",
                candidates=resolved["candidates"],
                target_pid=target_pid,
                owner_id=interaction.user.id,
            )
            await interaction.edit_original_response(content=None, embed=None, view=view)
            return

        if resolved["status"] == "not_found":
            hint = ""
            if dungeon and dungeon.strip():
                hint = "\n\nChoose an extracted name from autocomplete, or use an ID such as `hr:26` / `st:26`."
            await interaction.edit_original_response(
                content=f"❌ {resolved.get('message', 'Not found.')}{hint}"
            )
            return

        await self.show_ranking_results(
            interaction,
            resolved["rank_type"],
            resolved["dungeon_id"],
            page=max(1, int(page or 1)),
            target_pid=target_pid,
            dungeon_label=(resolved.get("entry") or {}).get("name"),
        )

    def _check_add_rate_limit(self, user_id: int) -> bool:
        """Simple in-memory rate limiter for the trust-based /ranking map add."""
        now = time.time()
        window = [t for t in self._add_times.get(user_id, []) if now - t < ADD_COOLDOWN_WINDOW]
        if len(window) >= ADD_COOLDOWN_MAX:
            self._add_times[user_id] = window
            return False
        window.append(now)
        self._add_times[user_id] = window
        return True

    # ------------------------------------------------------------------
    # /ranking types
    # ------------------------------------------------------------------
    @ranking_group.command(name="types", description="List the supported leaderboard types and mapping counts.")
    async def ranking_types(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        entries = await self._fetch_entries()
        counts: Dict[str, int] = {}
        for entry in entries:
            counts[entry["rank_type"]] = counts.get(entry["rank_type"], 0) + 1

        lines = ["# 🏆 Leaderboard Types", ""]
        for key, meta in RANKING_TYPES.items():
            status = "✅ enabled" if meta.get("enabled") else "⏸️ disabled"
            try:
                sample = _build_rank_name(key, 22)
            except (TypeError, ValueError):
                sample = meta.get("fixed", "-")
            lines.append(
                f"{meta.get('emoji', '')} **{meta['label']}** — {meta['long']} · {status} · "
                f"{counts.get(key, 0)} mapped\n   ↳ `{sample}`"
            )
        lines.append("")
        lines.append("Use `/ranking view` to open a board and `/ranking map list` to browse the registry.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /ranking map add / list
    # ------------------------------------------------------------------
    @map_group.command(
        name="add",
        description="Map a dungeon name to an HR/ST leaderboard ID (anyone can add).",
    )
    @app_commands.describe(
        rank_type="Which leaderboard the ID belongs to",
        name="Name people will type (e.g. 'Frost Blade')",
        dungeon_id="The numeric ID the leaderboard uses",
        aliases="Optional comma-separated extra names (e.g. 'frost,fb')",
    )
    @app_commands.choices(rank_type=TYPE_CHOICES)
    async def map_add(self, interaction: discord.Interaction, rank_type: str, name: str,
                      dungeon_id: int, aliases: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not self._check_add_rate_limit(interaction.user.id):
            await interaction.followup.send(
                f"⏳ You're adding mappings too quickly — max {ADD_COOLDOWN_MAX} per "
                f"{ADD_COOLDOWN_WINDOW // 60} minutes.",
                ephemeral=True,
            )
            return

        clean_name = (name or "").strip()
        if not clean_name:
            await interaction.followup.send("❌ The name cannot be empty.", ephemeral=True)
            return
        if len(clean_name) > MAX_NAME_LENGTH:
            await interaction.followup.send(
                f"❌ The name is too long (max {MAX_NAME_LENGTH} characters).", ephemeral=True
            )
            return
        if dungeon_id is None or dungeon_id < 1 or dungeon_id > 9_999_999:
            await interaction.followup.send(
                "❌ The ID must be a positive number (up to 7 digits).", ephemeral=True
            )
            return
        alias_list = _parse_aliases(aliases)
        if len(",".join(alias_list)) > MAX_ALIASES_LENGTH:
            await interaction.followup.send(
                f"❌ Too many aliases (max {MAX_ALIASES_LENGTH} characters).", ephemeral=True
            )
            return

        existing = await self._fetch_entry(rank_type, dungeon_id)
        if existing:
            await interaction.followup.send(
                f"❌ `{_tag(rank_type, dungeon_id)}` is already mapped to **{existing['name']}**.\n"
                "Ask an admin/staff member to rename it with `/ranking map edit`.",
                ephemeral=True,
            )
            return

        verified, entry_count, error = await self.probe_entry(rank_type, dungeon_id)
        await self.insert_entry(rank_type, dungeon_id, clean_name, alias_list,
                                interaction.user.id, verified, entry_count)
        await self.audit(interaction.user.id, "add", rank_type, dungeon_id,
                         None, clean_name, None, ",".join(alias_list))

        same_name = [e for e in await self._fetch_entries(rank_type)
                     if e["name"].lower() == clean_name.lower()]
        lines = [f"{'✅' if verified else '⚠️'} Mapped **{clean_name}** → `{_tag(rank_type, dungeon_id)}`"]
        if verified:
            lines.append(f"✅ Verified — this board currently has **{entry_count:,}** entries.")
        else:
            lines.append(
                f"⚠️ **Not verified** — the API probe returned no entries ({error}). "
                "It was saved anyway so a staff member can review it."
            )
        if len(same_name) > 1:
            lines.append(
                f"ℹ️ {len(same_name)} {_type_label(rank_type)} mappings share this name — "
                "autocomplete shows the ID to disambiguate."
            )
        lines.append("\nOpen it with `/ranking view`.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

        await self.log_change(
            interaction,
            f"{interaction.user.mention} added `{_tag(rank_type, dungeon_id)}` → **{clean_name}**"
            + (" (verified)" if verified else " (⚠️ unverified)"),
        )

    @map_group.command(name="list", description="Show the dungeon name → ID registry.")
    @app_commands.describe(rank_type="Optional: only show one leaderboard type")
    @app_commands.choices(rank_type=TYPE_CHOICES)
    async def map_list(self, interaction: discord.Interaction, rank_type: Optional[str] = None):
        await interaction.response.defer()
        entries = await self._fetch_entries(rank_type)
        if not entries:
            scope = f" for **{_type_label(rank_type)}**" if rank_type else ""
            await interaction.followup.send(
                f"ℹ️ No mappings yet{scope}. Anyone can add one with `/ranking map add`."
            )
            return
        await self.show_map_list(interaction, rank_type=rank_type, page=0)

    async def _find_entry_for_input(self, raw: str) -> Optional[dict]:
        """Resolve autocompleted / free-typed input to an existing registry row."""
        resolved = await self.resolve_dungeon_input(raw, legacy=True)
        if resolved.get("status") == "ok" and resolved.get("entry"):
            return resolved["entry"]
        return None

    @map_group.command(name="edit", description="Rename / retarget a mapping (admin/staff only).")
    @app_commands.describe(
        entry="The mapping to edit",
        name="New display name",
        dungeon_id="New numeric ID (moves the mapping to another board)",
        aliases="New aliases (comma-separated, or '-' to clear)",
    )
    @app_commands.autocomplete(entry=entry_acomplete)
    @admin_or_staff()
    async def map_edit(self, interaction: discord.Interaction, entry: str,
                       name: Optional[str] = None, dungeon_id: Optional[int] = None,
                       aliases: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)

        current = await self._find_entry_for_input(entry)
        if current is None:
            await interaction.followup.send(
                "❌ No mapping matched that entry — pick one from the autocomplete list.", ephemeral=True
            )
            return

        new_name = name.strip() if name and name.strip() else None
        if new_name and len(new_name) > MAX_NAME_LENGTH:
            await interaction.followup.send(
                f"❌ The name is too long (max {MAX_NAME_LENGTH} characters).", ephemeral=True
            )
            return
        new_aliases: Optional[List[str]] = None
        if aliases is not None:
            new_aliases = [] if aliases.strip() in ("-", "") else _parse_aliases(aliases)

        target_id = current["dungeon_id"]
        moved = False
        if dungeon_id is not None and int(dungeon_id) != current["dungeon_id"]:
            if int(dungeon_id) < 1:
                await interaction.followup.send("❌ The ID must be a positive number.", ephemeral=True)
                return
            clash = await self._fetch_entry(current["rank_type"], int(dungeon_id))
            if clash:
                await interaction.followup.send(
                    f"❌ `{_tag(current['rank_type'], int(dungeon_id))}` already exists "
                    f"(**{clash['name']}**). Remove it first if you want to move this mapping.",
                    ephemeral=True,
                )
                return
            target_id = int(dungeon_id)
            moved = True

        if new_name is None and new_aliases is None and not moved:
            await interaction.followup.send(
                "ℹ️ Nothing to change — provide `name`, `dungeon_id` or `aliases`.", ephemeral=True
            )
            return

        final_aliases = new_aliases if new_aliases is not None else current["aliases"]
        final_name = new_name or current["name"]

        if moved:
            await self.delete_entry(current["rank_type"], current["dungeon_id"])
            verified, entry_count, _error = await self.probe_entry(current["rank_type"], target_id)
            await self.insert_entry(current["rank_type"], target_id, final_name, final_aliases,
                                    interaction.user.id, verified, entry_count)
        else:
            await self.update_entry(current["rank_type"], target_id, name=new_name,
                                    aliases=new_aliases, user_id=interaction.user.id)
            verified = bool(current.get("verified"))
            entry_count = current.get("last_entries")

        await self.audit(interaction.user.id, "edit", current["rank_type"], target_id,
                         current["name"], final_name, ",".join(current["aliases"]),
                         ",".join(final_aliases))

        lines = [f"✏️ Updated `{_tag(current['rank_type'], target_id)}` → **{final_name}**"]
        if moved:
            lines.append(f"↪️ Moved from `{_tag(current['rank_type'], current['dungeon_id'])}`.")
            if verified:
                lines.append(f"✅ Verified — **{entry_count:,}** entries.")
            else:
                lines.append("⚠️ The new ID did not return entries from the API probe.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        await self.log_change(
            interaction,
            f"{interaction.user.mention} edited `{_tag(current['rank_type'], target_id)}`: "
            f"**{current['name']}** → **{final_name}**" + (" (moved ID)" if moved else ""),
        )

    @map_group.command(name="remove", description="Remove a mapping (admin/staff only).")
    @app_commands.describe(entry="The mapping to remove")
    @app_commands.autocomplete(entry=entry_acomplete)
    @admin_or_staff()
    async def map_remove(self, interaction: discord.Interaction, entry: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        current = await self._find_entry_for_input(entry)
        if current is None:
            await interaction.followup.send(
                "❌ No mapping matched that entry — pick one from the autocomplete list.", ephemeral=True
            )
            return

        tag = _tag(current["rank_type"], current["dungeon_id"])
        await self.delete_entry(current["rank_type"], current["dungeon_id"])
        await self.audit(interaction.user.id, "remove", current["rank_type"],
                         current["dungeon_id"], current["name"], None,
                         ",".join(current["aliases"]), None)
        await interaction.followup.send(
            f"🗑️ Removed **{current['name']}** (`{tag}`) from the registry.", ephemeral=True
        )
        await self.log_change(
            interaction,
            f"{interaction.user.mention} removed `{tag}` (**{current['name']}**)",
        )

    # ------------------------------------------------------------------
    # /ranking map verify (staff)
    # ------------------------------------------------------------------
    @map_group.command(name="verify", description="Re-check mapped IDs against the live API (admin/staff only).")
    @app_commands.describe(rank_type="Optional: only verify one leaderboard type",
                           max_entries="Safety cap on how many entries to check (default 60)")
    @app_commands.choices(rank_type=TYPE_CHOICES)
    @admin_or_staff()
    async def map_verify(self, interaction: discord.Interaction, rank_type: Optional[str] = None,
                         max_entries: int = 60):
        await interaction.response.defer(thinking=True)
        max_entries = max(1, min(int(max_entries or 60), SCAN_MAX_RANGE))

        entries = await self._fetch_entries(rank_type)
        if not entries:
            await interaction.followup.send("ℹ️ Nothing to verify — the registry is empty.")
            return

        targets = entries[:max_entries]
        checked = verified_count = 0
        invalid: List[str] = []
        for entry in targets:
            ok, count, _error = await self.probe_entry(entry["rank_type"], entry["dungeon_id"])
            await self.update_entry(entry["rank_type"], entry["dungeon_id"],
                                    verified=ok, last_entries=count if ok else 0)
            checked += 1
            if ok:
                verified_count += 1
            else:
                invalid.append(f"`{_tag(entry['rank_type'], entry['dungeon_id'])}` {entry['name']}")
            await asyncio.sleep(PROBE_DELAY)

        lines = [
            "# 🔄 Registry Verification",
            f"Checked **{checked}** of {len(entries)} mapping(s).",
            f"✅ Verified: **{verified_count}**",
            f"⚠️ No entries returned: **{len(invalid)}**",
        ]
        if invalid:
            lines.append("")
            lines.append("**Needs review:**")
            lines.extend(f"• {item}" for item in invalid[:20])
            if len(invalid) > 20:
                lines.append(f"• …and {len(invalid) - 20} more")
        if len(entries) > checked:
            lines.append("")
            lines.append("-# Run the command again to continue where this stopped.")
        await interaction.followup.send("\n".join(lines))

    # ------------------------------------------------------------------
    # /ranking map scan (staff)
    # ------------------------------------------------------------------
    @map_group.command(name="scan", description="Probe an ID range to discover valid leaderboard IDs (admin/staff only).")
    @app_commands.describe(rank_type="Which leaderboard to scan",
                           from_id="First ID in the range (inclusive)",
                           to_id="Last ID in the range (inclusive)")
    @app_commands.choices(rank_type=TYPE_CHOICES)
    @admin_or_staff()
    async def map_scan(self, interaction: discord.Interaction, rank_type: str,
                       from_id: int, to_id: int):
        await interaction.response.defer(thinking=True)

        from_id, to_id = int(from_id), int(to_id)
        if from_id > to_id:
            from_id, to_id = to_id, from_id
        if from_id < 1:
            from_id = 1
        if to_id - from_id + 1 > SCAN_MAX_RANGE:
            await interaction.followup.send(
                f"❌ Please scan at most {SCAN_MAX_RANGE} IDs per run "
                f"(you asked for {to_id - from_id + 1})."
            )
            return

        await interaction.followup.send(
            f"🔎 Scanning `{_type_label(rank_type)}` IDs {from_id}–{to_id} "
            f"({to_id - from_id + 1} requests) — this can take a while…"
        )

        results = await self.probe_range(rank_type, from_id, to_id)
        found = [row for row in results if row["entries"]]
        mapped = {entry["dungeon_id"]: entry for entry in await self._fetch_entries(rank_type)}

        lines = [
            f"# 🔎 Scan Results — {_type_label(rank_type)}",
            f"Range **{from_id}–{to_id}** · found **{len(found)}** board(s) with data.",
        ]
        if found:
            lines.append("")
            lines.append("**Boards with data:**")
            for row in found[:30]:
                entry = mapped.get(row["dungeon_id"])
                label = entry["name"] if entry else "— unmapped —"
                lines.append(
                    f"• `{_tag(rank_type, row['dungeon_id'])}` · {row['entries']:,} entries · {label}"
                )
            if len(found) > 30:
                lines.append(f"• …and {len(found) - 30} more")

        unmapped = [row for row in found if row["dungeon_id"] not in mapped]
        if unmapped:
            lines.append("")
            lines.append(
                f"**{len(unmapped)} unmapped ID(s):** "
                + ", ".join(f"`{_tag(rank_type, row['dungeon_id'])}`" for row in unmapped[:20])
            )
            lines.append("-# Anyone can name them with `/ranking map add`.")
        if not found:
            lines.append("")
            lines.append("No boards in this range returned entries.")
        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
