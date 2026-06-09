"""
Leaderboard Cog — Components V2 leaderboard with auto-refresh.
Supports MULTIPLE simultaneous leaderboards of different types.

Supports leaderboard types:
  - elegance: fashion score (from "fashion" API field)
  - martial_mastery: max_xiuwei_kungfu (from "base" API field)
  - exploration_mastery: XIUWEI_EXPLORE (from "attr" API field)

Architecture:
  - Admin posts a leaderboard to a channel with /leaderboard command
  - JSON file stores a list [{channel_id, message_id, type, guild_id}, ...]
  - A single background task refreshes ALL active leaderboards every 60 seconds
  - A "Check My Rank" button lets users see their position even if off-screen

Breaking Army Leaderboard:
  - Displays the two weekly Breaking Army sessions (schedule_infos "1" and "2" from play type 13)
  - Each session has a boss, start time, and lasts 2 hours
  - Tracks: best_time (seconds), attempts count per player per session
  - Lifecycle:
    - "upcoming"    — more than 1 hour before start → show next BA info, clear old data
    - "active"      — within the 2-hour window → collect timings in real-time
    - "finalized"   — after 2 hours have elapsed → lock results, show final standings
"""
import asyncio
import datetime

import discord
import json
import os
import aiosqlite
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow
from typing import Optional, List, Tuple, Dict, Any

import settings
from settings import logger, BASE_DIR, WWM_UID, WWM_REDIS_PLAYER_URL, CLUB_ID, WWM_FULL_GUILD_URL, DISCORD_SERVER_ID
from utility.wwm import _wwm_api_post
from cogs.view_registry import register
from utility.api_constants import BOSS_NAMES

DB_PATH = BASE_DIR / "data" / "guild_verification.db"
CONFIG_PATH = BASE_DIR / "data" / "leaderboard_config.json"
BA_TIMINGS_PATH = BASE_DIR / "data" / "breaking_army_timings.json"
BA_SCHEDULE_PATH = BASE_DIR / "data" / "breaking_army_schedule.json"

LEADERBOARD_COLORS = {
    "elegance": 0xFF69B4,
    "martial_mastery": 0xE74C3C,
    "exploration_mastery": 0x2ECC71,
    "playtime": 0x3498DB,
    "breaking_army": 0xBB8FCE,
}

LEADERBOARD_EMOJIS = {
    "elegance": "💃",
    "martial_mastery": "⚔️",
    "exploration_mastery": "🗺️",
    "playtime": "⌛",
    "breaking_army": "💀",
}

LB_API_FIELDS = {
    "elegance":         (["fashion", "base"], 10403),
    "martial_mastery":   (["base"], 10595),
    "exploration_mastery": (["attr", "base"], 10595),
    "playtime":          (["base"], 10595),
}


def _extract_score(lb_type: str, player_data: dict) -> float:
    """Pull the correct score value from player_data for a given leaderboard type."""
    if lb_type == "elegance":
        fashion = player_data.get("fashion", {})
        if isinstance(fashion, dict):
            return fashion.get("score", 0) or 0
        return float(fashion) if isinstance(fashion, (int, float)) else 0

    if lb_type == "playtime":
        base = player_data.get("base", {})
        online_seconds = base.get("online_time", 0) or 0
        return round(online_seconds / 3600, 1)  # convert to hours

    attr_map = {
        "exploration_mastery": "XIUWEI_EXPLORE",
    }
    key = attr_map.get(lb_type)
    if key:
        attr = player_data.get("attr", {})
        return round(float(attr.get(key, 0)), 1)

    logger.debug(f"Testing martial mastery extraction for player {player_data.get('base', {}).get('nickname', 'Unknown')}")

    if lb_type == "martial_mastery":
        logger.debug(f"Extracting martial mastery score for player {player_data.get('base', {}).get('nickname', 'Unknown')}")
        base = player_data.get("base", {})
        return base.get("max_xiuwei_kungfu", 0) or 0
    return 0


# ── Breaking Army helper utilities ───────────────────────────────────
def _gmt8_now() -> datetime.datetime:
    """Return current time as timezone-aware datetime in GMT+8."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return now_utc + datetime.timedelta(hours=8)


def _weekday_start_ts(weekday: int, hour: int, minute: int) -> float:
    """
    Given a weekday (0=Mon..6=Sun), hour, minute in GMT+8,
    return the Unix timestamp (UTC) of the most recent occurrence
    of that weekday+time that is <= current GMT+8 time.
    """
    gmt8_now = _gmt8_now()
    target = gmt8_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - target.weekday()) % 7
    target += datetime.timedelta(days=days_ahead)
    if target > gmt8_now:
        target -= datetime.timedelta(days=7)
    # Convert back to UTC timestamp
    utc_target = target - datetime.timedelta(hours=8)
    return utc_target.timestamp()


def _weekday_name(weekday: int) -> str:
    """Map 0=Mon..6=Sun to short name."""
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekday]


# ── BA Schedule & State ──────────────────────────────────────────────
# Tested working endpoint for club play info
CLUB_INFO_URL = WWM_FULL_GUILD_URL

# Play type 13 = Breaking Army
BA_PLAY_TYPE = 13


def _fetch_ba_schedule() -> Dict[str, Any]:
    """
    Fetch the guild club info and extract Breaking Army (play type 13) schedule.
    Returns dict with session "1" and "2" schedule info + boss_id, or empty dict on failure.
    """
    payload = {
        "club_id": CLUB_ID,
        "uid": WWM_UID,
        "field_info": {
            "member": [],
            "play": []
        },
        "hostnum": 10103
    }
    try:
        response = _wwm_api_post(CLUB_INFO_URL, payload)
        if not response or 'result' not in response:
            logger.warning("BA schedule: failed to fetch club info")
            return {}
        plays = response['result'].get('play', {}).get('plays', {})
        ba_play = plays.get(int(BA_PLAY_TYPE), {})
        schedule_infos = ba_play.get('schedule_infos', {})
        if not schedule_infos:
            logger.warning("BA schedule: no schedule_infos found in play type 13")
            return {}
        result = {}
        logger.debug(f"BA schedule: raw schedule_infos: {schedule_infos}")
        for session_key_int in (1, 2):
            info = schedule_infos.get(session_key_int)
            session_key_str = str(session_key_int)
            logger.debug(f"BA schedule: session {session_key_str} info: {info}")
            if info:
                # API weekday = 1=Mon, 2=Tue, 3=Wed, ..., 7=Sun
                # Python weekday = 0=Mon, 1=Tue, 2=Wed, ..., 6=Sun
                api_weekday = info.get("weekday", 0)
                py_weekday = (api_weekday - 1) % 7
                result[session_key_str] = {
                    "weekday": py_weekday,
                    "start_time": info.get("start_time", [0, 0]),
                    "boss_id": info.get("boss_id", 0),
                    "play_space_id": info.get("play_space_id", ""),
                    "transport_id": info.get("transport_id", 0),
                }
        return result
    except Exception as e:
        logger.error(f"BA schedule fetch error: {e}")
        return {}


def _compute_session_ts(schedule_entry: dict) -> Tuple[float, float, float, float]:
    """
    Compute both this week's occurrence and next week's occurrence timestamps.
    Returns (this_week_start, this_week_end, next_week_start, next_week_end).
    All UTC Unix timestamps.
    """
    weekday = schedule_entry["weekday"]
    hour, minute = schedule_entry["start_time"]
    start_ts = _weekday_start_ts(weekday, hour, minute)
    end_ts = start_ts + 7200  # 2 hours duration
    next_start = start_ts + 7 * 86400
    next_end = end_ts + 7 * 86400
    return start_ts, end_ts, next_start, next_end


def _get_ba_state(schedule: dict, timings: Optional[dict] = None) -> Dict[str, Any]:
    """
    Given a fetched BA schedule (dict with keys "1", "2"),
    compute the current state of each session.

    If timings dict is provided, finalized sessions will use the boss info
    from stored timings instead of the current schedule (to avoid displaying
    a new boss name with old data when the boss changes between weeks).

    Returns dict with keys:
      - sessions: { "1": {state, start_ts, end_ts, boss_id, boss_name, ...}, "2": {...} }
      - week_start_ts: approximate Monday 5AM GMT+8 ts (for grouping)
    """
    NOW_BUFFER = 120  # 2 min grace period after end
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    sessions = {}
    for session_key in ("1", "2"):
        entry = schedule.get(session_key)
        if not entry:
            sessions[session_key] = {"state": "unknown", "boss_name": "Unknown"}
            continue
        boss_id = entry.get("boss_id", 0)
        boss_name = BOSS_NAMES.get(int(boss_id), f"Boss #{boss_id}")
        weekday = entry["weekday"]
        hour, minute = entry["start_time"]

        # Compute this week's occurrence
        this_start, this_end, next_start, next_end = _compute_session_ts(entry)

        # Determine state using this week's timestamps, not next week's
        if now_ts < this_start:
            # Haven't started this week yet
            display_start = this_start
            display_end = this_end
            time_until = this_start - now_ts
            if time_until <= 3600:
                state = "upcoming_soon"
            else:
                state = "upcoming"
        elif now_ts <= this_end:
            # Currently active
            display_start = this_start
            display_end = this_end
            state = "active"
        else:
            # This week's session is over.
            # Stay finalized until 1 hour before NEXT week's session
            time_until_next = next_start - now_ts
            if time_until_next <= 3600:
                # Less than 1 hour until next week's BA — show upcoming
                display_start = next_start
                display_end = next_end
                state = "upcoming_soon" if time_until_next > 0 else "upcoming"
            else:
                # Show finalized results from this week
                display_start = this_start
                display_end = this_end
                state = "finalized"

                # Use boss info from stored timings (not current schedule)
                # to prevent showing a new week's boss name with old data
                if timings:
                    stored_session = timings.get(f"session_{session_key}", {})
                    stored_boss_id = stored_session.get("boss_id")
                    stored_boss_name = stored_session.get("boss_name")
                    if stored_boss_id is not None:
                        boss_id = int(stored_boss_id)
                        boss_name = stored_boss_name or BOSS_NAMES.get(boss_id, f"Boss #{boss_id}")

        sessions[session_key] = {
            "state": state,
            "start_ts": int(display_start),
            "end_ts": int(display_end),
            "boss_id": int(boss_id),
            "boss_name": boss_name,
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
            "weekday_name": _weekday_name(weekday),
        }
    # Compute the week start (Monday 5AM GMT+8) from session 1's start
    week_start_ts = None
    for s in sessions.values():
        if s.get("state") != "unknown" and "start_ts" in s:
            # Align to the Monday 5AM of that start
            gmt8_start = datetime.datetime.fromtimestamp(s["start_ts"] + 8 * 3600, tz=datetime.timezone.utc)
            adjusted = gmt8_start - datetime.timedelta(hours=5)
            monday = adjusted - datetime.timedelta(days=adjusted.weekday())
            monday_5am = monday.replace(hour=5, minute=0, second=0, microsecond=0)
            week_start_ts = int(monday_5am.timestamp() - 8 * 3600)
            break
    if week_start_ts is None:
        week_start_ts = 0
    return {"sessions": sessions, "week_start_ts": week_start_ts}


# ── BA Timings Storage ───────────────────────────────────────────────
def _load_ba_timings() -> dict:
    """Load BA timings from JSON file."""
    try:
        if os.path.exists(BA_TIMINGS_PATH):
            with open(BA_TIMINGS_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load BA timings: {e}")
    return {}


def _save_ba_timings(data: dict):
    """Save BA timings to JSON file."""
    try:
        os.makedirs(BASE_DIR / "data", exist_ok=True)
        with open(BA_TIMINGS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save BA timings: {e}")


def _load_ba_schedule_cache() -> dict:
    """Load cached BA schedule."""
    try:
        if os.path.exists(BA_SCHEDULE_PATH):
            with open(BA_SCHEDULE_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load BA schedule cache: {e}")
    return {}


def _save_ba_schedule_cache(data: dict):
    """Save BA schedule to cache file."""
    try:
        os.makedirs(BASE_DIR / "data", exist_ok=True)
        with open(BA_SCHEDULE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save BA schedule cache: {e}")


# ── BA Timings Data Builder ─────────────────────────────────────────
def _build_ba_leaderboard_data() -> Tuple[List[dict], int, dict]:
    """
    Build the leaderboard entries for Breaking Army.
    Returns (entries, total_players, session_info) where:
      - entries: list of dicts with pid, nickname, score (best_time), attempts, session_key
      - total_players: total unique players across both sessions
      - session_info: dict with current BA state info
    """
    schedule = _load_ba_schedule_cache()
    timings = _load_ba_timings()
    state = _get_ba_state(schedule, timings)
    week_start_ts = state.get("week_start_ts", 0)
    sessions = state.get("sessions", {})

    entries = []
    all_pids = set()

    # ── Per-session stale data cleanup ─────────────────────────────────
    # For each session, check if the stored start_ts no longer matches
    # the schedule's computed start_ts (indicating the session rolled over
    # to a new week). This mirrors the check in on_breaking_army_timing
    # and ensures old data is cleared even during refresh cycles.
    altered = False
    for session_key in ("1", "2"):
        stored_session = timings.get(f"session_{session_key}", {})
        stored_start = stored_session.get("start_ts", 0)
        session_info = sessions.get(session_key, {})
        expected_start = session_info.get("start_ts", 0)
        if stored_start and expected_start and stored_start != expected_start:
            logger.info(
                f"BA build: session {session_key} start_ts changed "
                f"({stored_start} -> {expected_start}), clearing old session data"
            )
            timings.pop(f"session_{session_key}", None)
            if week_start_ts:
                timings["week_start_ts"] = week_start_ts
            altered = True
    if altered:
        _save_ba_timings(timings)

    # For each session, gather player timings
    for session_key in ("1", "2"):
        session_timings = timings.get(f"session_{session_key}", {}).get("players", {})
        session_info = sessions.get(session_key, {})
        boss_name = session_info.get("boss_name", "Unknown")
        session_start_ts = session_info.get("start_ts", 0)
        for pid, data in list(session_timings.items()):
            # ── Per-player staleness check ──────────────────────────────
            # If a player's last recorded timing is before the session's
            # current start_ts, their data is from a previous occurrence
            # and should be ignored (treated as stale).
            last_ts = data.get("last_ts", 0)
            if session_start_ts and last_ts and last_ts < session_start_ts:
                continue
            all_pids.add(pid)
            entries.append({
                "pid": pid,
                "nickname": data.get("nickname", "Unknown"),
                "score": data.get("best_time", 0),  # score = best_time (lower is better)
                "attempts": data.get("attempts", 0),
                "session_key": session_key,
                "boss_name": boss_name,
            })

    # Sort: session 1 first (sorted by time ascending), then session 2
    entries.sort(key=lambda e: (0 if e["session_key"] == "1" else 1, e["score"]))

    return entries, len(all_pids), state


# ── Breaking Army Persistent View ────────────────────────────────────
class BreakingArmyView(LayoutView):
    """The auto-refreshing Breaking Army leaderboard with Components V2."""

    def __init__(self, cog: "LeaderboardCog", entries: list,
                 total_players: int, timestamp: int, session_state: dict):
        super().__init__(timeout=None)
        self.cog = cog
        self.entries = entries
        self.total_players = total_players
        self.timestamp = timestamp
        self.session_state = session_state
        self._build()

    def _build(self):
        inner: list = []
        color = LEADERBOARD_COLORS["breaking_army"]
        sessions = self.session_state.get("sessions", {})

        inner.append(TextDisplay(
            "# 💀 Breaking Army Leaderboard\n"
            "Fastest clear times for each session"
        ))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        for session_key in ("1", "2"):
            info = sessions.get(session_key, {})
            state = info.get("state", "unknown")
            boss_name = info.get("boss_name", "Unknown")
            weekday_name = info.get("weekday_name", "?")
            hour = info.get("hour", 0)
            minute = info.get("minute", 0)
            start_ts = info.get("start_ts", 0)
            end_ts = info.get("end_ts", 0)
            boss_id = info.get("boss_id", 0)

            # Header line
            header = f"**── Session {session_key} ──**\n🐺 **Boss:** {boss_name}"

            if state == "unknown":
                header += "\n*Schedule not available*"
            elif state == "upcoming":
                header += f"\n🕐 <t:{start_ts}:F>\n⏳ <t:{start_ts}:R>"
            elif state == "upcoming_soon":
                header += f"\n🕐 <t:{start_ts}:F>\n🔔 <t:{start_ts}:R> — Preparing..."
            elif state == "active":
                header += f"\n🕐 <t:{start_ts}:F>\n🟢 **ACTIVE** — <t:{end_ts}:R> remaining"
            elif state == "finalized":
                header += f"\n🕐 <t:{start_ts}:F>\n🔒 **Finalized** — Ended <t:{end_ts}:R>"

            inner.append(TextDisplay(header))
            inner.append(Separator(spacing=discord.SeparatorSpacing.small))

            # Player rankings for this session
            session_entries = [e for e in self.entries if e["session_key"] == session_key]
            lines = []
            for i, e in enumerate(session_entries[:10], 1):
                prefix = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                time_val = int(e["score"])
                mins = time_val // 60
                secs = time_val % 60
                if mins > 0:
                    time_str_display = f"{mins}m {secs}s"
                else:
                    time_str_display = f"{secs}s"
                attempts = e.get("attempts", 1)
                lines.append(
                    f"{prefix} **{e['nickname']}** — `{time_str_display}` "
                    f"(⚔️ {attempts} attempt{'s' if attempts != 1 else ''})"
                )

            if lines:
                inner.append(TextDisplay("\n".join(lines)))
            else:
                if state == "active":
                    inner.append(TextDisplay("*Waiting for timing results...*"))
                elif state in ("upcoming", "upcoming_soon"):
                    inner.append(TextDisplay("*No data yet — session hasn't started*"))
                else:
                    inner.append(TextDisplay("*No data recorded*"))

            inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Footer
        footer_parts = [f"👥 **{self.total_players}** players tracked"]
        if self.timestamp:
            footer_parts.append(f"⏱️ <t:{self.timestamp}:R>")
        inner.append(TextDisplay("  •  ".join(footer_parts)))

        # Button row
        row = ActionRow()
        btn = discord.ui.Button(
            label="🔍 Check My Rank",
            style=discord.ButtonStyle.primary,
            custom_id="ba_leaderboard_check_rank",
        )
        btn.callback = self._on_check_rank
        row.add_item(btn)
        inner.append(row)

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
        user_entries = [e for e in self.entries if e["pid"] == user_pid]
        if not user_entries:
            await interaction.followup.send(
                "❌ You don't have any Breaking Army records yet.\n"
                "Participate in BA and your times will appear here!",
                ephemeral=True
            )
            return
        embed = discord.Embed(
            title="💀 Your Breaking Army Rankings",
            color=LEADERBOARD_COLORS["breaking_army"]
        )
        for ue in user_entries:
            time_val = int(ue["score"])
            mins = time_val // 60
            secs = time_val % 60
            time_display = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            # Compute rank within this session's entries only
            session_entries = [e for e in self.entries if e["session_key"] == ue["session_key"]]
            rank = next((i for i, e in enumerate(session_entries, 1)
                         if e["pid"] == user_pid), len(session_entries))
            total_in_session = len(session_entries)
            embed.add_field(
                name=f"Session {ue['session_key']} — {ue.get('boss_name', '?')}",
                value=f"**Time:** `{time_display}`\n"
                      f"**Attempts:** {ue.get('attempts', 1)}\n"
                      f"**Rank:** #{rank} / {total_in_session}",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Persistent Components V2 LayoutView — the standard leaderboard message itself
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

        if self.lb_type == "breaking_army":
            # Use the dedicated BA view instead
            return

        inner.append(TextDisplay(f"# {emoji} {display_name} Leaderboard\nTop players who have bound their accounts"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        if self.lb_type == "elegance":
            milestones = [
                {
                    "name": "Matchless Elegance (Tier 3) (90k)",
                    "score": 90000,
                    "already_reached": False
                },
                {
                    "name": "Matchless Elegance (Tier 2) (70k)",
                    "score": 70000,
                    "already_reached": False
                },
                {
                    "name": "Matchless Elegance (Tier 1) (50k)",
                    "score": 50000,
                    "already_reached": False
                },
                {
                    "name": "Timeless Hero (30k)",
                    "score": 30000,
                    "already_reached": False
                },
                {
                    "name": "Embracer of Splendor (5k)",
                    "score": 5000,
                    "already_reached": False
                }
            ]

        lines = []
        for i, e in enumerate(self.entries[:15], 1):
            prefix = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            score_str = f"{e['score']:,}" if isinstance(e['score'], int) else str(e['score'])
            lines.append(f"{prefix} **{e['nickname']}** — `{score_str}`")
            if self.lb_type == "elegance":
                # Check for highest milestone reached
                if e["score"] >= 90000 and not milestones[0]["already_reached"]:
                    milestones[0]["already_reached"] = True
                    lines.append(f"**-----{milestones[0]['name']}-----**")
                elif e["score"] >= 70000 and not milestones[1]["already_reached"]:
                    milestones[1]["already_reached"] = True
                    lines.append(f"**-----{milestones[1]['name']}-----**")
                elif e["score"] >= 50000 and not milestones[2]["already_reached"]:
                    milestones[2]["already_reached"] = True
                    lines.append(f"**-----{milestones[2]['name']}-----**")
                elif e["score"] >= 30000 and not milestones[3]["already_reached"]:
                    milestones[3]["already_reached"] = True
                    lines.append(f"**-----{milestones[3]['name']}-----**")
                elif e["score"] >= 5000 and not milestones[4]["already_reached"]:
                    milestones[4]["already_reached"] = True
                    lines.append(f"**-----{milestones[4]['name']}-----**")
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
        # BA timing tracking
        self._last_ba_schedule_refresh = 0.0

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

    # ── Breaking Army: Listen for timing events ───────────────────────
    @commands.Cog.listener()
    async def on_breaking_army_timing(self, nickname: str, seconds: float, timestamp: float):
        """
        Listener for the 'breaking_army_timing' event dispatched by live_chat_cog.
        Records the timing result into the appropriate session.
        """
        # Determine which BA session is active at this timestamp
        schedule = _load_ba_schedule_cache()
        if not schedule:
            logger.warning("BA timing: no schedule available, refreshing...")
            schedule = _fetch_ba_schedule()
            if not schedule:
                logger.error("BA timing: cannot determine active session without schedule")
                return
            _save_ba_schedule_cache(schedule)

        # Check which session was active at the timestamp of the message
        timings = _load_ba_timings()

        gmt8_ts = timestamp + 8 * 3600
        gmt8_dt = datetime.datetime.fromtimestamp(gmt8_ts, tz=datetime.timezone.utc)

        active_sessions = []
        for session_key in ("1", "2"):
            entry = schedule.get(session_key)
            if not entry:
                continue
            this_start, this_end, next_start, next_end = _compute_session_ts(entry)
            # Check if this session's time window contains the timestamp
            # (handle cases where the BA ended recently, use <= end_ts with some grace)
            if this_start <= timestamp <= this_end + 120:  # 2 min grace period
                active_sessions.append((session_key, entry, this_start, this_end))
            elif next_start <= timestamp <= next_end + 120:
                active_sessions.append((session_key, entry, next_start, next_end))

        if not active_sessions:
            logger.debug(f"BA timing: no active session at timestamp {timestamp} for {nickname}")
            return

        # Use the first matching session
        session_key, entry, start_ts, end_ts = active_sessions[0]
        boss_id = entry.get("boss_id", 0)
        boss_name = BOSS_NAMES.get(int(boss_id), f"Boss #{boss_id}")

        # Build the week_start_ts from the schedule
        state = _get_ba_state(schedule)
        week_start_ts = state.get("week_start_ts", 0)
        if not timings.get("week_start_ts"):
            timings["week_start_ts"] = week_start_ts

        # ── Per-session new week detection ───────────────────────────────
        # Only clear THIS session's old data when its start time changes
        # (i.e. the session has rolled over to a new week).
        # Other sessions' old data stays visible until they themselves roll over.
        stored_session = timings.get(f"session_{session_key}", {})
        stored_start = stored_session.get("start_ts", 0)
        if stored_start and stored_start != int(start_ts):
            logger.info(
                f"BA timing: session {session_key} start_ts changed "
                f"({stored_start} -> {int(start_ts)}), "
                f"clearing old session data for {nickname}"
            )
            timings.pop(f"session_{session_key}", None)
            timings["week_start_ts"] = week_start_ts
            _save_ba_timings(timings)

        # Get or create session data
        session_data = timings.setdefault(f"session_{session_key}", {
            "boss_id": int(boss_id),
            "boss_name": boss_name,
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "players": {},
        })

        # Update boss info if it changed (new week)
        if session_data.get("boss_id") != int(boss_id):
            session_data["boss_id"] = int(boss_id)
            session_data["boss_name"] = boss_name
        session_data["start_ts"] = int(start_ts)
        session_data["end_ts"] = int(end_ts)

        players = session_data.setdefault("players", {})

        # Resolve PID from nickname — check existing players first
        pid = None
        for p, pdata in players.items():
            if pdata.get("nickname") == nickname:
                pid = p
                break


        if pid is None:
            # Try to find the pid from recent BA schedule members or just use a temp key
            # We'll attempt to look up by nickname from the bulk API
            try:
                from utility.wwm import find_people_by_nickname
                result = await asyncio.to_thread(find_people_by_nickname, nickname)
                if result and 'result' in result:
                    pid = result['result'].get('id')
            except Exception as e:
                logger.warning(f"BA timing: could not resolve PID for {nickname}: {e}")

        if pid is None:
            # Fallback: store by nickname as key (less ideal but functional)
            logger.warning(f"BA timing: no PID for {nickname}, storing by nickname")
            pid = f"__{nickname}__"

        # Update timing
        player_entry = players.setdefault(pid, {
            "nickname": nickname,
            "best_time": float('inf'),
            "attempts": 0,
            "last_ts": 0,
        })
        player_entry["nickname"] = nickname
        player_entry["attempts"] = player_entry.get("attempts", 0) + 1
        player_entry["last_ts"] = max(player_entry.get("last_ts", 0), int(timestamp))

        best = player_entry.get("best_time", float('inf'))
        if seconds < best:
            player_entry["best_time"] = seconds
            logger.info(f"🏆 BA {session_key} ({boss_name}): {nickname} new best {seconds}s "
                        f"(attempt {player_entry['attempts']})")

        _save_ba_timings(timings)

        # Trigger a refresh of any BA leaderboard instances
        for inst in self.instances:
            if inst.lb_type == "breaking_army":
                try:
                    await self._publish_one(inst)
                except Exception as e:
                    logger.error(f"BA timing: failed to refresh leaderboard: {e}")

    # ── BA Schedule Refresh ───────────────────────────────────────────
    def _refresh_ba_schedule_if_needed(self):
        """Fetch BA schedule from API if cache is stale (every 5 minutes)."""
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        if now - self._last_ba_schedule_refresh > 300:  # 5 min
            schedule = _fetch_ba_schedule()
            if schedule:
                _save_ba_schedule_cache(schedule)
            self._last_ba_schedule_refresh = now
            return schedule
        return None

    # ── data fetching ──────────────────────────────────────────────────
    async def _fetch_data(self, lb_type: str) -> Tuple[List[dict], int]:
        if lb_type == "breaking_army":
            # Refresh BA schedule periodically
            self._refresh_ba_schedule_if_needed()
            entries, total, state = _build_ba_leaderboard_data()
            # Store session_state for publishing
            self._ba_session_state = state
            return entries, total

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

    # ── Elegance top-3 role assignment ──────────────────────────────────
    async def _assign_elegance_roles(self, entries: list):
        """
        Assign #1, #2, #3 elegance roles to the top 3 players.
        Only runs if settings.LEADERBOARD_ROLES is defined (main branch only).
        Removes roles from players who dropped out of the top 3.
        """
        leaderboard_roles = getattr(settings, "LEADERBOARD_ROLES", None)
        if leaderboard_roles is None:
            return  # Not on main branch — no role config

        rank_role_keys = ["elegance_1", "elegance_2", "elegance_3"]
        role_ids = [leaderboard_roles[k] for k in rank_role_keys]

        guild = self.bot.get_guild(DISCORD_SERVER_ID)
        if not guild:
            logger.warning("Cannot assign elegance roles: guild not found")
            return

        # (Tracking sets populated below in the role-assignment loop,
        # which cascades to the next in-guild member when a top-3 player
        # has left the guild.)

        # Determine role objects
        roles = [guild.get_role(rid) for rid in role_ids]
        roles = [r for r in roles if r is not None]

        # Assign roles to top 3, cascading to the next in-guild member when
        # a top-3 player has left the guild. This way the role slot is
        # always filled by the next eligible bound-and-present member.
        new_top3_pids: list = []
        new_top3_user_ids: set = set()
        rank_for_pid: dict = {}
        next_rank = 1
        for entry in entries:
            if next_rank > 3:
                break
            pid = entry["pid"]
            # Resolve Discord user_id from DB
            async with aiosqlite.connect(DB_PATH) as conn:
                cur = await conn.execute(
                    "SELECT user_id FROM verified_members WHERE player_pid = ?",
                    (pid,)
                )
                row = await cur.fetchone()
            if not row:
                # No Discord binding for this pid — skip the pid entirely
                # (not a guild-leave case; just un-bound).
                continue
            user_id = row[0]
            member = guild.get_member(user_id)
            if not member:
                # User has left the guild — skip them and let the loop try
                # the next entry in the leaderboard for this rank slot.
                logger.info(
                    f"Elegance role: skipping pid {pid} (uid {user_id}) — not in guild"
                )
                continue
            rank = next_rank
            new_top3_pids.append(pid)
            rank_for_pid[pid] = rank
            new_top3_user_ids.add(user_id)
            role = roles[rank - 1]
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Elegance rank #{rank}")
                    logger.info(f"Assigned {role.name} to {member} (rank #{rank})")
                except Exception as e:
                    logger.error(f"Failed to assign elegance role to {member}: {e}")

            # Remove lower elegance roles if they have them (e.g. #1 shouldn't also have #2 or #3)
            for lower_rank in range(rank, 3):  # rank is 1-indexed, so lower_rank=rank..3
                lower_role = roles[lower_rank]
                if lower_role in member.roles:
                    try:
                        await member.remove_roles(lower_role, reason="Elegance rank upgraded")
                        logger.info(f"Removed lower elegance role {lower_role.name} from {member}")
                    except Exception as e:
                        logger.error(f"Failed to remove lower elegance role from {member}: {e}")
            next_rank += 1

        # Store current top 3 PIDs for delta tracking next cycle
        self._prev_elegance_top3 = list(new_top3_pids)

        # Remove roles from users who are no longer top 3 (but still have the role)
        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT user_id FROM verified_members WHERE player_pid IS NOT NULL"
            )
            all_verified = await cur.fetchall()

        for (uid,) in all_verified:
            member = guild.get_member(uid)
            if not member:
                continue
            # Check if they have any elegance role but are NOT in new top 3
            user_has_role = any(r in member.roles for r in roles)
            if user_has_role and uid not in new_top3_user_ids:
                for role in roles:
                    if role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Elegance rank lost (dropped out of top 3)")
                            logger.info(f"Removed elegance role {role.name} from {member} (uid={uid})")
                        except Exception as e:
                            logger.error(f"Failed to remove elegance role from {member}: {e}")

    # ── publish / refresh a single instance ────────────────────────────
    async def _publish_one(self, inst: _LeaderboardInstance):
        entries, total = await self._fetch_data(inst.lb_type)
        now_ts = int(discord.utils.utcnow().timestamp())

        # Assign elegance roles if applicable (main branch only)
        if inst.lb_type == "elegance":
            try:
                await self._assign_elegance_roles(entries)
            except Exception as e:
                logger.error(f"Failed to assign elegance roles: {e}")

        if inst.lb_type == "breaking_army":
            session_state = getattr(self, '_ba_session_state', {"sessions": {}})
            view = BreakingArmyView(self, entries, total, now_ts, session_state)
        else:
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
                # Wait a bit between refreshes to avoid hitting rate limits if there are many instances
                await asyncio.sleep(5)
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
                discord.SelectOption(label="Martial Mastery", description="Max XIUWEI_KUNGFU",
                                     value="martial_mastery", emoji="⚔️"),
                discord.SelectOption(label="Exploration Mastery", description="XIUWEI_EXPLORE",
                                     value="exploration_mastery", emoji="🗺️"),
                discord.SelectOption(label="Playtime", description="Total online time in hours",
                                     value="playtime", emoji="⌛"),
                discord.SelectOption(label="Breaking Army", description="BA fastest clear times",
                                     value="breaking_army", emoji="💀"),
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
register(BreakingArmyView, cog=None, entries=[],
         total_players=0, timestamp=0, session_state={"sessions": {}})


# ---------------------------------------------------------------------------
# Cog entry point
# ---------------------------------------------------------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))