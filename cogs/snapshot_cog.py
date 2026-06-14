"""
Snapshot Cog — Daily guild member snapshots at 5 AM GMT+8.

Captures comprehensive player data each day and provides daily/weekly
comparison reports showing deltas in playtime, liveness, mastery, etc.

Architecture:
  - daily_snapshot_task fires at 5:00 AM GMT+8 every day
  - Each snapshot stores JSON with guild summary + per-player stats
  - /snapshot commands generate comparison reports between any two dates
"""

import asyncio
import concurrent.futures
import datetime
import json
import math

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import (
    LayoutView, Container, TextDisplay, Separator, ActionRow, Button,
)

import settings
from settings import logger, BASE_DIR, CLUB_ID
from utility.wwm import get_full_guild_info, get_bulk_players_info, get_fashion_score

# ─── Timezone ────────────────────────────────────────────────────────
GMT8_TZ = datetime.timezone(datetime.timedelta(hours=8))
GMT8_OFFSET = 8 * 3600

# ─── Paths ───────────────────────────────────────────────────────────
SNAPSHOT_DB_PATH = BASE_DIR / "data" / "snapshot.db"

# ─── Colours ─────────────────────────────────────────────────────────
BLURPLE = 0x5865F2
GREEN = 0x2ECC71
ORANGE = 0xE67E22


# ─── Permission check (reuse wwm_cog pattern) ───────────────────────
def admin_or_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        try:
            from settings import STAFF_ROLES
            staff_role_ids = set(STAFF_ROLES.values())
        except (ImportError, AttributeError):
            raise app_commands.MissingPermissions(["administrator"])
        member_role_ids = {r.id for r in interaction.user.roles}
        if staff_role_ids & member_role_ids:
            return True
        raise app_commands.MissingPermissions(["administrator"])
    return app_commands.check(predicate)


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _gmt8_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def _snapshot_date_label(ts: int) -> str:
    gmt8 = datetime.datetime.fromtimestamp(ts + GMT8_OFFSET, tz=datetime.timezone.utc)
    return gmt8.strftime("%b %d, %Y")


def _today_gmt8_str() -> str:
    return _gmt8_now().strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════
# Data collection
# ══════════════════════════════════════════════════════════════════════

async def _collect_snapshot_data() -> dict | None:
    try:
        guild_data = await get_full_guild_info(CLUB_ID)
        if not guild_data or "result" not in guild_data:
            logger.error("Snapshot: failed to fetch guild data")
            return None

        result = guild_data["result"]
        base = result.get("base", {})
        members = result.get("members", {})
        play = result.get("play", {})
        member_list = members.get("members", {})

        guild_summary = {
            "guild_name": base.get("name", "Unknown"),
            "guild_level": base.get("level", 0),
            "funds": base.get("fund", 0),
            "fame": base.get("fame", 0),
            "week_fame": base.get("week_fame", 0),
            "gvg_points": play.get("pk_match_info", {}).get("battle_score", 0),
            "member_count": members.get("member_num", 0),
        }

        all_pids = list(member_list.keys())
        if not all_pids:
            logger.warning("Snapshot: no members in guild")
            return {"guild_summary": guild_summary, "players": []}

        bulk_data = await get_bulk_players_info(all_pids, fields=["base", "club", "attr"])
        players_result = {}
        if bulk_data and bulk_data.get("code") == 0:
            players_result = bulk_data.get("result", {})

        # Fetch fashion scores concurrently
        fashion_scores = {}

        async def _fetch_fashion(pid):
            try:
                sd = await get_fashion_score(pid)
                if sd and "result" in sd:
                    score = sd["result"]
                    if isinstance(score, dict):
                        score = score.get("score", 0)
                    return (pid, int(score) if score else 0)
            except Exception:
                pass
            return (pid, 0)

        for pid in all_pids:
            try:
                pid, score = await _fetch_fashion(pid)
                fashion_scores[pid] = score
            except Exception:
                pass

        players = []
        for pid, pdata in players_result.items():
            pbase = pdata.get("base", {})
            pclub = pdata.get("club", {})
            pattr = pdata.get("attr", {})
            players.append({
                "pid": pid,
                "nickname": pbase.get("nickname", "Unknown"),
                "number_id": str(pbase.get("number_id", "")),
                "level": pbase.get("level", 0),
                "online_time": pbase.get("online_time", 0),
                "liveness": pclub.get("liveness", 0),
                "total_liveness": pclub.get("total_liveness", 0),
                "contribution": pclub.get("contribution", 0),
                "martial_mastery": round(pbase.get("max_xiuwei_kungfu", 0), 1),
                "scholar_mastery": round(float(pattr.get("XIUWEI_TRADE3", 0)), 1),
                "healer_mastery": round(float(pattr.get("XIUWEI_TRADE4", 0)), 1),
                "explore_mastery": round(float(pattr.get("XIUWEI_EXPLORE", 0)), 1),
                "fashion_score": fashion_scores.get(pid, 0),
                "logout_time": pbase.get("logout_time", 0),
            })

        logger.info(f"Snapshot collected: {len(players)} players, guild {guild_summary['guild_name']}")
        return {"guild_summary": guild_summary, "players": players}
    except Exception as e:
        logger.error(f"Snapshot collection failed: {e}", exc_info=True)
        return None


# ══════════════════════════════════════════════════════════════════════
# Comparison engine
# ══════════════════════════════════════════════════════════════════════

def _compare_snapshots(old_data: dict, new_data: dict,
                       old_date: str, new_date: str) -> dict:
    """Compare two snapshots, respecting visibility and monotonic data rules.

    Monotonic metrics (online_time, mastery, fashion, level, total_liveness,
    contribution) only ever increase.  If the new value is ≤ the old value
    the change is either a data-artifact or the player turned off their
    profile search — we silently skip it.

    Fashion score specifically can become 0 when a player hides their
    profile.  We treat old=X → new=0 as "hidden" rather than "lost X points".

    Liveness resets weekly, so decreases are natural — we only report gains.
    """
    gs_old = old_data.get("guild_summary", {})
    gs_new = new_data.get("guild_summary", {})

    guild_delta = {}
    for key in ("funds", "fame", "week_fame", "gvg_points", "member_count", "guild_level"):
        ov = gs_old.get(key, 0)
        nv = gs_new.get(key, 0)
        guild_delta[key] = {"old": ov, "new": nv, "delta": nv - ov}

    old_players = {p["pid"]: p for p in old_data.get("players", [])}
    new_players = {p["pid"]: p for p in new_data.get("players", [])}

    playtime_gainers = []
    liveness_gainers = []
    martial_gainers = []
    scholar_gainers = []
    healer_gainers = []
    explore_gainers = []
    fashion_gainers = []
    level_gainers = []
    new_members = []
    left_members = []
    total_active = 0

    for pid, np in new_players.items():
        if np.get("liveness", 0) > 0:
            total_active += 1
        op = old_players.get(pid)
        if op is None:
            new_members.append((np["nickname"], np.get("number_id", "")))
            continue

        # ── Playtime (online_time, seconds — monotonically increasing) ──
        old_ot = op.get("online_time", 0)
        new_ot = np.get("online_time", 0)
        if new_ot > old_ot and old_ot > 0:
            pt_delta = (new_ot - old_ot) / 3600
            if pt_delta > 0.05:
                playtime_gainers.append((np["nickname"], round(pt_delta, 1)))

        # ── Liveness (weekly reset — only report gains) ─────────────────
        lv_delta = np.get("liveness", 0) - op.get("liveness", 0)
        if lv_delta > 0:
            liveness_gainers.append((np["nickname"], lv_delta))

        # ── Mastery (monotonically increasing — skip if new ≤ old) ─────
        for key, lst in [
            ("martial_mastery", martial_gainers),
            ("scholar_mastery", scholar_gainers),
            ("healer_mastery", healer_gainers),
            ("explore_mastery", explore_gainers),
        ]:
            old_v = op.get(key, 0)
            new_v = np.get(key, 0)
            if new_v > old_v and old_v > 0:
                d = new_v - old_v
                if d > 0.1:
                    lst.append((np["nickname"], round(d, 1)))

        # ── Fashion score (hidden = 0 — only track real gains) ──────────
        old_fs = op.get("fashion_score", 0)
        new_fs = np.get("fashion_score", 0)
        if new_fs > old_fs and old_fs > 0:
            fashion_gainers.append((np["nickname"], new_fs - old_fs))

        # ── Level (monotonically increasing) ────────────────────────────
        old_lv = op.get("level", 0)
        new_lv = np.get("level", 0)
        if new_lv > old_lv and old_lv > 0:
            level_gainers.append((np["nickname"], old_lv, new_lv))

    for pid, op in old_players.items():
        if pid not in new_players:
            left_members.append((op["nickname"], op.get("number_id", "")))

    for lst in (playtime_gainers, liveness_gainers, martial_gainers,
                scholar_gainers, healer_gainers, explore_gainers, fashion_gainers):
        lst.sort(key=lambda x: x[1], reverse=True)

    return {
        "date_range": (old_date, new_date),
        "guild_delta": guild_delta,
        "playtime_gainers": playtime_gainers,
        "liveness_gainers": liveness_gainers,
        "martial_gainers": martial_gainers,
        "scholar_gainers": scholar_gainers,
        "healer_gainers": healer_gainers,
        "explore_gainers": explore_gainers,
        "fashion_gainers": fashion_gainers,
        "level_gainers": level_gainers,
        "new_members": new_members,
        "left_members": left_members,
        "total_active": total_active,
        "total_members": len(new_players),
    }


# ══════════════════════════════════════════════════════════════════════
# Views (Components V2)
# ══════════════════════════════════════════════════════════════════════

class DailyReportView(LayoutView):
    """Daily comparison report with section tabs."""

    ITEMS_PER_PAGE = 10

    def __init__(self, report: dict):
        super().__init__(timeout=300)
        self.report = report
        self.section = "overview"
        self.page = 0
        self._rebuild()

    def _overview_items(self) -> list:
        rd = self.report
        old_d, new_d = rd["date_range"]
        gd = rd["guild_delta"]
        items = [
            TextDisplay(f"# 📊 Daily Report\n📅 **{old_d}** → **{new_d}**"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]
        lines = []
        for label, key, emoji in [
            ("Guild Level", "guild_level", "⭐"), ("Funds", "funds", "💰"),
            ("Total Fame", "fame", "📈"), ("Weekly Fame", "week_fame", "🔥"),
            ("GvG Points", "gvg_points", "⚔️"), ("Members", "member_count", "👥"),
        ]:
            g = gd.get(key, {})
            ov, nv, d = g.get("old", 0), g.get("new", 0), g.get("delta", 0)
            sign = "+" if d > 0 else ""
            if key == "members":
                lines.append(f"{emoji} **{label}:** {ov} → **{nv}** ({sign}{d})")
            else:
                lines.append(f"{emoji} **{label}:** {ov:,} → **{nv:,}** ({sign}{d:,})")
        lines.append(f"🟢 **Active Today:** {rd['total_active']}/{rd['total_members']}")
        lines.append("")
        if rd["new_members"]:
            lines.append(f"✅ **Joined:** {', '.join(n for n, _ in rd['new_members'][:5])}")
        if rd["left_members"]:
            lines.append(f"❌ **Left:** {', '.join(n for n, _ in rd['left_members'][:5])}")
        if not rd["new_members"] and not rd["left_members"]:
            lines.append("✅ No membership changes")
        items.append(TextDisplay("\n".join(lines)))
        return items

    def _list_section(self, title, emoji, data, fmt_func=None) -> list:
        if fmt_func is None:
            fmt_func = lambda item: f"**{item[0]}:** +{item[1]:,}"
        items = [TextDisplay(f"# {emoji} {title}"), Separator(spacing=discord.SeparatorSpacing.small)]
        total_pages = max(1, math.ceil(len(data) / self.ITEMS_PER_PAGE))
        self.page = min(self.page, total_pages - 1)
        start = self.page * self.ITEMS_PER_PAGE
        page_data = data[start:start + self.ITEMS_PER_PAGE]
        if page_data:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, item in enumerate(page_data):
                prefix = medals[i] if i < 3 else f"{start + i + 1}."
                lines.append(f"{prefix} {fmt_func(item)}")
            items.append(TextDisplay("\n".join(lines)))
        else:
            items.append(TextDisplay("*No data for this section*"))
        items.append(Separator(spacing=discord.SeparatorSpacing.small))
        items.append(TextDisplay(f"Page {self.page + 1}/{total_pages}  •  Total: {len(data)} players"))
        return items

    def _build_mastery_section(self) -> list:
        items = [TextDisplay("# 🎓 Mastery Gainers"), Separator(spacing=discord.SeparatorSpacing.small)]
        rd = self.report
        for label, key in [
            ("⚔️ Martial", "martial_gainers"), ("📚 Scholar", "scholar_gainers"),
            ("💚 Healer", "healer_gainers"), ("🗺️ Exploration", "explore_gainers"),
        ]:
            data = rd.get(key, [])
            if data:
                top5 = data[:5]
                lines = [f"**{label}**"]
                for i, (name, delta) in enumerate(top5):
                    prefix = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
                    lines.append(f"{prefix} {name}: +{delta}")
                if len(data) > 5:
                    lines.append(f"*... and {len(data) - 5} more*")
                items.append(TextDisplay("\n".join(lines)))
            else:
                items.append(TextDisplay(f"**{label}:** No changes"))
            items.append(Separator(spacing=discord.SeparatorSpacing.small))
        if rd.get("level_gainers"):
            lines = ["**⭐ Level Ups**"]
            for name, olv, nlv in rd["level_gainers"][:5]:
                lines.append(f"• {name}: {olv} → **{nlv}**")
            items.append(TextDisplay("\n".join(lines)))
        return items

    def _rebuild(self):
        self.clear_items()
        inner = []
        if self.section == "overview":
            inner = self._overview_items()
        elif self.section == "playtime":
            inner = self._list_section("Playtime Gainers", "⏰",
                                       self.report["playtime_gainers"],
                                       fmt_func=lambda i: f"**{i[0]}:** +{i[1]}h")
        elif self.section == "liveness":
            inner = self._list_section("Liveness Gainers", "🔥",
                                       self.report["liveness_gainers"],
                                       fmt_func=lambda i: f"**{i[0]}:** +{i[1]:,} pts")
        elif self.section == "mastery":
            inner = self._build_mastery_section()
        elif self.section == "fashion":
            inner = self._list_section("Elegance Gainers", "💃",
                                       self.report["fashion_gainers"],
                                       fmt_func=lambda i: f"**{i[0]}:** +{i[1]:,}")
        elif self.section == "levels":
            inner = self._list_section("Level Ups", "⭐",
                                       self.report["level_gainers"],
                                       fmt_func=lambda i: f"**{i[0]}:** {i[1]} → {i[2]}")

        tab_row = ActionRow()
        for sec, label in [("overview", "📋 Overview"), ("playtime", "⏰ Playtime"),
                           ("liveness", "🔥 Activity"), ("mastery", "🎓 Mastery"),
                           ("fashion", "💃 Elegance"), ("levels", "⭐ Levels")]:
            btn = Button(label=label,
                         style=discord.ButtonStyle.primary if self.section == sec else discord.ButtonStyle.secondary,
                         custom_id=f"snapshot_tab_{sec}", disabled=self.section == sec)
            btn.callback = self._make_tab_cb(sec)
            tab_row.add_item(btn)
        inner.append(tab_row)

        if self.section != "overview":
            nav_row = ActionRow()
            prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary,
                              custom_id="snap_prev", disabled=self.page <= 0)
            prev_btn.callback = self._on_prev
            nav_row.add_item(prev_btn)
            dl = self._current_data_len()
            tp = max(1, math.ceil(dl / self.ITEMS_PER_PAGE))
            next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary,
                              custom_id="snap_next", disabled=self.page >= tp - 1)
            next_btn.callback = self._on_next
            nav_row.add_item(next_btn)
            inner.append(nav_row)

        self.add_item(Container(*inner, accent_color=BLURPLE))

    def _current_data_len(self) -> int:
        key = f"{self.section}_gainers"
        return len(self.report.get(key, []))

    def _make_tab_cb(self, sec):
        async def cb(interaction):
            await interaction.response.defer()
            self.section = sec
            self.page = 0
            self._rebuild()
            await interaction.edit_original_response(view=self)
        return cb

    async def _on_prev(self, interaction):
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _on_next(self, interaction):
        await interaction.response.defer()
        dl = self._current_data_len()
        tp = max(1, math.ceil(dl / self.ITEMS_PER_PAGE))
        if self.page < tp - 1:
            self.page += 1
            self._rebuild()
        await interaction.edit_original_response(view=self)


class WeeklyReportView(LayoutView):
    """Weekly comparison (7-day delta) with section tabs."""

    ITEMS_PER_PAGE = 10

    def __init__(self, report: dict):
        super().__init__(timeout=300)
        self.report = report
        self.section = "overview"
        self.page = 0
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        inner = []
        rd = self.report
        old_d, new_d = rd["date_range"]
        gd = rd["guild_delta"]

        inner.append(TextDisplay(f"# 📊 Weekly Report\n📅 **{old_d}** → **{new_d}**"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        if self.section == "overview":
            lines = []
            for label, key, emoji in [
                ("Guild Level", "guild_level", "⭐"), ("Funds", "funds", "💰"),
                ("Total Fame", "fame", "📈"), ("Weekly Fame", "week_fame", "🔥"),
                ("GvG Points", "gvg_points", "⚔️"), ("Members", "member_count", "👥"),
            ]:
                g = gd.get(key, {})
                ov, nv, d = g.get("old", 0), g.get("new", 0), g.get("delta", 0)
                sign = "+" if d > 0 else ""
                if key == "members":
                    lines.append(f"{emoji} **{label}:** {ov} → **{nv}** ({sign}{d})")
                else:
                    lines.append(f"{emoji} **{label}:** {ov:,} → **{nv:,}** ({sign}{d:,})")
            lines.append(f"🟢 **Active This Week:** {rd['total_active']}/{rd['total_members']}")
            if rd["new_members"]:
                lines.append(f"✅ **Joined ({len(rd['new_members'])}):** " + ", ".join(n for n, _ in rd["new_members"][:5]))
            if rd["left_members"]:
                lines.append(f"❌ **Left ({len(rd['left_members'])}):** " + ", ".join(n for n, _ in rd["left_members"][:5]))
            for sl, data, fmt in [
                ("⏰ **Top Playtime**", rd["playtime_gainers"], lambda x: f"{x[0]}: +{x[1]}h"),
                ("🔥 **Top Activity**", rd["liveness_gainers"], lambda x: f"{x[0]}: +{x[1]:,} pts"),
                ("💃 **Top Elegance**", rd["fashion_gainers"], lambda x: f"{x[0]}: +{x[1]:,}"),
            ]:
                lines.append("")
                lines.append(sl)
                if data:
                    medals = ["🥇", "🥈", "🥉"]
                    for i, item in enumerate(data[:3]):
                        lines.append(f"{medals[i]} {fmt(item)}")
                else:
                    lines.append("*No changes*")
            inner.append(TextDisplay("\n".join(lines)))
        elif self.section == "playtime":
            inner.extend(self._list_items("⏰ Weekly Playtime Gainers", rd["playtime_gainers"],
                                          lambda i: f"**{i[0]}:** +{i[1]}h"))
        elif self.section == "liveness":
            inner.extend(self._list_items("🔥 Weekly Activity Gainers", rd["liveness_gainers"],
                                          lambda i: f"**{i[0]}:** +{i[1]:,} pts"))
        elif self.section == "mastery":
            inner.extend(self._mastery_items())
        elif self.section == "fashion":
            inner.extend(self._list_items("💃 Weekly Elegance Gainers", rd["fashion_gainers"],
                                          lambda i: f"**{i[0]}:** +{i[1]:,}"))

        tab_row = ActionRow()
        for sec, label in [("overview", "📋 Overview"), ("playtime", "⏰ Playtime"),
                           ("liveness", "🔥 Activity"), ("mastery", "🎓 Mastery"),
                           ("fashion", "💃 Elegance")]:
            btn = Button(label=label,
                         style=discord.ButtonStyle.primary if self.section == sec else discord.ButtonStyle.secondary,
                         custom_id=f"wreport_tab_{sec}", disabled=self.section == sec)
            btn.callback = self._make_tab_cb(sec)
            tab_row.add_item(btn)
        inner.append(tab_row)

        if self.section not in ("overview", "mastery"):
            nav_row = ActionRow()
            prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary,
                              custom_id="wrep_prev", disabled=self.page <= 0)
            prev_btn.callback = self._on_prev
            nav_row.add_item(prev_btn)
            dl = self._current_data_len()
            tp = max(1, math.ceil(dl / self.ITEMS_PER_PAGE))
            next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary,
                              custom_id="wrep_next", disabled=self.page >= tp - 1)
            next_btn.callback = self._on_next
            nav_row.add_item(next_btn)
            inner.append(nav_row)

        self.add_item(Container(*inner, accent_color=GREEN))

    def _list_items(self, title, data, fmt_func) -> list:
        items = [TextDisplay(f"# {title}"), Separator(spacing=discord.SeparatorSpacing.small)]
        total_pages = max(1, math.ceil(len(data) / self.ITEMS_PER_PAGE))
        self.page = min(self.page, total_pages - 1)
        start = self.page * self.ITEMS_PER_PAGE
        page_data = data[start:start + self.ITEMS_PER_PAGE]
        if page_data:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, item in enumerate(page_data):
                prefix = medals[i] if i < 3 else f"{start + i + 1}."
                lines.append(f"{prefix} {fmt_func(item)}")
            items.append(TextDisplay("\n".join(lines)))
        else:
            items.append(TextDisplay("*No data for this section*"))
        items.append(Separator(spacing=discord.SeparatorSpacing.small))
        items.append(TextDisplay(f"Page {self.page + 1}/{total_pages}  •  Total: {len(data)} players"))
        return items

    def _mastery_items(self) -> list:
        rd = self.report
        items = [TextDisplay("# 🎓 Weekly Mastery Gainers"), Separator(spacing=discord.SeparatorSpacing.small)]
        for label, key in [("⚔️ Martial", "martial_gainers"), ("📚 Scholar", "scholar_gainers"),
                           ("💚 Healer", "healer_gainers"), ("🗺️ Exploration", "explore_gainers")]:
            data = rd.get(key, [])
            if data:
                top5 = data[:5]
                lines = [f"**{label}**"]
                for i, (name, delta) in enumerate(top5):
                    prefix = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
                    lines.append(f"{prefix} {name}: +{delta}")
                if len(data) > 5:
                    lines.append(f"*... and {len(data) - 5} more*")
                items.append(TextDisplay("\n".join(lines)))
            else:
                items.append(TextDisplay(f"**{label}:** No changes"))
            items.append(Separator(spacing=discord.SeparatorSpacing.small))
        return items

    def _current_data_len(self) -> int:
        return len(self.report.get(f"{self.section}_gainers", []))

    def _make_tab_cb(self, sec):
        async def cb(interaction):
            await interaction.response.defer()
            self.section = sec
            self.page = 0
            self._rebuild()
            await interaction.edit_original_response(view=self)
        return cb

    async def _on_prev(self, interaction):
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _on_next(self, interaction):
        await interaction.response.defer()
        dl = self._current_data_len()
        tp = max(1, math.ceil(dl / self.ITEMS_PER_PAGE))
        if self.page < tp - 1:
            self.page += 1
            self._rebuild()
        await interaction.edit_original_response(view=self)


class PlayerProgressView(LayoutView):
    """Single player's progression over time."""

    ITEMS_PER_PAGE = 7

    def __init__(self, nickname: str, history: list):
        super().__init__(timeout=300)
        self.nickname = nickname
        self.history = history
        self.page = max(0, len(history) - self.ITEMS_PER_PAGE)
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        inner = []
        inner.append(TextDisplay(f"# 📈 {self.nickname}'s Progression\n📅 {len(self.history)} snapshots recorded"))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        total_pages = max(1, math.ceil(len(self.history) / self.ITEMS_PER_PAGE))
        self.page = min(self.page, total_pages - 1)
        start = self.page * self.ITEMS_PER_PAGE
        page_data = self.history[start:start + self.ITEMS_PER_PAGE]

        for date_label, snap in reversed(page_data):
            lines = [
                f"**{date_label}**",
                f"⭐ Lv.{snap.get('level', '?')}  ⏰ {round(snap.get('online_time', 0) / 3600, 1)}h total",
                f"🔥 Liveness: {snap.get('liveness', 0):,}  💰 Contribution: {snap.get('contribution', 0):,}",
                f"⚔️ Martial: {snap.get('martial_mastery', 0)}  📚 Scholar: {snap.get('scholar_mastery', 0)}",
                f"💚 Healer: {snap.get('healer_mastery', 0)}  🗺️ Explore: {snap.get('explore_mastery', 0)}",
                f"💃 Elegance: {snap.get('fashion_score', 0):,}",
            ]
            inner.append(TextDisplay("\n".join(lines)))
            inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        if total_pages > 1:
            inner.append(TextDisplay(f"Page {self.page + 1}/{total_pages}"))
            nav_row = ActionRow()
            prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary,
                              custom_id="pprogress_prev", disabled=self.page <= 0)
            prev_btn.callback = self._on_prev
            nav_row.add_item(prev_btn)
            next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary,
                              custom_id="pprogress_next", disabled=self.page >= total_pages - 1)
            next_btn.callback = self._on_next
            nav_row.add_item(next_btn)
            inner.append(nav_row)

        self.add_item(Container(*inner, accent_color=ORANGE))

    async def _on_prev(self, interaction):
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _on_next(self, interaction):
        await interaction.response.defer()
        tp = max(1, math.ceil(len(self.history) / self.ITEMS_PER_PAGE))
        if self.page < tp - 1:
            self.page += 1
            self._rebuild()
        await interaction.edit_original_response(view=self)


# ══════════════════════════════════════════════════════════════════════
# Main Cog
# ══════════════════════════════════════════════════════════════════════

class SnapshotCog(commands.Cog):
    """Daily guild snapshot and comparison reports."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.report_channel_id = None

    snapshot_group = app_commands.Group(
        name="snapshot",
        description="Daily snapshot and comparison reports",
    )

    # ── Database ──────────────────────────────────────────────────

    async def _init_db(self):
        (BASE_DIR / "data").mkdir(exist_ok=True)
        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    snapshot_date TEXT PRIMARY KEY,
                    snapshot_ts   INTEGER NOT NULL,
                    guild_summary_json TEXT NOT NULL,
                    player_data_json   TEXT NOT NULL
                )
            """)
            await db.commit()

        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            cursor = await db.execute("SELECT value FROM snapshot_config WHERE key = 'report_channel_id'")
            row = await cursor.fetchone()
            if row:
                self.report_channel_id = int(row[0])

    async def _save_config(self):
        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            await db.execute(
                "REPLACE INTO snapshot_config (key, value) VALUES ('report_channel_id', ?)",
                (str(self.report_channel_id) if self.report_channel_id else None,),
            )
            await db.commit()

    # ── Lifecycle ─────────────────────────────────────────────────

    async def cog_load(self):
        await self._init_db()
        self.daily_snapshot_task.start()

    async def cog_unload(self):
        if self.daily_snapshot_task.is_running():
            self.daily_snapshot_task.cancel()

    # ── Snapshot task (5 AM GMT+8) ────────────────────────────────

    @tasks.loop(time=datetime.time(hour=5, minute=0, tzinfo=GMT8_TZ))
    async def daily_snapshot_task(self):
        try:
            today = _today_gmt8_str()
            logger.info(f"Daily snapshot starting for {today}")

            data = await _collect_snapshot_data()
            if data is None:
                logger.error("Daily snapshot: data collection returned None")
                return

            ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO daily_snapshots (snapshot_date, snapshot_ts, guild_summary_json, player_data_json) VALUES (?, ?, ?, ?)",
                    (today, ts, json.dumps(data["guild_summary"], ensure_ascii=False),
                     json.dumps(data["players"], ensure_ascii=False)),
                )
                # Cleanup old snapshots (>90 days)
                cutoff = today[:4]  # rough — better to compute proper date
                await db.execute(
                    "DELETE FROM daily_snapshots WHERE snapshot_date < date(?, '-90 days')",
                    (today,),
                )
                await db.commit()

            logger.info(f"Daily snapshot saved for {today}: {len(data['players'])} players")

            # Auto-post summary if channel configured
            if self.report_channel_id:
                await self._post_auto_summary(data, today)

        except Exception as e:
            logger.error(f"Daily snapshot task failed: {e}", exc_info=True)

    @daily_snapshot_task.before_loop
    async def before_daily_snapshot(self):
        await self.bot.wait_until_ready()

    # ── Auto-post summary ─────────────────────────────────────────

    async def _post_auto_summary(self, data: dict, today: str):
        """Post a brief summary after a snapshot is taken."""
        try:
            channel = self.bot.get_channel(self.report_channel_id)
            if not channel:
                return

            gs = data["guild_summary"]
            players = data["players"]
            active = sum(1 for p in players if p.get("liveness", 0) > 0)

            # Find yesterday's snapshot for deltas
            async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT snapshot_date, guild_summary_json, player_data_json FROM daily_snapshots WHERE snapshot_date < ? ORDER BY snapshot_date DESC LIMIT 1",
                    (today,),
                )
                prev = await cursor.fetchone()

            lines = [
                f"# 📊 Daily Snapshot — {today}",
                f"👥 **{len(players)}** members | 🟢 **{active}** active",
                f"💰 Funds: **{gs['funds']:,}** | 📈 Fame: **{gs['fame']:,}**",
                f"🔥 Week Fame: **{gs['week_fame']:,}** | ⚔️ GvG: **{gs['gvg_points']:,}**",
            ]

            if prev:
                prev_gs = json.loads(prev[1])
                fd = gs["funds"] - prev_gs.get("funds", 0)
                fmd = gs["fame"] - prev_gs.get("fame", 0)
                wfd = gs["week_fame"] - prev_gs.get("week_fame", 0)
                if fd != 0 or fmd != 0 or wfd != 0:
                    lines.append("")
                    lines.append("**Changes from yesterday:**")
                    if fd != 0:
                        sign = "+" if fd > 0 else ""
                        lines.append(f"💰 Funds: {sign}{fd:,}")
                    if fmd != 0:
                        sign = "+" if fmd > 0 else ""
                        lines.append(f"📈 Fame: {sign}{fmd:,}")
                    if wfd != 0:
                        sign = "+" if wfd > 0 else ""
                        lines.append(f"🔥 Week Fame: {sign}{wfd:,}")

            view = LayoutView(timeout=None)
            container = Container(
                TextDisplay("\n".join(lines)),
                accent_color=BLURPLE,
            )
            view.add_item(container)
            await channel.send(view=view)
        except Exception as e:
            logger.error(f"Failed to post auto snapshot summary: {e}")

    # ── Helper: get snapshot by date ──────────────────────────────

    async def _get_snapshot(self, date_str: str) -> dict | None:
        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            cursor = await db.execute(
                "SELECT guild_summary_json, player_data_json FROM daily_snapshots WHERE snapshot_date = ?",
                (date_str,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "guild_summary": json.loads(row[0]),
            "players": json.loads(row[1]),
        }

    async def _get_nearest_snapshot(self, date_str: str) -> tuple | None:
        """Get the snapshot on or before the given date. Returns (date_label, data)."""
        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            cursor = await db.execute(
                "SELECT snapshot_date, guild_summary_json, player_data_json FROM daily_snapshots WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1",
                (date_str,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return (row[0], {
            "guild_summary": json.loads(row[1]),
            "players": json.loads(row[2]),
        })

    async def _list_snapshot_dates(self) -> list[str]:
        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            cursor = await db.execute(
                "SELECT snapshot_date FROM daily_snapshots ORDER BY snapshot_date ASC"
            )
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    # ── Slash commands ────────────────────────────────────────────

    @snapshot_group.command(name="daily", description="Generate a daily comparison report (yesterday vs today)")
    @admin_or_staff()
    async def snapshot_daily(self, interaction: discord.Interaction):
        await interaction.response.defer()

        today = _today_gmt8_str()
        dates = await self._list_snapshot_dates()
        if not dates:
            await interaction.followup.send("❌ No snapshots recorded yet. Use `/snapshot force` to take one now.")
            return

        # Find today's and the previous snapshot
        new_snap_data = None
        new_date = None
        old_snap_data = None
        old_date = None

        # Get the latest snapshot
        if today in dates:
            new_date = today
            new_snap_data = await self._get_snapshot(today)
        else:
            # Latest available
            new_date = dates[-1]
            new_snap_data = await self._get_snapshot(new_date)

        if not new_snap_data:
            await interaction.followup.send("❌ Failed to load latest snapshot.")
            return

        # Get the one before it
        if new_date in dates:
            idx = dates.index(new_date)
            if idx > 0:
                old_date = dates[idx - 1]
                old_snap_data = await self._get_snapshot(old_date)

        if not old_snap_data:
            await interaction.followup.send(
                f"❌ Need at least 2 snapshots for a comparison. Currently have {len(dates)} snapshot(s).\n"
                f"Latest: **{new_date}**"
            )
            return

        report = _compare_snapshots(old_snap_data, new_snap_data, old_date, new_date)
        view = DailyReportView(report)
        await interaction.followup.send(view=view)

    @snapshot_group.command(name="weekly", description="Generate a weekly comparison report (7 days ago vs today)")
    @admin_or_staff()
    async def snapshot_weekly(self, interaction: discord.Interaction):
        await interaction.response.defer()

        dates = await self._list_snapshot_dates()
        if len(dates) < 2:
            await interaction.followup.send(f"❌ Need at least 2 snapshots for a comparison. Currently have {len(dates)}.")
            return

        today = _today_gmt8_str()
        # Compute 7 days ago
        gmt8_now = _gmt8_now()
        week_ago = (gmt8_now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

        new_result = await self._get_nearest_snapshot(today)
        old_result = await self._get_nearest_snapshot(week_ago)

        if not new_result or not old_result:
            await interaction.followup.send("❌ Could not find snapshots for the requested range.")
            return

        new_date, new_data = new_result
        old_date, old_data = old_result

        if old_date == new_date:
            await interaction.followup.send(
                f"❌ Only one snapshot available at **{old_date}**. Need snapshots at least 1 day apart for a weekly report."
            )
            return

        report = _compare_snapshots(old_data, new_data, old_date, new_date)
        view = WeeklyReportView(report)
        await interaction.followup.send(view=view)

    @snapshot_group.command(name="player", description="View a specific player's progression over time")
    @admin_or_staff()
    @app_commands.describe(nickname="The player's in-game nickname")
    async def snapshot_player(self, interaction: discord.Interaction, nickname: str):
        await interaction.response.defer()

        dates = await self._list_snapshot_dates()
        if not dates:
            await interaction.followup.send("❌ No snapshots recorded yet.")
            return

        history = []
        for date_str in dates:
            snap = await self._get_snapshot(date_str)
            if not snap:
                continue
            for p in snap.get("players", []):
                if p.get("nickname", "").lower() == nickname.lower():
                    history.append((date_str, p))
                    break

        if not history:
            await interaction.followup.send(f"❌ No data found for player **{nickname}** in any snapshot.")
            return

        view = PlayerProgressView(history[0][1].get("nickname", nickname), history)
        await interaction.followup.send(view=view)

    @snapshot_group.command(name="force", description="Take a snapshot immediately (admin only)")
    @admin_or_staff()
    async def snapshot_force(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        today = _today_gmt8_str()
        data = await _collect_snapshot_data()
        if data is None:
            await interaction.followup.send("❌ Snapshot data collection failed.")
            return

        ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        async with aiosqlite.connect(SNAPSHOT_DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO daily_snapshots (snapshot_date, snapshot_ts, guild_summary_json, player_data_json) VALUES (?, ?, ?, ?)",
                (today, ts, json.dumps(data["guild_summary"], ensure_ascii=False),
                 json.dumps(data["players"], ensure_ascii=False)),
            )
            await db.commit()

        await interaction.followup.send(
            f"✅ Snapshot taken for **{today}**: {len(data['players'])} players captured.",
            ephemeral=True,
        )

    @snapshot_group.command(name="status", description="View snapshot system status")
    @admin_or_staff()
    async def snapshot_status(self, interaction: discord.Interaction):
        dates = await self._list_snapshot_dates()
        total = len(dates)

        channel = self.bot.get_channel(self.report_channel_id) if self.report_channel_id else None

        embed = discord.Embed(title="📸 Snapshot System Status", color=BLURPLE)
        embed.add_field(name="Total Snapshots", value=f"`{total}`", inline=True)
        embed.add_field(name="Task Running", value="✅ Yes" if self.daily_snapshot_task.is_running() else "❌ No", inline=True)
        embed.add_field(name="Report Channel", value=channel.mention if channel else "Not set", inline=True)
        if dates:
            embed.add_field(name="Oldest", value=f"`{dates[0]}`", inline=True)
            embed.add_field(name="Latest", value=f"`{dates[-1]}`", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @snapshot_group.command(name="set-channel", description="Set the channel for auto-posting daily summaries")
    @admin_or_staff()
    async def snapshot_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.report_channel_id = channel.id
        await self._save_config()
        await interaction.response.send_message(
            f"✅ Snapshot report channel set to {channel.mention}. "
            f"Daily summaries will be posted here after each snapshot.",
            ephemeral=True,
        )

    @snapshot_group.command(name="list", description="List all available snapshot dates")
    @admin_or_staff()
    async def snapshot_list(self, interaction: discord.Interaction):
        dates = await self._list_snapshot_dates()
        if not dates:
            await interaction.response.send_message("❌ No snapshots recorded yet.", ephemeral=True)
            return

        # Show last 30 dates
        recent = dates[-30:]
        lines = [f"`{d}`" for d in reversed(recent)]
        embed = discord.Embed(
            title=f"📸 Snapshot Dates ({len(dates)} total)",
            description="\n".join(lines),
            color=BLURPLE,
        )
        if len(dates) > 30:
            embed.set_footer(text=f"Showing latest 30 of {len(dates)} snapshots")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Persistent view registration ─────────────────────────────────────
# No persistent views needed for this cog (all views are ephemeral per-command)


async def setup(bot: commands.Bot):
    await bot.add_cog(SnapshotCog(bot))