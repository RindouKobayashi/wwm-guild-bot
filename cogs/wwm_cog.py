import discord
import datetime
import os
import tempfile
import time
import asyncio
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow, Thumbnail, Section, MediaGallery, Button, Select
import logging
import aiosqlite
import json
from collections import defaultdict
from typing import Optional
from deepdiff import DeepDiff
import aiohttp

import settings
from utility.wwm import get_player_info, get_club_hostnums, get_full_guild_info, get_fashion_plan, get_fashion_score, get_club_by_name, get_bulk_players_info, get_bulk_players_info_multi_hostnum, get_club_brief_info_batch, find_people_by_nickname, fetch_player_data_by_pid, get_custom_guild_info, get_topics_likes, get_club_by_number_id, get_homeland_info, get_like_history, get_player_combat_plan, get_rank_list
from settings import WWM_UID, WWM_TOKEN, WWM_API_URL, logger, CLUB_ID, BASE_DIR
from utility.api_constants import SCHOOL_NAMES, SCHOOL_RANKING, SCHOOL_EMOTES, get_kongfu_ids_from_player, classify_kongfu_role, VOTE_COUNTS
from utility.wwm import get_sect_election_ranking
from utility.affix_mapper import map_data, init_db


def admin_or_staff():
    """Check if the user is an administrator OR has any of the staff roles defined in settings.
    
    On the dev branch (where STAFF_ROLES is not defined), only administrators pass the check.
    On the main branch, both administrators and staff role holders pass.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        # On the dev branch, STAFF_ROLES is not defined — only admins are allowed.
        # On the main branch, check if the user has any staff role.
        # Import inside the function to avoid ImportError on dev branch at module load time.
        try:
            from settings import STAFF_ROLES
            staff_role_ids = set(STAFF_ROLES.values())
        except (ImportError, AttributeError):
            # Dev branch — STAFF_ROLES not defined, admin-only
            raise app_commands.MissingPermissions(["administrator"])
        member_role_ids = {r.id for r in interaction.user.roles}
        if staff_role_ids & member_role_ids:
            return True
        raise app_commands.MissingPermissions(["administrator"])
    return app_commands.check(predicate)

DB_PATH = BASE_DIR / "data" / "guild_verification.db"
SCHEDULE_DB_PATH = BASE_DIR / "data" / "schedule.db"
BIRTHDAY_ROLE_ID = 1469960226294730753

BLURPLE = 0x5865F2
ORANGE = 0xE67E22

GMT8_TZ = datetime.timezone(datetime.timedelta(hours=8))

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def _ordinal(n: int) -> str:
    """Return ordinal string for a number: 1 -> '1st', 2 -> '2nd', 3 -> '3rd', etc."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

def _format_birthday(month: int, day: int) -> str:
    """Format month/day as a human-readable string, e.g. '3rd Feb'."""
    return f"{_ordinal(day)} {MONTH_NAMES[month - 1]}" if 1 <= month <= 12 else f"{month}/{day}"

class OnlinePlayersResultView(LayoutView):
    """Components V2 LayoutView for displaying online players result."""
    def __init__(self, online_players: list):
        super().__init__(timeout=120)
        
        # Build compact player lines — rank, nickname, level, and role only
        player_lines = []
        for p in online_players:
            rank_str = f"[{p['rank_name']}] " if p['rank_name'] else ""
            lv_str = f"Lv.{p['level']}"
            role_str = f" | {p['role']}" if p['role'] else ""
            line = f"{rank_str}**{p['nickname']}** ({lv_str}){role_str}"
            player_lines.append(line)

        players_text = "\n".join(player_lines)
        
        inner_items = []
        inner_items.append(TextDisplay(f"# 🟢 Online Players ({len(online_players)})\n\n{players_text}"))
        
        container = Container(*inner_items, accent_color=0x2ECC71)
        self.add_item(container)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class GuildStatusBoard(LayoutView):
    """Components V2 LayoutView for the guild live status board."""
    
    def __init__(self, cog, guild_name: str, guild_level: int, member_count: int,
                 apprentice_count: int, funds: int, total_fame: int, week_fame: int,
                 gvg_points: int, online_count: int, weekly_leaderboard: list,
                 pending_apps: int, now_ts: int, next_update_ts: int,
                 birthdays_this_week: list = None,
                 press_count: int = 0,
                 league_info: dict = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_name = guild_name
        self.online_count = online_count
        self.member_count = member_count
        self.pending_apps = pending_apps
        self.birthdays_this_week = birthdays_this_week or []
        self.press_count = press_count
        self.league_info = league_info or {}

        # ── Build section texts ──
        # Identity section
        identity_text = (
            f"📛 **Name:** __**{guild_name}**__\n"
            f"⭐ **Level:** __**{guild_level}**__  👥 **Members:** __**{member_count}/100**__\n"
            f"🎓 **Apprentices:** __**{apprentice_count}**__"
        )

        # Leadership section (currently not gathered — skipped)
        leadership_text = None
        # Finances + League section
        league_score = self.league_info.get('small_score', 0)
        league_rank = self.league_info.get('rank', 0)
        league_wins = self.league_info.get('win_count', 0)

        finance_parts = [
            f"💰 **Funds:** __**{funds:,}**__",
            f"📈 **Fame:** __**{total_fame:,}**__",
            f"🔥 **Weekly:** __**{week_fame:,}**__",
        ]
        if gvg_points:
            finance_parts.append(f"🏆 **Ranked:** __**{gvg_points:,}**__")
        if league_score or league_rank or league_wins:
            league_parts = []
            if league_score:
                league_parts.append(f"Score: __**{league_score:,}**__")
            if league_rank:
                league_parts.append(f"Rank: __**#{league_rank:,}**__")
            if league_wins:
                league_parts.append(f"Wins: __**{league_wins:,}**__")
            finance_parts.append(f"⚔️ **League:** {' | '.join(league_parts)}")

        finances_text = "\n".join(finance_parts)

        # Online status section
        status_text = (
            f"🟢 **Online:** __**{online_count}/{member_count}**__\n"
            f"🖱️ **Check Button Presses:** __**{press_count}**__"
        )

        # Weekly Activity section
        leaderboard_lines = []
        for rank, (name, points) in enumerate(weekly_leaderboard[:10], 1):
            if rank == 1:
                prefix = "🥇"
            elif rank == 2:
                prefix = "🥈"
            elif rank == 3:
                prefix = "🥉"
            else:
                prefix = f"{rank}."
            leaderboard_lines.append(f"{prefix} **{name}:** __**{points:,}**__")

        activity_text = "\n".join(leaderboard_lines) if leaderboard_lines else "*No activity this week*"

        # Birthdays this week section
        birthdays_text = None
        if self.birthdays_this_week:
            bday_lines = []
            for entry in self.birthdays_this_week:
                if len(entry) == 5:
                    nickname, month, day, _, _ = entry
                else:
                    nickname, month, day = entry[:3]
                formatted = _format_birthday(month, day)
                logger.debug(f"BIRTHDAY_FORMATTED: nickname={nickname} month={month} day={day} formatted={formatted}")
                bday_lines.append(f"🎂 **{nickname}** — {formatted}")
            birthdays_text = "\n".join(bday_lines)

        # Footer text
        footer_lines = []
        if pending_apps > 0:
            footer_lines.append(f"📋 **Pending Applications:** __**{pending_apps}**__")
        footer_lines.append(f"⏱️ <t:{now_ts}:R>  •  🔄 <t:{next_update_ts}:R>")
        footer_text = "  ".join(footer_lines)

        # ── Single master Container with all inner components ──
        inner_items = []

        # Header + Identity
        inner_items.append(TextDisplay(f"# 🏰 Guild Live Status\n\n{identity_text}"))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Leadership (if available)
        if leadership_text:
            inner_items.append(TextDisplay(leadership_text))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Finances
        inner_items.append(TextDisplay(finances_text))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Status (Online)
        inner_items.append(TextDisplay(status_text))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Birthdays This Week
        if birthdays_text:
            inner_items.append(TextDisplay(f"🎂 **Birthdays This Week**\n\n{birthdays_text}"))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Weekly Activity
        inner_items.append(TextDisplay(f"🔥 **Weekly Activity — Top 10**\n\n{activity_text}"))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Footer (time info)
        inner_items.append(TextDisplay(footer_text))

        # Button action row
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        button_row = ActionRow()
        button = discord.ui.Button(
            label="Check Online Players",
            style=discord.ButtonStyle.green,
            emoji="🟢",
            custom_id="online_players_button",
        )
        button.callback = self._handle_check_online
        button_row.add_item(button)
        inner_items.append(button_row)

        # Wrap everything in a single Container
        master_container = Container(*inner_items, accent_color=BLURPLE)
        self.add_item(master_container)

    async def _handle_check_online(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        GUILD_MEMBER_ROLE_ID = settings.GUILD_MEMBER_ROLE_ID
        member_role = discord.utils.get(interaction.user.roles, id=GUILD_MEMBER_ROLE_ID)
        if not member_role:
            await interaction.followup.send("❌ You are not guild member", ephemeral=True)
            return

        # Increment press counters
        self.press_count += 1
        self.cog.online_players_button_presses += 1
        logger.debug(f"Online players button pressed by {interaction.user} (total: {self.cog.online_players_button_presses})")
        # Persist the updated count to the database
        await self.cog._save_config()
        # Update the main monitor message to reflect the new count
        await self._update_monitor_press_count()

        loading_msg = await interaction.followup.send("🔄 Getting player list...", ephemeral=True, wait=True)

        try:
            if not self.cog.last_guild_state:
                await loading_msg.edit(content="❌ Guild data not initialized, please try again shortly")
                return

            result = self.cog.last_guild_state.get('result', {})
            members = result.get('members', {})
            member_list = members.get('members', {})

            all_pids = list(member_list.keys())

            # Extract rank info (custom posts) directly from cached guild state — no extra API call
            ranks_data = {}
            try:
                cached_members = self.cog.last_guild_state.get('result', {}).get('members', {})
                ranks_data = cached_members.get('custom_posts', {})
            except Exception as cache_err:
                logger.debug(f"Could not extract cached ranks: {cache_err}")

            # Custom rank name mapping (from live_chat_cog.py)
            custom_rank_names = {
                1: "Guild Leader",
                2: "Vice Leader",
                5: "Command",
                7: "Half Time Performer",
            }

            def get_player_rank_name(pid: str) -> str:
                """Get the highest rank name for a player PID."""
                player_ranks = []
                for rank_id_str, rank_info in ranks_data.items():
                    if pid in rank_info.get('pids', []):
                        player_ranks.append((int(rank_id_str), rank_info.get('name', 'Unknown')))
                if player_ranks:
                    player_ranks.sort(key=lambda x: x[0])
                    rid, rname = player_ranks[0]
                    return custom_rank_names.get(rid, rname)
                return None

            # Extract league info from cached guild state — no extra API call
            cached_play = self.cog.last_guild_state.get('result', {}).get('play', {})
            ranked_match_score = cached_play.get('pk_match_info', {}).get('battle_score', 0)
            league_info = cached_play.get('league_info', {}) or {}

            # Fetch player data with base + kongfu fields
            bulk_data = await get_bulk_players_info(all_pids, fields=["base", "kongfu"])

            online_players = []
            if bulk_data and bulk_data.get('code') == 0:
                players = bulk_data.get('result', {})
                for pid, player_data in players.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        nickname = player_base.get('nickname', 'Unknown')
                        level = player_base.get('level', 0)
                        # Rank
                        rank_name = get_player_rank_name(pid)
                        # Kungfu role classification
                        weapon_ids = get_kongfu_ids_from_player(player_data)
                        role = classify_kongfu_role(weapon_ids) if weapon_ids else ""
                        online_players.append({
                            'pid': pid,
                            'nickname': nickname,
                            'level': level,
                            'rank_name': rank_name,
                            'role': role,
                        })

            if online_players:
                # Sort: ranked players first (by rank ID), then by name
                def sort_key(p):
                    rank_priority = {"Guild Leader": 0, "Vice Leader": 1, "Command": 2, "Half Time Performer": 3}
                    rp = rank_priority.get(p['rank_name'], 99) if p['rank_name'] else 99
                    return (rp, p['nickname'].lower())
                online_players.sort(key=sort_key)

                # Send result as a Components V2 LayoutView
                result_view = OnlinePlayersResultView(online_players)
                await loading_msg.edit(content=None, view=result_view)
            else:
                await loading_msg.edit(content="🔴 No players are currently online")

        except Exception as e:
            logger.error(f"Failed to fetch online players: {str(e)}")
            await loading_msg.edit(content="❌ Failed to retrieve online players list")

    async def _update_monitor_press_count(self):
        """Rebuild and edit the monitor message to show the updated press count."""
        try:
            cog = self.cog
            if not cog or not cog.monitor_message:
                return
            now_ts = int(discord.utils.utcnow().timestamp())
            board_data = await cog._gather_status_data(cog.last_guild_state)
            if not board_data:
                return
            new_view = GuildStatusBoard(
                cog=cog,
                guild_name=board_data['guild_name'],
                guild_level=board_data['guild_level'],
                member_count=board_data['member_count'],
                apprentice_count=board_data['apprentice_count'],
                funds=board_data['funds'],
                total_fame=board_data['total_fame'],
                week_fame=board_data['week_fame'],
                gvg_points=board_data['gvg_points'],
                online_count=board_data['online_count'],
                weekly_leaderboard=board_data['weekly_leaderboard'],
                pending_apps=board_data['pending_apps'],
                now_ts=now_ts,
                next_update_ts=now_ts + 60,
                birthdays_this_week=board_data['birthdays_this_week'],
                press_count=cog.online_players_button_presses,
                league_info=board_data.get('league_info', {}),
            )
            await cog.monitor_message.edit(content=None, embeds=[], attachments=[], view=new_view)
        except Exception as e:
            logger.error(f"Failed to update monitor press count: {e}")


class GuildRegionSummaryView(LayoutView):
    """Components V2 LayoutView: shows 5 members per region with buttons to expand each region fully."""
    def __init__(self, guild_name: str, regions: dict, tag_map: dict, cog):
        super().__init__(timeout=120)
        self.guild_name = guild_name
        self.regions = regions
        self.tag_map = tag_map
        self.cog = cog

        self._rebuild()

    def _region_label(self, tag):
        return self.tag_map.get(tag, f"❓ {tag}")

    def _rebuild(self):
        """Rebuild the V2 layout (summary overview)."""
        self.clear_items()

        total_members = sum(len(m) for m in self.regions.values())
        sorted_tags = sorted(self.regions.keys(), key=lambda t: self._region_label(t))

        inner_items = [
            TextDisplay(f"# 🌍 {self.guild_name} — Members by Region\n**Total:** {total_members}  |  **Regions:** {len(sorted_tags)}"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        for tag in sorted_tags:
            member_list = self.regions[tag]
            sorted_members = sorted(member_list, key=lambda m: (not m['is_online'], m['nickname'].lower()))
            online_count = sum(1 for m in member_list if m['is_online'])
            region_label = self._region_label(tag)

            preview = sorted_members[:5]
            remaining = len(sorted_members) - 5

            lines = []
            for m in preview:
                online_icon = "🟢" if m['is_online'] else "⚫"
                number_id = m.get('number_id', 'N/A')
                lines.append(f"{online_icon} Lv{m['level']:<3} | {m['nickname']:<25} | ID: {number_id}")

            preview_text = "\n".join(lines)
            if remaining > 0:
                preview_text += f"\n... and {remaining} more"

            inner_items.append(TextDisplay(f"### {region_label} ({len(member_list)} members, 🟢 {online_count} online)\n```{preview_text}```"))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Region detail buttons in action rows
        # Group into rows of 5
        button_rows = []
        current_row = ActionRow()
        for idx, tag in enumerate(sorted_tags):
            label = f"{self._region_label(tag)} ({len(self.regions[tag])})"
            btn = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"region_detail_v2_{idx}"
            )
            btn.callback = self._make_detail_callback(tag)
            current_row.add_item(btn)
            if len(current_row.children) >= 5:
                button_rows.append(current_row)
                current_row = ActionRow()

        if current_row.children:
            button_rows.append(current_row)

        inner_items.extend(button_rows)

        container = Container(*inner_items, accent_color=BLURPLE)
        self.add_item(container)

    def _make_detail_callback(self, tag: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            members = self.regions[tag]
            sorted_members = sorted(members, key=lambda m: (not m['is_online'], m['nickname'].lower()))
            online_count = sum(1 for m in members if m['is_online'])
            region_label = self._region_label(tag)

            lines = []
            for m in sorted_members:
                online_icon = "🟢" if m['is_online'] else "⚫"
                number_id = m.get('number_id', 'N/A')
                lines.append(f"{online_icon} Lv{m['level']:<3} | {m['nickname']:<25} | ID: {number_id}")

            body = "\n".join(lines)

            # Use GuildDetailView for the detail with back button
            detail_view = GuildDetailView(
                title=f"🌍 {region_label} — {self.guild_name}",
                body=f"**{len(members)} members** | 🟢 {online_count} online\n\n```{body}```",
                accent=BLURPLE,
                back_view=self,
            )
            await interaction.edit_original_response(content=None, embed=None, view=detail_view)
        return callback

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class GuildRegionSelectView(LayoutView):
    """Components V2 LayoutView with buttons for selecting a guild to view region breakdown"""
    def __init__(self, clubs: list, guild_infos: list, cog):
        super().__init__(timeout=60)
        self.cog = cog
        self.clubs = clubs
        self.guild_infos = guild_infos
        
        self.clear_items()
        
        inner_items = [
            TextDisplay("# 🔍 Guild Search for Region View\nSelect a guild below to see its member breakdown by region."),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]
        
        button_row = ActionRow()
        for idx, club in enumerate(clubs[:5]):
            guild_name = "Unknown"
            if guild_infos and idx < len(guild_infos):
                info = guild_infos[idx]
                guild_name = info.get('base', {}).get('name', 'Unknown')
            
            label = f"{idx + 1}. {guild_name[:45]}" if len(guild_name) > 45 else f"{idx + 1}. {guild_name}"
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"guild_region_select_v2_{idx}"
            )
            btn.callback = self.make_callback(idx)
            button_row.add_item(btn)
            if len(button_row.children) >= 5:
                inner_items.append(button_row)
                button_row = ActionRow()
        
        if button_row.children:
            inner_items.append(button_row)
        
        # Cancel row
        cancel_row = ActionRow()
        cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="guild_region_select_cancel_v2",
        )
        cancel_btn.callback = self._cancel
        cancel_row.add_item(cancel_btn)
        inner_items.append(cancel_row)
        
        container = Container(*inner_items, accent_color=BLURPLE)
        self.add_item(container)
    
    def make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_guild_select(interaction, idx)
        return callback
    
    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Cancelled.", embed=None, view=None)
        self.stop()
    
    async def _handle_guild_select(self, interaction: discord.Interaction, idx: int):
        await interaction.response.defer()
        
        club = self.clubs[idx]
        club_id = club.get('club_id')
        hostnum = club.get('hostnum', 10103)
        
        if not club_id:
            await interaction.followup.send("❌ Invalid club data")
            return
        
        try:
            guild_data = await get_full_guild_info(club_id, hostnum=hostnum)
            if not guild_data or 'result' not in guild_data:
                await interaction.followup.send("❌ Guild not found or API error")
                return

            result = guild_data['result']
            members = result.get('members', {}).get('members', {})
            all_uids = list(members.keys())

            if not all_uids:
                await interaction.followup.send("❌ No members found in guild")
                return

            bulk_data = await get_bulk_players_info(all_uids, fields=["base"])
            if not bulk_data or bulk_data.get('code') != 0:
                await interaction.followup.send("❌ Failed to fetch player info")
                return

            players_result = bulk_data.get('result', {})
            tag_map = {
                "": "Unknown",
                "CN": "🇨🇳 CN (Mainland China)",
                "AS": "🌏 AS (Asia)",
                "EU": "🇪🇺 EU (Europe)",
                "HMT": "🇭🇰 HMT (Hong Kong/Macau/Taiwan)",
                "JP": "🇯🇵 JP (Japan)",
                "KR": "🇰🇷 KR (South Korea)",
                "NA": "🇺🇸 NA (North America)",
                "NAW": "🌎 NAW (North America West)",
                "SA": "🌎 SA (South America)",
                "SEA": "🌏 SEA (Southeast Asia)",
                "OC": "🌏 OC (Oceania)",
                "OTHER": "🌍 Other",
            }
            def get_region_label(tag):
                return tag_map.get(tag, f"❓ {tag}")

            regions = defaultdict(list)
            for pid, player_data in players_result.items():
                base = player_data.get('base', {})
                nickname = base.get('nickname', 'Unknown')
                level = base.get('level', 0)
                number_id = base.get('number_id', '')
                oversea_tag = str(base.get('oversea_tag', ''))
                is_online = base.get('is_online', 0) == 1
                regions[oversea_tag].append({
                    'pid': pid, 'number_id': str(number_id), 'nickname': nickname,
                    'level': level, 'is_online': is_online, 'oversea_tag': oversea_tag,
                })

            guild_name = result.get('base', {}).get('name', 'Unknown Guild')

            # Use GuildRegionSummaryView directly (V2) — no embed needed
            view = GuildRegionSummaryView(guild_name, regions, tag_map, self.cog)
            await interaction.edit_original_response(content=None, embed=None, view=view)

        except Exception as e:
            logger.error(f"Guild region select failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to load region data: `{str(e)}`")
    
    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class GuildSearchSelectView(LayoutView):
    """Components V2 LayoutView with buttons for selecting a guild from search results"""
    def __init__(self, clubs: list, guild_infos: list, cog, header: str = None):
        super().__init__(timeout=60)
        self.cog = cog
        self.clubs = clubs
        self.guild_infos = guild_infos
        
        self.clear_items()
        
        header_display = header or "# 🔍 Guild Search Results\nSelect a button below to view the guild details."
        inner_items = [
            TextDisplay(header_display),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]
        
        button_row = ActionRow()
        for idx, club in enumerate(clubs[:5]):
            guild_name = "Unknown"
            member_num = "?"
            apprentice_num = "?"
            
            if guild_infos and idx < len(guild_infos):
                info = guild_infos[idx]
                guild_name = info.get('base', {}).get('name', 'Unknown')
                member_num = info.get('members', {}).get('member_num', '?')
                apprentice_num = info.get('members', {}).get('apprentice_num', '?')
            
            label = f"{idx + 1}. {guild_name[:40]}" if len(guild_name) > 40 else f"{idx + 1}. {guild_name}"
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"guild_select_v2_{idx}"
            )
            btn.callback = self.make_callback(idx)
            button_row.add_item(btn)
            if len(button_row.children) >= 5:
                inner_items.append(button_row)
                button_row = ActionRow()
        
        if button_row.children:
            inner_items.append(button_row)
        
        # Cancel row
        cancel_row = ActionRow()
        cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="guild_select_cancel_v2",
        )
        cancel_btn.callback = self._cancel
        cancel_row.add_item(cancel_btn)
        inner_items.append(cancel_row)
        
        container = Container(*inner_items, accent_color=BLURPLE)
        self.add_item(container)
    
    def make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_guild_select(interaction, idx)
        return callback
    
    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Search cancelled.", embed=None, view=None)
        self.stop()
    
    async def _handle_guild_select(self, interaction: discord.Interaction, idx: int):
        await interaction.response.defer(ephemeral=True)
        
        club = self.clubs[idx]
        club_id = club.get('club_id')
        hostnum = club.get('hostnum', 10103)
        
        if not club_id:
            await interaction.followup.send("❌ Invalid club data", ephemeral=True)
            return
        
        loading_msg = await interaction.followup.send("📋 Loading guild data...", ephemeral=True, wait=True)
        
        try:
            logger.debug(f"Trying to fetch full guild info for selected club_id: {club_id} with hostnum: {hostnum}")
            guild_data = await get_full_guild_info(club_id, hostnum=hostnum)
            
            if not guild_data or 'result' not in guild_data:
                await loading_msg.edit(content="❌ Guild not found or API error")
                return
            
            result = guild_data['result']
            base = result.get('base', {})
            members = result.get('members', {})
            play = result.get('play', {})
            create_ts = base.get('create_ts', 0)
            
            leader_name = "None"
            vice_leader_name = "None"
            leader_pid = "None"
            vice_leader_pid = "None"
            
            member_list = members.get('members', {})
            for pid, member in member_list.items():
                post_list = member.get('post', [])
                if 1 in post_list:
                    leader_pid = pid
                if 2 in post_list:
                    vice_leader_pid = pid
            
            pids_to_fetch = []
            if leader_pid != "None":
                pids_to_fetch.append(leader_pid)
            if vice_leader_pid != "None":
                pids_to_fetch.append(vice_leader_pid)
            
            if pids_to_fetch:
                bulk_data = await get_bulk_players_info(pids_to_fetch, fields=["base"])
                if bulk_data and bulk_data.get('code') == 0:
                    players = bulk_data.get('result', {})
                    if leader_pid in players:
                        leader_base = players[leader_pid].get('base', {})
                        leader_name = leader_base.get('nickname', 'Unknown')
                    if vice_leader_pid in players:
                        vice_base = players[vice_leader_pid].get('base', {})
                        vice_leader_name = vice_base.get('nickname', 'Unknown')
            
            online = 0
            all_pids = list(member_list.keys())
            bulk_data = await get_bulk_players_info(all_pids, fields=["base"])
            if bulk_data and bulk_data.get('code') == 0:
                players = bulk_data.get('result', {})
                for pid, player_data in players.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online += 1
            
            announcement = result.get('gonggao_info', {}).get('msg')
            
            # Use GuildProfileView instead of embed
            view = GuildProfileView(
                guild_name=base.get('name', 'Unknown Guild'),
                guild_level=base.get('level', 0),
                member_count=members.get('member_num', 0),
                member_max=100,
                create_ts=create_ts,
                funds=base.get('fund', 0),
                total_fame=base.get('fame', 0),
                week_fame=base.get('week_fame', 0),
                gvg_points=play.get('pk_match_info', {}).get('battle_score', 0),
                leader_name=leader_name,
                vice_leader_name=vice_leader_name,
                online_count=online,
                announcement=announcement,
            )
            await interaction.edit_original_response(content=None, embed=None, view=view)
            await loading_msg.edit(content="✅ Guild found!")
            
        except Exception as e:
            logger.error(f"Guild detail fetch failed: {str(e)}", exc_info=True)
            await loading_msg.edit(content=f"❌ Failed to load guild details: `{str(e)}`")
    
    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class PlayerProfileView(LayoutView):
    """Components V2 LayoutView for player profile with stat category buttons."""
    
    GRADE_NAMES = {
        1: "Beginner", 2: "Novice", 3: "Silver", 4: "Adept",
        5: "Expert", 6: "Veteran", 7: "Master", 8: "Grandmaster",
        9: "Legend", 10: "Mythic",
    }
    
    SMALL_GRADE_SUFFIXES = {0: "", 1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
    
    def __init__(
        self,
        player_nickname: str,
        number_id: str,
        discord_user_id: int = None,
        ly_stage_name: str = None,
        level: int = 0,
        is_online: bool = False,
        is_invisible: bool = False,
        oversea_tag: str = "N/A",
        online_hours: float = 0,
        create_time: int = 0,
        player_signature: str = None,
        cover_img: str = None,
        cover_img_path: str = None,
        # Social
        birthday_str: str = None,
        jieyi_name: str = None,
        jieyi_text: str = None,
        likes_count: int = 0,
        likes_data_raw: dict = None,
        likes_history: list = None,
        # Masteries
        martial_mastery: float = 0,
        scholar_mastery: float = 0,
        healer_mastery: float = 0,
        explore_mastery: float = 0,
        # Attributes
        attr_str: float = 0,
        attr_con: float = 0,
        attr_bas: float = 0,
        attr_cri: float = 0,
        attr_agi: float = 0,
        # Sect
        school_emoji: str = "",
        school_name: str = None,
        school_rank: str = None,
        school_data: dict = None,
        # Fashion / Elegance
        fashion_score: int = 0,
        # Combat
        arena_1v1_rank: str = None,
        arena_1v1_max_winning_streak: int = 0,
        arena_1v1_total_num: int = 0,
        arena_3v3_rank: str = None,
        arena_3v3_total_num: int = 0,
        group_strategy: int = 0,
        group_strategy_total_num: int = 0,
        assist_points: int = 0,
        # Guild
        guild_name: str = None,
        is_our_guild: bool = False,
        guild_level: int = 0,
        guild_leader: str = None,
        guild_vice_leader: str = None,
        guild_members: int = 0,
        guild_funds: int = 0,
        guild_fame: int = 0,
        guild_announcement: str = None,
        # Kongfu
        kongfu_main: str = None,
        kongfu_sub: str = None,
        kongfu_role: str = None,
        is_verified: bool = False,
        # Head avatar from data/avatars/mapped/
        head_avatar_path: str = None,
        head_id=None,
        body_type=None,
        sender_pid: str = None,
        # Combat Plan / Equipments
        player_pid: str = None,
        player_hostnum: int = None,
        # Homestead
        homeland_info: dict = None,
        # Achievements
        achievement_data: dict = None,
    ):
        super().__init__(timeout=180)
        
        # Storage for file attachments (e.g. head avatar thumbnail)
        self._files = []
        
        # Store all data
        self.player_nickname = player_nickname
        self.number_id = number_id
        self.discord_user_id = discord_user_id
        self.ly_stage_name = ly_stage_name
        self.level = level
        self.is_online = is_online
        self.is_invisible = is_invisible
        self.oversea_tag = oversea_tag
        self.online_hours = online_hours
        self.create_time = create_time
        self.player_signature = player_signature
        self.cover_img = cover_img
        self.cover_img_path = cover_img_path
        self.birthday_str = birthday_str
        self.jieyi_name = jieyi_name
        self.jieyi_text = jieyi_text
        self.likes_count = likes_count
        self.likes_data_raw = likes_data_raw or {}
        self.likes_history = likes_history or []
        self.likes_page = 0  # Pagination for likes history
        self.martial_mastery = martial_mastery
        self.scholar_mastery = scholar_mastery
        self.healer_mastery = healer_mastery
        self.explore_mastery = explore_mastery
        self.attr_str = attr_str
        self.attr_con = attr_con
        self.attr_bas = attr_bas
        self.attr_cri = attr_cri
        self.attr_agi = attr_agi
        self.school_emoji = school_emoji
        self.school_name = school_name
        self.school_rank = school_rank
        self.school_data = school_data
        self.fashion_score = fashion_score
        self.arena_1v1_rank = arena_1v1_rank
        self.arena_1v1_max_winning_streak = arena_1v1_max_winning_streak
        self.arena_1v1_total_num = arena_1v1_total_num
        self.arena_3v3_rank = arena_3v3_rank
        self.arena_3v3_total_num = arena_3v3_total_num
        self.group_strategy = group_strategy
        self.group_strategy_total_num = group_strategy_total_num
        self.assist_points = assist_points
        self.guild_name = guild_name
        self.is_our_guild = is_our_guild
        self.guild_level = guild_level
        self.guild_leader = guild_leader
        self.guild_vice_leader = guild_vice_leader
        self.guild_members = guild_members
        self.guild_funds = guild_funds
        self.guild_fame = guild_fame
        self.guild_announcement = guild_announcement
        self.kongfu_main = kongfu_main
        self.kongfu_sub = kongfu_sub
        self.kongfu_role = kongfu_role
        self.is_verified = is_verified
        self.head_avatar_path = head_avatar_path
        self.head_id = head_id
        self.body_type = body_type
        self.sender_pid = sender_pid
        self.player_pid = player_pid
        self.player_hostnum = player_hostnum
        self.homeland_info = homeland_info
        self.achievement_data = achievement_data
        # Equipment pagination state
        self.equipments_page = 0
        self._equipments_lines: list = []

        self._build_overview()
    
    def _build_header_text(self) -> str:
        """Build the persistent header text (name, core stats, social, sect)."""
        lines = []
        
        # Title line
        title = f"# {self.player_nickname} | {self.number_id}"
        if self.ly_stage_name:
            title += f" ({self.ly_stage_name})"
        if self.discord_user_id:
            title += f"\n### <@{self.discord_user_id}>'s profile"
        lines.append(title)
        
        # Signature
        if self.player_signature:
            lines.append(f"\n*{self.player_signature}*")
        
        lines.append("")
        
        # Core stats
        if not self.is_invisible:
            online_str = "🟢 **Online**" if self.is_online else "🔴 **Offline**"
        else:
            online_str = "⚫ **Invisible**"
        
        lines.append(f"🏆 **Level:** {self.level}    {online_str}")
        
        # Always show these (moved from social/sect buttons)
        if self.create_time:
            lines.append(f"📅 **Account:** <t:{int(self.create_time)}:F> <t:{int(self.create_time)}:R>")
        if self.birthday_str:
            lines.append(f"🎂 **Birthday:** {self.birthday_str}")
        
        lines.append(f"🌍 **Region:** {self.oversea_tag}")
        lines.append(f"⌛ **Online:** {self.online_hours}h")
        
        if self.is_verified:
            lines.append(f"💃 **Elegance:** {int(self.fashion_score):,}" if int(self.fashion_score or 0) else "")
            lines.append(f"❤️ **Likes:** {int(self.likes_count):,}" if int(self.likes_count or 0) else "")
            lines.append(f"🤝 **Assist Points:** {int(self.assist_points):,}" if int(self.assist_points or 0) else "")
            
            # Sworn Cohort (moved from social button)
            if self.jieyi_name:
                cohort_line = f"🤝 **Sworn Cohort:** {self.jieyi_name}"
                if self.jieyi_text:
                    cohort_line += f" — *{self.jieyi_text}*"
                lines.append(cohort_line)
        
        # Sect info (always shown now)
        if self.school_name:
            sect_line = f"{self.school_emoji} **Sect:** {self.school_name}"
            if self.school_rank:
                sect_line += f" — {self.school_rank}"
            lines.append(sect_line)
        
        # Guild info
        if self.guild_name:
            guild_icon = "✅" if self.is_our_guild else "🏰"
            lines.append(f"{guild_icon} **Guild:** {self.guild_name}")
        
        # Online stats always
        lines.append(f"")
        
        # Non-verified hint
        if not self.is_verified:
            lines.append("🔗 **Bind your account** in <#1469961307154288703> to view full stats.")
        
        return "\n".join(filter(None, lines))
    
    def _build_container(self, detail_items: list = None) -> Container:
        """Build a single Container: header + optional detail."""
        inner = []
        
        # Header: MediaGallery first if available
        if self.cover_img:
            gallery = MediaGallery()
            # Prefer locally downloaded file (cover_img_path) so Discord can render it
            if self.cover_img_path and os.path.exists(self.cover_img_path):
                cover_filename = os.path.basename(self.cover_img_path)
                self._files.append(discord.File(self.cover_img_path, filename=cover_filename))
                gallery.add_item(media=f"attachment://{cover_filename}", description="Fashion Cover")
            else:
                gallery.add_item(media=self.cover_img, description="Fashion Cover")
            inner.append(gallery)
        
        # Header: text display (with optional head avatar thumbnail)
        header_text = self._build_header_text()
        if self.head_avatar_path:
            head_ext = os.path.splitext(self.head_avatar_path)[1] or ".png"
            head_filename = f"head_pfp{head_ext}"
            self._files.append(discord.File(self.head_avatar_path, filename=head_filename))
            section = Section(accessory=Thumbnail(media=f"attachment://{head_filename}"))
            section.add_item(TextDisplay(header_text))
            inner.append(section)
        else:
            inner.append(TextDisplay(header_text))
        
        # Separator
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        if detail_items:
            # Detail view: supplied items (TextDisplay + Separator + back button)
            inner.extend(detail_items)
        else:
            # Overview: Select menu
            select_options = [
                discord.SelectOption(label="Combat", value="combat", emoji="⚔️"),
                discord.SelectOption(label="Masteries", value="masteries", emoji="🎓"),
                discord.SelectOption(label="Attributes", value="attributes", emoji="📊"),
                discord.SelectOption(label="Kongfu & Role", value="kongfu", emoji="🔧"),
                discord.SelectOption(label="Achievements", value="achievements", emoji="🏆"),
                #discord.SelectOption(label="Equipments", value="equipments", emoji="🛡️"),
                discord.SelectOption(label="Guild Profile", value="guild", emoji="🏰"),
                discord.SelectOption(label="Homestead", value="homestead", emoji="🏡"),
                discord.SelectOption(label="Likes", value="likes", emoji="❤️"),
                discord.SelectOption(label="Sect", value="school", emoji="🏫")
            ]
            # Only show Set Avatar option when head_id is present but no mapped avatar exists
            if self.head_id is not None and self.head_avatar_path is None and self.body_type in (0, 1):
                select_options.append(discord.SelectOption(label="Set Avatar", value="set_avatar", emoji="🖼️"))

            if self.discord_user_id in [125331697867816961, 96417753300209664, 617161435398799390]:
                select_options.append(discord.SelectOption(label="Equipments", value="equipments", emoji="🛡️"))
            
            select_row = ActionRow()
            select_menu = Select(
                placeholder="View detailed stats...",
                options=select_options,
                custom_id="player_profile_select",
            )
            select_menu.callback = self._handle_select_change
            select_row.add_item(select_menu)
            inner.append(select_row)
        
        return Container(*inner, accent_color=BLURPLE)
    
    def _resolve_files(self) -> list:
        """Return a copy of attached discord.File objects (e.g. head avatar)."""
        return list(getattr(self, "_files", []))

    def _build_overview(self):
        """Build the default overview (single Container with header + buttons)."""
        self.clear_items()
        self._files = []  # reset files each rebuild
        self.add_item(self._build_container())
    
    def _show_detail(self, title: str, stat_lines: list, accent: int):
        """Show a detail view: single Container with header + detail + back button."""
        inner = []
        inner.append(TextDisplay(f"# {title}\n\n" + "\n".join(stat_lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        back_row = ActionRow()
        back_btn = discord.ui.Button(label="🔙 Overview", style=discord.ButtonStyle.secondary, custom_id="player_back")
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner.append(back_row)
        
        self.clear_items()
        self.add_item(self._build_container(detail_items=inner))
    
    @staticmethod
    def _format_rank(grade: int, small_grade: int) -> str:
        grade_name = PlayerProfileView.GRADE_NAMES.get(grade, f"Unknown ({grade})")
        small_suffix = PlayerProfileView.SMALL_GRADE_SUFFIXES.get(small_grade, str(small_grade))
        return f"{grade_name} {small_suffix}" if small_suffix else grade_name
    
    async def _handle_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self._build_overview()
        await interaction.edit_original_response(view=self)
    
    async def _handle_select_change(self, interaction: discord.Interaction):
        """Handle Select menu option selection."""
        selected = interaction.data.get("values", [""])[0]
        
        handler_map = {
            "combat": self._handle_combat,
            "masteries": self._handle_masteries,
            "attributes": self._handle_attributes,
            "kongfu": self._handle_kongfu,
            "achievements": self._handle_achievements,
            "equipments": self._handle_equipments,
            "guild": self._handle_guild,
            "homestead": self._handle_homestead,
            "likes": self._handle_likes,
            "set_avatar": self._handle_set_avatar,
            "school": self._handle_school
        }
        
        handler = handler_map.get(selected)
        if handler:
            await handler(interaction)
    
    async def _handle_combat(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        if self.arena_1v1_rank:
            lines.append(f"⚔️ **1v1 Arena Rank:** {self.arena_1v1_rank}")
        if self.arena_1v1_max_winning_streak:
            lines.append(f"⚔️ **1v1 Max Winning Streak:** {self.arena_1v1_max_winning_streak}")
        if self.arena_1v1_total_num:
            lines.append(f"⚔️ **1v1 Total Battles:** {self.arena_1v1_total_num}")
        if self.arena_3v3_rank:
            lines.append(f"⚔️ **3v3 Arena Rank:** {self.arena_3v3_rank}")
        if self.arena_3v3_total_num:
            lines.append(f"⚔️ **3v3 Total Battles:** {self.arena_3v3_total_num}")
        if self.group_strategy:
            lines.append(f"📋 **Group Strategy:** {self.group_strategy}")
        if self.group_strategy_total_num:
            lines.append(f"📋 **Group Strategy Battles:** {self.group_strategy_total_num}")
        if not lines:
            lines.append("*No combat data available*")
        
        self._show_detail("⚔️ Combat & Arena", lines, accent=0xE74C3C)
        await interaction.edit_original_response(view=self)
    
    async def _handle_masteries(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        lines.append(f"⚔️ **Martial Mastery:** {self.martial_mastery}")
        lines.append(f"📚 **Scholar Mastery:** {self.scholar_mastery}")
        lines.append(f"💚 **Healer Mastery:** {self.healer_mastery}")
        lines.append(f"🗺️ **Exploration Mastery:** {self.explore_mastery}")
        
        self._show_detail("🎓 Masteries", lines, accent=0x2ECC71)
        await interaction.edit_original_response(view=self)
    
    async def _handle_attributes(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        lines.append(f"🥊 **Power (STR):** {self.attr_str}")
        lines.append(f"🛡️ **Body (CON):** {self.attr_con}")
        lines.append(f"⚡ **Momentum (BAS):** {self.attr_bas}")
        lines.append(f"💨 **Agility (CRI):** {self.attr_cri}")
        lines.append(f"🔰 **Defense (AGI):** {self.attr_agi}")
        
        self._show_detail("📊 Base Attributes", lines, accent=0x3498DB)
        await interaction.edit_original_response(view=self)
    
    async def _handle_kongfu(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        if self.kongfu_main:
            lines.append(f"🗡️ **Main Weapon:** {self.kongfu_main}")
        if self.kongfu_sub:
            lines.append(f"🗡️ **Sub Weapon:** {self.kongfu_sub}")
        if self.kongfu_role:
            lines.append(f"🎯 **Role:** {self.kongfu_role}")
        if not lines:
            lines.append("*No kongfu data available*")
        
        self._show_detail("🔧 Kongfu & Role", lines, accent=0x1ABC9C)
        await interaction.edit_original_response(view=self)
    
    async def on_timeout(self):
        """Disable all buttons and selects when the view times out, keeping the current page visible."""
        # Disable all buttons and selects in the current layout without changing the page
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True
                            elif isinstance(item, discord.ui.Select):
                                item.disabled = True
                    elif isinstance(sub, discord.ui.Button):
                        sub.disabled = True
                    elif isinstance(sub, discord.ui.Select):
                        sub.disabled = True
            elif isinstance(child, ActionRow):
                for item in child.children:
                    if isinstance(item, discord.ui.Button):
                        item.disabled = True
                    elif isinstance(item, discord.ui.Select):
                        item.disabled = True

        # Best-effort: edit the original message so the disabled state is visible to users.
        try:
            original = getattr(self, "_original_message", None)
            if original is not None:
                view_files = self._resolve_files()
                await original.edit(view=self, attachments=view_files)
        except Exception:
            # Message may have been deleted or we may not own it — silently ignore.
            pass

        self.stop()
    
    async def _handle_set_avatar(self, interaction: discord.Interaction):
        """Open the avatar picker (CategoryPickerView) so the user can map an avatar for this player."""
        try:
            from cogs.live_chat_cog import CategoryPickerView
        except ImportError:
            await interaction.response.send_message("❌ Avatar picker not available.", ephemeral=True)
            return

        cog = interaction.client.get_cog("LiveChatCog")
        if cog is None:
            await interaction.response.send_message("❌ LiveChatCog is not loaded.", ephemeral=True)
            return

        view = CategoryPickerView(
            cog=cog,
            head_id=str(self.head_id),
            body_type=self.body_type,
            suggested_by=interaction.user,
            sender_nickname=self.player_nickname,
            sender_pid=self.sender_pid,
        )
        await interaction.response.send_message(
            content=None,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _handle_school(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = await self._build_school_detail()
        self.clear_items()
        self._show_detail(f"{self.school_emoji} {self.school_name} {self.school_emoji}", lines, accent=0x9B59B6)
        await interaction.edit_original_response(view=self)


    async def _build_school_detail(self) -> list:
        lines = []
        if self.school_data:
            chief_campaign = self.school_data.get('chief_campaign', {})
            if chief_campaign:
                # Calculate votes left
                school_status = self.school_data.get('status', 0)
                if school_status in VOTE_COUNTS:
                    vote_count = VOTE_COUNTS[school_status]
                    vote_num = chief_campaign.get('vote_num', 0)
                    lines.append(f"**Total**: {vote_count:,} vote{'' if vote_count == 1 else 's'}, voted {vote_num:,} time{'' if vote_num == 1 else 's'}, {vote_count - vote_num:,} vote{'' if vote_count - vote_num == 1 else 's'} left")
                vote_list = chief_campaign.get('vote_list', [])
                if len(vote_list) != vote_num:
                    lines.append(f"**Out of Sect**: voted {len(vote_list) - vote_num:,} time{'' if len(vote_list) - vote_num == 1 else 's'}")

            rule = self.school_data.get('rule', {})
            if rule:
                msd_paper = rule.get('msd_paper', {})
                if msd_paper:
                    pid2hostnum = msd_paper.get('pid2hostnum', {})
                    if pid2hostnum:
                        # Get name for each pid
                        response = await get_bulk_players_info(list(pid2hostnum.keys()), fields=['base'])
                        result = response.get('result', {})
                        pid2name = {}
                        for id in result:
                            base = result[id].get('base', {})
                            name = base.get('nickname')
                            pid2name[id] = name

                        logger.debug(f"pid2name: {pid2name}")
                        
                            
                    fellow_score = msd_paper.get('fellow_score', {})
                    if fellow_score:
                        pupil = fellow_score.get('pupil', {})
                        if pupil:
                            lines.append("")
                            lines.append(f"**Pupil Submisson** {sum(pupil.values())} (Copies: {len(pupil)})")
                            # Sort pupil.items by score
                            pupil = dict(sorted(pupil.items(), key=lambda item: item[1], reverse=True))
                            for pid, score in pupil.items():
                                name = pid2name.get(pid)
                                if name:
                                    lines.append(f"{name}: {score}")
                        collab = fellow_score.get('collab', {})
                        if collab:
                            lines.append("")
                            lines.append(f"**Co-authored Submisson** {sum(collab.values())} (Copies: {len(collab)})")
                            # Sort collab.items by score
                            collab = dict(sorted(collab.items(), key=lambda item: item[1], reverse=True))
                            for pid, score in collab.items():
                                name = pid2name.get(pid)
                                if name:
                                    lines.append(f"{name}: {score}")
                        
                

        else:
            lines.append("*No school data available*")
        return lines


    def _build_likes_detail(self) -> list:
        """Build the likes detail view items (text + buttons) without responding to interaction."""
        from utility.api_constants import LIKES
        lines = []
        
        # Section 1: Topic breakdown
        if self.likes_data_raw:
            total = 0
            for topic_id_str, topic_data in self.likes_data_raw.items():
                if not isinstance(topic_data, dict):
                    continue
                n_likes = topic_data.get('n_likes', 0)
                if n_likes > 0:
                    try:
                        tid = int(topic_id_str)
                    except (ValueError, TypeError):
                        continue
                    topic_name = LIKES.get(tid, f"Topic {tid}")
                    lines.append(f"❤️ **{topic_name}:** {n_likes:,}")
                    total += n_likes
            if total > 0:
                lines.append(f"\n**Total Likes:** {total:,}")
        
        # Section 2: Who liked this player (paginated, 10 per page)
        if self.likes_history:
            lines.append("## ❤️ Recent Likes\n")
            
            # Calculate pagination
            ITEMS_PER_PAGE = 10
            total_items = len(self.likes_history)
            total_pages = max(1, -(-total_items // ITEMS_PER_PAGE))  # ceil division
            
            # Clamp page to valid range
            self.likes_page = max(0, min(self.likes_page, total_pages - 1))
            
            # Get items for current page
            start_idx = self.likes_page * ITEMS_PER_PAGE
            end_idx = start_idx + ITEMS_PER_PAGE
            page_items = self.likes_history[start_idx:end_idx]
            
            # Display items
            for liker in page_items:
                liker_name = liker.get('nickname', 'Unknown')
                liker_level = liker.get('level', '?')
                liker_id = liker.get('number_id', 'N/A')
                topic_id = liker.get('topic_id', 0)
                timestamp = liker.get('timestamp', 0)
                
                # Format topic name if available
                topic_name = LIKES.get(topic_id, '')
                topic_str = f" ({topic_name})" if topic_name else ""
                
                # Format timestamp in both absolute and relative formats
                time_str = ""
                if timestamp:
                    time_str = f" <t:{int(timestamp)}:D> <t:{int(timestamp)}:R>"
                
                lines.append(f"• **{liker_name}** Lv.{liker_level}{topic_str}{time_str}")
        
        if not lines:
            lines.append("*No likes data available*")
        
        # Build detail view items
        inner = []
        inner.append(TextDisplay(f"# ❤️ Likes Breakdown\n\n" + "\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        # Add pagination buttons if there are multiple pages
        if self.likes_history and len(self.likes_history) > 10:
            ITEMS_PER_PAGE = 10
            total_pages = max(1, -(-len(self.likes_history) // ITEMS_PER_PAGE))
            
            nav_row = ActionRow()
            
            # Prev button
            prev_btn = Button(
                style=discord.ButtonStyle.secondary,
                label="◀ Prev",
                custom_id="likes_prev",
                disabled=self.likes_page <= 0,
            )
            prev_btn.callback = self._handle_likes_page_prev
            nav_row.add_item(prev_btn)
            
            # Page indicator
            page_label = Button(
                style=discord.ButtonStyle.secondary,
                label=f"{self.likes_page + 1}/{total_pages}",
                custom_id="likes_page_label",
                disabled=True,
            )
            nav_row.add_item(page_label)
            
            # Next button
            next_btn = Button(
                style=discord.ButtonStyle.secondary,
                label="Next ▶",
                custom_id="likes_next",
                disabled=self.likes_page >= total_pages - 1,
            )
            next_btn.callback = self._handle_likes_page_next
            nav_row.add_item(next_btn)
            
            inner.append(nav_row)
        
        # Back button
        back_row = ActionRow()
        back_btn = discord.ui.Button(label="🔙 Overview", style=discord.ButtonStyle.secondary, custom_id="player_back")
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner.append(back_row)
        
        return inner
    
    async def _handle_likes(self, interaction: discord.Interaction):
        await interaction.response.defer()
        inner = self._build_likes_detail()
        self.clear_items()
        self.add_item(self._build_container(detail_items=inner))
        await interaction.edit_original_response(view=self)
    
    async def _handle_likes_page_prev(self, interaction: discord.Interaction):
        """Navigate to previous page of likes."""
        if self.likes_page > 0:
            self.likes_page -= 1
        await self._handle_likes(interaction)
    
    async def _handle_likes_page_next(self, interaction: discord.Interaction):
        """Navigate to next page of likes."""
        ITEMS_PER_PAGE = 10
        total_pages = max(1, -(-len(self.likes_history) // ITEMS_PER_PAGE))
        if self.likes_page < total_pages - 1:
            self.likes_page += 1
        await self._handle_likes(interaction)

    async def _handle_homestead(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        if self.homeland_info:
            homeland_base = self.homeland_info.get('homeland_base', {})
            homeland_mate = self.homeland_info.get('homeland_mate', {})
            taoyuan_desc_data = self.homeland_info.get('taoyuan_description', {})
            
            if homeland_base:
                base_level = homeland_base.get('level', 0)
                base_name = homeland_base.get('name', 'Homestead')
                lines.append(f"🏡 **Homestead:** {base_name}")
                lines.append(f"⭐ **Level:** {base_level}")
                lines.append(f"🪙 **Bounty Gourd:** {homeland_base.get('token', 0):,}")
                lines.append(f"🌟 **Prosperity:** {homeland_base.get('prosperity', 0):,}")
            
            # Handle mate info - check nested mate_info dict
            if homeland_mate and isinstance(homeland_mate, dict):
                mate_info = homeland_mate.get('mate_info', {})
                if mate_info and isinstance(mate_info, dict):
                    mate_name = mate_info.get('nickname')
                    if mate_name:
                        lines.append(f"👫 **Mate:** {mate_name}")
            
            # Handle taoyuan description - it's a dict with 'description' key
            if taoyuan_desc_data and isinstance(taoyuan_desc_data, dict):
                description_text = taoyuan_desc_data.get('description', '')
                if description_text and description_text.strip():
                    lines.append(f"\n📝 **Description:**\n{description_text}")
        else:
            lines.append("*No homestead data available*")
        
        self._show_detail("🏡 Homestead", lines, accent=0xE67E22)
        await interaction.edit_original_response(view=self)

    async def _handle_guild(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        if self.guild_name:
            lines.append(f"🏰 **Guild:** {self.guild_name}")
            if self.is_our_guild:
                lines.append("✅ **Our Guild Member**")
            if self.guild_level:
                lines.append(f"⭐ **Level:** {self.guild_level}")
            if self.guild_members:
                lines.append(f"👥 **Members:** {self.guild_members}/100")
            if self.guild_leader:
                lines.append(f"👑 **Leader:** {self.guild_leader}")
            if self.guild_vice_leader:
                lines.append(f"⚔️ **Vice Leader:** {self.guild_vice_leader}")
            if self.guild_funds:
                lines.append(f"💰 **Funds:** {int(self.guild_funds):,}")
            if self.guild_fame:
                lines.append(f"📈 **Fame:** {int(self.guild_fame):,}")
            if self.guild_announcement:
                lines.append("")
                lines.append(f"📢 **Announcement:** {self.guild_announcement}")
        else:
            lines.append("*No guild info available*")
        
        self._show_detail("🏰 Guild Profile", lines, accent=BLURPLE)
        await interaction.edit_original_response(view=self)

    async def _handle_achievements(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lines = []
        quantity = self.achievement_data.get('quantity', {})
        if quantity:
            if quantity.get(3):
                lines.append(f"🏆 **Expert:** {quantity.get(3)}")
            if quantity.get(2):
                lines.append(f"🏆 **Hard**: {quantity.get(2)}")
            if quantity.get(1):
                lines.append(f"🏆 **Normal**: {quantity.get(1)}")
        else:
            lines.append("*No achievement data available*")
        
        self._show_detail("🏆 Achievements", lines, accent=0x3498DB)
        await interaction.edit_original_response(view=self)

    @staticmethod
    def _smart_round(value) -> str:
        """Smart rounding for display: removes floating-point artifacts and trailing zeros."""
        if not isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, int):
            return str(value)
        # Round to 4 decimal places first to clean up floating point artifacts
        rounded = round(value, 4)
        # If it's effectively an integer, show as integer
        if rounded == int(rounded):
            return str(int(rounded))
        # Otherwise strip trailing zeros
        s = f"{rounded:.4f}".rstrip('0').rstrip('.')
        return s

    async def _handle_equipments(self, interaction: discord.Interaction):
        """Fetch and display the player's equipped items and their stats (paginated)."""
        await interaction.response.defer()

        if not self.player_pid or not self.player_hostnum:
            self._show_detail("🛡️ Equipments", ["*No player data available to fetch equipments*"], accent=0xF39C12)
            await interaction.edit_original_response(view=self)
            return

        try:
            combat_data = await get_player_combat_plan(self.player_pid, self.player_hostnum)
        except Exception as e:
            logger.error(f"Failed to fetch combat plan for {self.player_pid}: {e}")
            self._show_detail("🛡️ Equipments", [f"❌ Failed to load equipments: `{str(e)}`"], accent=0xF39C12)
            await interaction.edit_original_response(view=self)
            return

        if not combat_data or combat_data.get('code') != 0:
            self._show_detail("🛡️ Equipments", ["*No equipment data available*"], accent=0xF39C12)
            await interaction.edit_original_response(view=self)
            return

        result = combat_data.get('result', {})
        wear_equips = result.get('wear_equips', {})
        equips_map = result.get('combat_plan', {}).get('equips', {})

        if not wear_equips:
            self._show_detail("🛡️ Equipments", ["*No equipped items found*"], accent=0xF39C12)
            await interaction.edit_original_response(view=self)
            return

        # Apply affix mapping to replace numeric affix IDs with human-readable names
        try:
            wear_equips = await map_data(wear_equips)
        except Exception as map_err:
            logger.warning(f"Affix mapping failed (non-critical): {map_err}")

        # Helper to extract a display name from a value that may be mapped
        def _affix_display(val):
            """If val is a mapped affix object, return its name. Otherwise return the raw value."""
            if isinstance(val, dict) and val.get('_affix'):
                return val.get('name', str(val.get('id', val)))
            return val

        # Slot number to human-readable name mapping
        SLOT_NAMES = {
            1: "Primary Weapon",
            2: "Secondary Weapon",
            3: "Helmet",
            4: "Chestpiece",
            5: "Greaves",
            8: "Bracer",
            9: "Archery Jade",
            10: "Disc",
            11: "Pendant",
            21: "Bow and Arrow",
        }

        # Build all equipment lines into a list
        all_lines = []
        # Sort slots by their display order
        sorted_slots = sorted(wear_equips.keys(), key=lambda s: int(s) if str(s).isdigit() else 999)

        for slot_key in sorted_slots:
            item = wear_equips[slot_key]
            slot_num = int(slot_key) if str(slot_key).isdigit() else slot_key
            slot_name = SLOT_NAMES.get(slot_num, f"Slot {slot_num}")

            item_no = item.get('No', 0)
            ex_data = item.get('ex', {})
            base_attrs = ex_data.get('base_attrs', {})
            base_affixes = ex_data.get('base_affixes', [])
            durability = ex_data.get('durability', '?')
            tone_determin = ex_data.get('tone_determin', None)
            another_determin = ex_data.get('another_determin', None)
            retoned = ex_data.get('retoned', 0)
            suffix = ex_data.get('suffix', 0)
            gain_ts = ex_data.get('gain_ts', 0)

            # Build item display — Idea D: bullet list with emoji headers
            slot_lines = []
            slot_lines.append(f"🪪 **{slot_name}** (#{item_no}) — <t:{int(gain_ts)}:R>")

            # Base attributes
            if base_attrs:
                attr_parts = []
                for attr_key, attr_val in base_attrs.items():
                    attr_parts.append(f"{attr_key}: **{self._smart_round(attr_val)}**")
                slot_lines.append(f"📊 {'  |  '.join(attr_parts)}")

            # Base affixes — bullet list
            if base_affixes:
                slot_lines.append("📎 Affixes:")
                for affix in base_affixes:
                    if isinstance(affix, list) and len(affix) >= 2:
                        affix_id = affix[0]
                        affix_val = affix[1]
                        display_id = _affix_display(affix_id)
                        slot_lines.append(f"  • {display_id}  →  **{self._smart_round(affix_val)}**")

            # Tone / Determin
            tone_parts = []
            if tone_determin:
                tone_parts.append(f"Tone: {_affix_display(tone_determin)}")
            if another_determin and isinstance(another_determin, list) and len(another_determin) >= 2:
                tone_parts.append(f"Det: {_affix_display(another_determin[0])} ({another_determin[1]})")
            if tone_parts:
                slot_lines.append(f"💠 {'  ·  '.join(tone_parts)}")

            # Durability / Retone / Suffix
            info_parts = [f"Dura: {durability}/100"]
            if retoned:
                info_parts.append(f"Retoned: {retoned}")
            if suffix:
                info_parts.append(f"Suffix: {suffix}")
            slot_lines.append(f"🔧 {'  ·  '.join(info_parts)}")

            slot_lines.append("")  # blank line between items
            all_lines.append((slot_num, slot_name, slot_lines))

        if not all_lines:
            self._show_detail("🛡️ Equipments", ["*No equipment data to display*"], accent=0xF39C12)
            await interaction.edit_original_response(view=self)
            return

        # Define page groups: (slots, label)
        PAGE_GROUPS = [
            ({1, 2, 10, 11}, "Weapons & Accessories"),
            ({3, 4, 5, 8}, "Armor"),
            ({9, 21}, "Ranged & Jade"),
        ]

        # Assign each item to a page
        page_items = {0: [], 1: [], 2: []}
        for slot_num, slot_name, slot_lines in all_lines:
            for page_idx, (slots, _) in enumerate(PAGE_GROUPS):
                if slot_num in slots:
                    page_items[page_idx].extend(slot_lines)
                    break
            else:
                # Unknown slot — put on page 0
                page_items[0].extend(slot_lines)

        # Store the page data for navigation
        self._equipments_lines = [page_items[i] for i in range(3)]
        self.equipments_page = 0

        await self._show_equipments_page(interaction)

    async def _show_equipments_page(self, interaction: discord.Interaction):
        """Display the current equipment page with navigation buttons."""
        page = self.equipments_page
        total_pages = 3
        page_labels = ["Weapons & Accessories", "Armor", "Ranged & Jade"]

        lines = self._equipments_lines[page]
        if not lines:
            lines = ["*No items on this page*"]

        title = f"🛡️ Equipments — {page_labels[page]} ({page + 1}/{total_pages})"

        # Build detail items with navigation
        inner = []
        inner.append(TextDisplay(f"# {title}\n\n" + "\n".join(lines)))
        inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Navigation row
        nav_row = ActionRow()
        prev_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="◀ Prev",
            custom_id="equip_prev",
            disabled=page <= 0,
        )
        prev_btn.callback = self._handle_equip_prev
        nav_row.add_item(prev_btn)

        page_label = Button(
            style=discord.ButtonStyle.secondary,
            label=f"{page + 1}/{total_pages}",
            custom_id="equip_page_label",
            disabled=True,
        )
        nav_row.add_item(page_label)

        next_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="Next ▶",
            custom_id="equip_next",
            disabled=page >= total_pages - 1,
        )
        next_btn.callback = self._handle_equip_next
        nav_row.add_item(next_btn)
        inner.append(nav_row)

        # Back button
        back_row = ActionRow()
        back_btn = discord.ui.Button(label="🔙 Overview", style=discord.ButtonStyle.secondary, custom_id="player_back")
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner.append(back_row)

        self.clear_items()
        self.add_item(self._build_container(detail_items=inner))
        await interaction.edit_original_response(view=self)

    async def _handle_equip_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.equipments_page > 0:
            self.equipments_page -= 1
        await self._show_equipments_page(interaction)

    async def _handle_equip_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.equipments_page < 2:
            self.equipments_page += 1
        await self._show_equipments_page(interaction)


class Inactive(LayoutView):
    """LayoutView displaying guild members below an activity-point threshold,
    with tab switching (Last Week / This Week) and pagination (10 per page)."""

    ITEMS_PER_PAGE = 10

    def __init__(self, inactive_last_week: list, inactive_this_week: list, point_threshold: int):
        super().__init__(timeout=180)
        self.inactive_last_week = inactive_last_week
        self.inactive_this_week = inactive_this_week
        self.point_threshold = point_threshold
        self.page = 0
        self.active_tab = "last_week"  # or "this_week" or "both"
        self.sort_by = "points_asc"  # points_asc, points_desc, name_az, logout_newest, logout_oldest, absent_first

        # Compute "failed both" — members appearing in BOTH lists (same nickname)
        # Build a lookup of last week's points by nickname
        last_week_pts = {nickname: points for nickname, points, _, _, _ in inactive_last_week}
        self.inactive_both = []
        for nickname, this_pts, logout, number_id, has_absent_role in inactive_this_week:
            if nickname in last_week_pts:
                self.inactive_both.append((nickname, last_week_pts[nickname], this_pts, logout, number_id, has_absent_role))

        self._rebuild()

    # ── helpers ──────────────────────────────────────────────────────

    def _current_list(self) -> list:
        """Return a shallow copy of the current tab's list, then sort it."""
        if self.active_tab == "this_week":
            current = list(self.inactive_this_week)
        elif self.active_tab == "both":
            current = list(self.inactive_both)
        else:
            current = list(self.inactive_last_week)
        
        # Apply sort
        if self.sort_by == "points_asc":
            # Last week / this week: sort by points (index 1) asc
            # Both: sort by this week pts (index 2) asc
            if self.active_tab == "both":
                current.sort(key=lambda x: x[1] + x[2])
            else:
                current.sort(key=lambda x: x[1])
        elif self.sort_by == "points_desc":
            if self.active_tab == "both":
                current.sort(key=lambda x: x[1] + x[2], reverse=True)
            else:
                current.sort(key=lambda x: x[1], reverse=True)
        elif self.sort_by == "name_az":
            current.sort(key=lambda x: x[0].lower())
        elif self.sort_by == "logout_newest":
            # Most recently logged out first (newest timestamp first)
            if self.active_tab == "both":
                current.sort(key=lambda x: x[3], reverse=True)
            else:
                current.sort(key=lambda x: x[2], reverse=True)
        elif self.sort_by == "logout_oldest":
            # Longest offline first (oldest timestamp first)
            if self.active_tab == "both":
                current.sort(key=lambda x: x[3])
            else:
                current.sort(key=lambda x: x[2])
        elif self.sort_by == "absent_first":
            # Members with absent role first, then by points ascending
            if self.active_tab == "both":
                current.sort(key=lambda x: (not x[5], x[2]))
            else:
                current.sort(key=lambda x: (not x[4], x[1]))
        
        return current

    def _rebuild(self):
        """Rebuild the single Container based on the active tab and page."""
        self.clear_items()

        current = self._current_list()
        total_pages = max(1, -(-len(current) // self.ITEMS_PER_PAGE))  # ceil div
        start = self.page * self.ITEMS_PER_PAGE
        end = start + self.ITEMS_PER_PAGE
        page_items = current[start:end]

        tab_label = "Last Week" if self.active_tab == "last_week" else "This Week"

        inner_items = [
            TextDisplay(f"# Guild Members Below {self.point_threshold} Activity Points"),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(
                f"Members inactive last week: **{len(self.inactive_last_week)}**\n"
                f"Members inactive this week: **{len(self.inactive_this_week)}**\n"
                f"Members failed both weeks: **{len(self.inactive_both)}**"
            ),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(f"### Inactive {tab_label} (page {self.page + 1}/{total_pages}):"),
        ]

        if page_items:
            if self.active_tab == "both":
                for nickname, last_pts, this_pts, logout, number_id, has_absent_role in page_items:
                    inner_items.append(TextDisplay(f"• **{nickname} ({number_id})**: last week **{last_pts}** pts, this week **{this_pts}** pts, logout <t:{logout:.0f}:R>" + (" [Absent]" if has_absent_role else "")))
            else:
                for nickname, points, logout, number_id, has_absent_role in page_items:
                    inner_items.append(TextDisplay(f"• **{nickname} ({number_id})**: **{points}** points, last logout <t:{logout:.0f}:R>" + (" [Absent]" if has_absent_role else "")))
        else:
            inner_items.append(TextDisplay("*No inactive members to display.*"))

        # ── Tab buttons ──
        tab_row = ActionRow()
        last_week_btn = Button(
            style=discord.ButtonStyle.secondary if self.active_tab == "last_week" else discord.ButtonStyle.primary,
            label=f"Inactive Last Week ({len(self.inactive_last_week)})",
            custom_id="inactive_last_week",
            disabled=self.active_tab == "last_week",
        )
        last_week_btn.callback = self._handle_last_week
        tab_row.add_item(last_week_btn)

        this_week_btn = Button(
            style=discord.ButtonStyle.secondary if self.active_tab == "this_week" else discord.ButtonStyle.primary,
            label=f"Inactive This Week ({len(self.inactive_this_week)})",
            custom_id="inactive_this_week",
            disabled=self.active_tab == "this_week",
        )
        this_week_btn.callback = self._handle_this_week
        tab_row.add_item(this_week_btn)

        both_btn = Button(
            style=discord.ButtonStyle.danger if self.active_tab == "both" else discord.ButtonStyle.secondary,
            label=f"Failed Both ({len(self.inactive_both)})",
            custom_id="inactive_both",
            disabled=self.active_tab == "both",
        )
        both_btn.callback = self._handle_both
        tab_row.add_item(both_btn)
        inner_items.append(tab_row)

        # ── Sort Select menu ──
        sort_options = [
            discord.SelectOption(label="Points (Low → High)", value="points_asc", emoji="⬆️"),
            discord.SelectOption(label="Points (High → Low)", value="points_desc", emoji="⬇️"),
            discord.SelectOption(label="Nickname (A → Z)", value="name_az", emoji="🔤"),
            discord.SelectOption(label="Last Logout (Newest)", value="logout_newest", emoji="🆕"),
            discord.SelectOption(label="Last Logout (Oldest)", value="logout_oldest", emoji="⏰"),
            discord.SelectOption(label="Absent First", value="absent_first", emoji="🚫"),
        ]
        sort_row = ActionRow()
        sort_select = Select(
            placeholder="Sort by...",
            options=sort_options,
            custom_id="inactive_sort",
        )
        # Set the default to show the currently active sort
        current_label = next(
            (opt.label for opt in sort_options if opt.value == self.sort_by),
            "Sort by..."
        )
        sort_select.default_values = [discord.SelectOption(label=current_label, value=self.sort_by)]
        sort_select.callback = self._handle_sort
        sort_row.add_item(sort_select)
        inner_items.append(sort_row)

        # ── Pagination buttons ──
        nav_row = ActionRow()
        prev_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="⬅ Previous",
            custom_id="inactive_prev",
            disabled=self.page <= 0,
        )
        prev_btn.callback = self._handle_prev
        nav_row.add_item(prev_btn)

        next_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="Next ➡",
            custom_id="inactive_next",
            disabled=self.page >= total_pages - 1,
        )
        next_btn.callback = self._handle_next
        nav_row.add_item(next_btn)
        inner_items.append(nav_row)

        container = Container(*inner_items, accent_color=ORANGE)
        self.add_item(container)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True

    # ── button callbacks ─────────────────────────────────────────────

    async def _handle_last_week(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.active_tab = "last_week"
        self.page = 0
        self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_this_week(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.active_tab = "this_week"
        self.page = 0
        self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_both(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.active_tab = "both"
        self.page = 0
        self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        current = self._current_list()
        total_pages = max(1, -(-len(current) // self.ITEMS_PER_PAGE))
        if self.page < total_pages - 1:
            self.page += 1
            self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_sort(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected = interaction.data.get("values", ["points_asc"])
        self.sort_by = selected[0]
        self.page = 0
        self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _handle_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild()
        await interaction.edit_original_response(view=self)


# -----------------------------------------------------------------------------
# Ranking (Leaderboard) Views & Command
# -----------------------------------------------------------------------------

RANKING_ACCENT = 0xE67E22  # Orange

def _format_rank_time(score: float) -> str:
    """Format a negative time-based score (in minutes) as M:SS.sss.

    Example: -450.11907958984375 -> '7:30.119'
    """
    if score is None:
        return "N/A"
    total_seconds = abs(float(score))
    minutes = int(total_seconds // 60)
    seconds = total_seconds - (minutes * 60)
    return f"{minutes}:{seconds:06.3f}"


def _format_rank_points(score) -> str:
    """Format a points-based score with thousands separators."""
    if score is None:
        return "N/A"
    try:
        return f"{int(score):,}"
    except (ValueError, TypeError):
        return str(score)


def _rank_name_to_display(rank_name: str) -> str:
    """Convert a rank_name like 'rank_team10_dungeon_22' to a friendly label."""
    if rank_name.startswith("rank_team10_dungeon_"):
        return f"HR Dungeon {rank_name.split('_')[-1]}"
    if rank_name.startswith("rank_team_dungeon_"):
        return f"ST Dungeon {rank_name.split('_')[-1]}"
    if rank_name == "rank_petbattle_3v3":
        return "Cutie Clash 3v3"
    return rank_name


class RankingTypeSelectView(LayoutView):
    """Step 1: Let the user pick HR / ST / 3v3 Pet."""

    def __init__(self, cog, target_pid: Optional[str] = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.target_pid = target_pid

        header_text = "# 🏆 Ranking Lookup\nSelect a ranking type to view."
        if target_pid:
            header_text += "\n\n🎯 **Target queued:** will jump to their rank when found."
        inner_items = [
            TextDisplay(header_text),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        row = ActionRow()
        hr_btn = Button(
            label="HR (Hero's Realm)",
            style=discord.ButtonStyle.primary,
            emoji="⚔️",
            custom_id="ranking_type_hr",
        )
        hr_btn.callback = self._make_type_callback("hr")
        row.add_item(hr_btn)

        st_btn = Button(
            label="ST (Sword Trial)",
            style=discord.ButtonStyle.primary,
            emoji="⏱️",
            custom_id="ranking_type_st",
        )
        st_btn.callback = self._make_type_callback("st")
        row.add_item(st_btn)

        pet_btn = Button(
            label="Cutie Clash",
            style=discord.ButtonStyle.primary,
            emoji="🐾",
            custom_id="ranking_type_3v3_pet",
        )
        pet_btn.callback = self._make_type_callback("3v3_pet")
        row.add_item(pet_btn)
        inner_items.append(row)

        container = Container(*inner_items, accent_color=RANKING_ACCENT)
        self.add_item(container)

    def _make_type_callback(self, rank_type: str):
        async def callback(interaction: discord.Interaction):
            if rank_type == "3v3_pet":
                # No dungeon needed — go straight to results
                await interaction.response.defer()
                await self.cog._show_ranking_results(interaction, "3v3_pet", None, target_pid=self.target_pid)
            else:
                # Ask for dungeon ID via modal (must be sent via response, not followup)
                modal = DungeonIDModal(self.cog, rank_type, self.target_pid)
                await interaction.response.send_modal(modal)
        return callback

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class DungeonIDModal(discord.ui.Modal, title="Enter Dungeon ID"):
    """Step 2: Ask for the dungeon ID (HR / ST only)."""

    def __init__(self, cog, rank_type: str, target_pid: Optional[str] = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.rank_type = rank_type
        self.target_pid = target_pid

    dungeon_id = discord.ui.TextInput(
        label="Dungeon ID",
        placeholder="e.g. 22",
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        dungeon_id = self.dungeon_id.value.strip()
        if not dungeon_id.isdigit():
            await interaction.followup.send("❌ Dungeon ID must be a number.", ephemeral=True)
            return
        await self.cog._show_ranking_results(interaction, self.rank_type, int(dungeon_id), target_pid=self.target_pid)


class RankingResultsView(LayoutView):
    """Step 3: Display the leaderboard page with pagination."""

    ITEMS_PER_PAGE = 20

    def __init__(self, cog, rank_type: str, dungeon_id, rank_name: str, page: int = 1, target_pid: Optional[str] = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.rank_type = rank_type
        self.dungeon_id = dungeon_id
        self.rank_name = rank_name
        self.page = page
        self.target_pid = target_pid
        self.target_nickname = None
        self.total_pages = 1
        self.total_entries = 0
        self.my_rank = None
        self.my_score = None
        self.my_nickname = None
        self.rank_list = []
        self.last_place_score = None
        self.last_place_nickname = None

        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        display_name = _rank_name_to_display(self.rank_name)
        is_time_based = self.rank_type in ("hr", "st")

        # Header
        header_lines = [f"# 🏆 {display_name}"]
        if self.total_entries:
            header_lines.append(f"📊 **Total Entries:** {self.total_entries}")
        header_lines.append(f"📍 **Page {self.page}/{self.total_pages}**")
        header_text = "\n".join(header_lines)

        inner_items = [
            TextDisplay(header_text),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        # Rank list
        if self.rank_list:
            lines = []
            for idx, entry in enumerate(self.rank_list):
                rank_num = (self.page - 1) * self.ITEMS_PER_PAGE + idx + 1
                player_info = entry.get('player_info', {})
                base = {}
                if isinstance(player_info, dict):
                    # Format A: player_info is keyed by PID -> {pid: {base: {...}, head: {...}, id: ...}}
                    # Format B: player_info is directly {base: {...}, head: {...}, id: ...}
                    if 'base' in player_info and isinstance(player_info.get('base'), dict):
                        base = player_info['base']
                    else:
                        for pid, pdata in player_info.items():
                            if isinstance(pdata, dict) and isinstance(pdata.get('base'), dict):
                                base = pdata['base']
                                break
                nickname = base.get('nickname', 'Unknown')
                score = entry.get('score', 0)

                if rank_num == 1:
                    prefix = "🥇"
                elif rank_num == 2:
                    prefix = "🥈"
                elif rank_num == 3:
                    prefix = "🥉"
                else:
                    prefix = f"{rank_num}."

                if is_time_based:
                    score_str = _format_rank_time(score)
                else:
                    score_str = _format_rank_points(score)

                lines.append(f"{prefix} **{nickname}** — {score_str}")

            inner_items.append(TextDisplay("\n".join(lines)))
        else:
            inner_items.append(TextDisplay("*No entries on this page.*"))

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # My rank section — shows the viewing player's rank (or target's if checking someone)
        if self.my_rank is not None and self.my_rank >= 0:
            my_rank_display = self.my_rank + 1
            if is_time_based:
                my_score_str = _format_rank_time(self.my_score)
            else:
                my_score_str = _format_rank_points(self.my_score)
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
            else:
                inner_items.append(TextDisplay("🎯 **You are not on this leaderboard.**"))

        # Last place / target info
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

        # Team/Player select dropdown
        self.page_entries = []
        if self.rank_list:
            options = []
            for idx, entry in enumerate(self.rank_list):
                rank_num = (self.page - 1) * self.ITEMS_PER_PAGE + idx + 1
                player_info = entry.get('player_info', {})
                base = {}
                if isinstance(player_info, dict):
                    if 'base' in player_info and isinstance(player_info.get('base'), dict):
                        base = player_info['base']
                    else:
                        for pid, pdata in player_info.items():
                            if isinstance(pdata, dict) and isinstance(pdata.get('base'), dict):
                                base = pdata['base']
                                break
                nickname = base.get('nickname', 'Unknown')
                self.page_entries.append({
                    'rank': rank_num,
                    'nickname': nickname,
                    'entry': entry,
                })
                label = f"{rank_num}. {nickname}"
                options.append(discord.SelectOption(label=label[:100], value=str(idx)))

            if options:
                select_placeholder = "👥 View Team" if self.rank_type in ("hr", "st") else "👤 View Player"
                select_row = ActionRow()
                view_select = Select(
                    placeholder=select_placeholder,
                    options=options[:25],
                    custom_id="ranking_view_entry",
                )
                view_select.callback = self._handle_view_entry
                select_row.add_item(view_select)
                inner_items.append(select_row)

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Pagination row
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

        # Back button
        back_row = ActionRow()
        back_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="🔙 Change Type",
            custom_id="ranking_back",
        )
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner_items.append(back_row)

        container = Container(*inner_items, accent_color=RANKING_ACCENT)
        self.add_item(container)

    async def _handle_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.page > 1:
            await self.cog._show_ranking_results(interaction, self.rank_type, self.dungeon_id, page=self.page - 1, target_pid=self.target_pid)

    async def _handle_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.page < self.total_pages:
            await self.cog._show_ranking_results(interaction, self.rank_type, self.dungeon_id, page=self.page + 1, target_pid=self.target_pid)

    async def _handle_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = RankingTypeSelectView(self.cog)
        await interaction.edit_original_response(content=None, embed=None, view=view)

    async def _handle_view_entry(self, interaction: discord.Interaction):
        """Handle selecting a team (HR/ST) or player (3v3 Pet) from the dropdown."""
        await interaction.response.defer(ephemeral=True)
        selected = interaction.data.get("values", [""])[0]
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
            # Team view
            ud = entry.get('ud', {})
            members = ud.get('members', [])
            leader_id = ud.get('leader_id')
            hostnum2pids = {}
            for pid in members:
                hostnum = ud.get(pid, {}).get('hostnum', 10595)
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
                    pdata = players.get(pid, {})
                    base = pdata.get('base', {})
                    school_id = base.get('school', 0)
                    school_name = SCHOOL_NAMES.get(school_id) if school_id in SCHOOL_NAMES else None
                    member_list.append({
                        'pid': pid,
                        'hostnum': ud.get(pid, {}).get('hostnum', 10595),
                        'nickname': base.get('nickname', 'Unknown'),
                        'level': base.get('level', 0),
                        'number_id': str(base.get('number_id', '')),
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
            )
            await interaction.edit_original_response(content=None, embed=None, view=team_view)
        else:
            # 3v3 Pet — view player profile
            player_info = entry.get('player_info', {})
            base = {}
            if isinstance(player_info, dict):
                if 'base' in player_info and isinstance(player_info.get('base'), dict):
                    base = player_info['base']
                else:
                    for pid, pdata in player_info.items():
                        if isinstance(pdata, dict) and isinstance(pdata.get('base'), dict):
                            base = pdata['base']
                            break
            number_id = str(base.get('number_id', ''))
            nickname = base.get('nickname', 'Unknown')
            if not number_id:
                await interaction.followup.send(f"❌ No Number ID available for {nickname}.", ephemeral=True)
                return
            try:
                view, files = await self.cog._build_player_profile_view(number_id, interaction, ephemeral=True)
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
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class TeamDetailView(LayoutView):
    """Components V2 LayoutView showing a team's members (HR=10, ST=5) with per-member profile buttons."""

    def __init__(self, cog, rank_type: str, rank_name: str, rank_num: int, score, members: list, back_view):
        super().__init__(timeout=180)
        self.cog = cog
        self.rank_type = rank_type
        self.rank_name = rank_name
        self.rank_num = rank_num
        self.score = score
        self.members = members  # list of dicts: {pid, hostnum, nickname, level, number_id, is_online, school_name}
        self.back_view = back_view

        is_time_based = self.rank_type in ("hr", "st")
        score_str = _format_rank_time(score) if is_time_based else _format_rank_points(score)

        inner_items = [
            TextDisplay(f"# 👥 Team #{self.rank_num} — {score_str}\n\n**Members:** {len(members)}"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        # Member rows with profile buttons
        for idx, m in enumerate(members):
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
                section = Section(TextDisplay(member_text), accessory=btn)
                inner_items.append(section)
            else:
                inner_items.append(TextDisplay(member_text))

        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Back button
        back_row = ActionRow()
        back_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="🔙 Back to Leaderboard",
            custom_id="team_back",
        )
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner_items.append(back_row)

        container = Container(*inner_items, accent_color=RANKING_ACCENT)
        self.add_item(container)

    def _make_profile_callback(self, member: dict):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                view, files = await self.cog._build_player_profile_view(member['number_id'], interaction, ephemeral=True)
                if not view:
                    await interaction.followup.send(f"❌ Could not load profile for {member['nickname']}", ephemeral=True)
                    return
                view._original_message = None
                await interaction.followup.send(view=view, files=files, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to show team member profile: {e}", exc_info=True)
                await interaction.followup.send(f"❌ Failed to load profile: `{str(e)}`", ephemeral=True)
        return callback

    async def _handle_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(content=None, embed=None, view=self.back_view)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class WWMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_guild_state = None
        self.monitor_channel = None
        self.monitor_enabled = False
        self.check_interval_minutes = 2
        self.monitor_message = None
        self.db_path = BASE_DIR / "data" / "guild_monitor.db"
        self.last_known_applications = {}
        self.pending_apps_channel_id = 1443104374837608529
        self.online_players_button_presses = 0

    player_group = app_commands.Group(
        name="player",
        description="WWM Player search commands"
    )
    
    guild_group = app_commands.Group(
        name="guild",
        description="Guild monitoring commands"
    )
    
    sect_group = app_commands.Group(
        name="sect",
        description="Sect-related commands"
    )

    ranking_group = app_commands.Group(
        name="ranking",
        description="View WWM leaderboards (HR, ST, Cutie Clash 3v3)"
    )

    @ranking_group.command(name="view", description="View a leaderboard by type (HR/ST/Cutie Clash 3v3).")
    @app_commands.describe(identifier="Optional: Player's 10-digit Number ID or in-game nickname to check their rank")
    async def ranking_view(self, interaction: discord.Interaction, identifier: Optional[str] = None):
        """Open the ranking type selector. If identifier is provided, jump to that player's rank."""
        target_pid = None
        if identifier and identifier.strip():
            pid, _, _ = await self._resolve_player_identifier(identifier.strip())
            if not pid:
                embed = discord.Embed(
                    title="❌ Player Not Found",
                    description=f"No player found matching '{identifier}'. Try a 10-digit Number ID or exact nickname.",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            target_pid = pid

        view = RankingTypeSelectView(self, target_pid=target_pid)
        await interaction.response.send_message(content=None, embed=None, view=view)

    async def _resolve_user_pid(self, interaction: discord.Interaction) -> Optional[str]:
        """Resolve the calling user's WWM player PID from the verified_members table."""
        try:
            async with aiosqlite.connect(DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT player_pid FROM verified_members WHERE user_id = ?",
                    (interaction.user.id,)
                )
                row = await cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.warning(f"Failed to resolve user PID for {interaction.user.id}: {e}")
            return None

    async def _show_ranking_results(self, interaction: discord.Interaction, rank_type: str, dungeon_id, page: int = 1, target_pid: Optional[str] = None):
        """Fetch and display leaderboard results for the given type/dungeon."""
        # Build the rank_name based on type
        if rank_type == "hr":
            if dungeon_id is None:
                await interaction.followup.send("❌ Dungeon ID is required for HR rankings.", ephemeral=True)
                return
            rank_name = f"rank_team10_dungeon_{dungeon_id}"
        elif rank_type == "st":
            if dungeon_id is None:
                await interaction.followup.send("❌ Dungeon ID is required for ST rankings.", ephemeral=True)
                return
            rank_name = f"rank_team_dungeon_{dungeon_id}"
        elif rank_type == "3v3_pet":
            rank_name = "rank_petbattle_3v3"
        else:
            await interaction.followup.send("❌ Invalid ranking type.", ephemeral=True)
            return


        try:
            # Check if the user is bound and get their PID
            user_pid = await self._resolve_user_pid(interaction)

            # If a target_pid was provided, first fetch page 1 to get my_rank / my_data
            # so we can jump to the target's page.
            if target_pid:
                probe_response = await get_rank_list(rank_name, page=1, pid=target_pid)
                if probe_response and probe_response.get('code') == 0:
                    probe_result = probe_response.get('result', {})
                    probe_rank = probe_result.get('my_rank', -1)
                    if probe_rank >= 0:
                        target_page = probe_rank // 20 + 1
                        if page == 1 and target_page > 1:
                            page = target_page

            # When checking a specific target, use their PID so my_rank / my_data
            # reflect the target's position (not the calling user's).
            fetch_pid = target_pid if target_pid else user_pid
            response = await get_rank_list(rank_name, page=page, pid=fetch_pid)

            if not response or response.get('code') != 0:
                await interaction.followup.send("❌ Failed to fetch leaderboard data. This dungeon may not exist or the API returned an error.", ephemeral=True)
                return

            result = response.get('result', {})
            rank_list = result.get('rank_list', [])
            total_entries = result.get('rank_total_len', 0)
            my_data = result.get('my_data', {}) or {}
            my_rank = result.get('my_rank', -1)  # -1 = not on leaderboard

            if not rank_list and total_entries == 0:
                await interaction.followup.send("❌ No data found for this ranking. The dungeon ID may be incorrect.", ephemeral=True)
                return

            total_pages = max(1, (total_entries + 19) // 20)  # ceil division
            if page > total_pages:
                page = total_pages

            # Extract user's own score from my_data
            my_score = my_data.get('score') if my_data else None

            # Find last place (from the last page)
            last_place_score = None
            last_place_nickname = None

            def _extract_nickname(player_info) -> str:
                """Extract nickname from player_info, handling both formats."""
                if not isinstance(player_info, dict):
                    return 'Unknown'
                # Format B: player_info is directly {base: {...}, head: {...}, id: ...}
                if 'base' in player_info and isinstance(player_info.get('base'), dict):
                    return player_info['base'].get('nickname', 'Unknown')
                # Format A: player_info is keyed by PID -> {pid: {base: {...}, ...}}
                for pdata in player_info.values():
                    if isinstance(pdata, dict) and isinstance(pdata.get('base'), dict):
                        return pdata['base'].get('nickname', 'Unknown')
                return 'Unknown'

            if total_entries > 0:
                last_page = total_pages
                if last_page == page:
                    # We're already on the last page — use the last entry on this page
                    if rank_list:
                        last_entry = rank_list[-1]
                        last_place_score = last_entry.get('score')
                        last_place_nickname = _extract_nickname(last_entry.get('player_info', {}))
                else:
                    # Fetch the last page to get the last place
                    try:
                        last_response = await get_rank_list(rank_name, page=last_page, pid=user_pid)
                        if last_response and last_response.get('code') == 0:
                            last_result = last_response.get('result', {})
                            last_page_list = last_result.get('rank_list', [])
                            if last_page_list:
                                last_entry = last_page_list[-1]
                                last_place_score = last_entry.get('score')
                                last_place_nickname = _extract_nickname(last_entry.get('player_info', {}))
                    except Exception as e:
                        logger.warning(f"Failed to fetch last place data: {e}")

            # When checking a target player, extract their nickname for display labels
            target_nickname = None
            if target_pid and my_data:
                target_nickname = _extract_nickname(my_data.get('player_info', {}))

            # Build the view
            view = RankingResultsView(
                cog=self,
                rank_type=rank_type,
                dungeon_id=dungeon_id,
                rank_name=rank_name,
                page=page,
                target_pid=target_pid,
            )
            view.total_pages = total_pages
            view.total_entries = total_entries
            view.my_rank = my_rank if my_rank >= 0 else None
            view.my_score = my_score
            view.rank_list = rank_list
            view.last_place_score = last_place_score
            view.last_place_nickname = last_place_nickname
            view.target_nickname = target_nickname
            view._rebuild()

            await interaction.edit_original_response(content=None, embed=None, view=view)

        except Exception as e:
            logger.error(f"Ranking results failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to load ranking data: `{str(e)}`", ephemeral=True)

    @sect_group.command(name="election", description="View the top election candidates for a sect")
    @app_commands.describe(sect_name="The name of the sect to check election rankings for", count="How many candidates to show (default 15, max 20)")
    @app_commands.choices(sect_name=[
        app_commands.Choice(name=name, value=str(sid))
        for sid, name in sorted(SCHOOL_NAMES.items()) if sid != 100
    ])
    async def sect_election(self, interaction: discord.Interaction, sect_name: app_commands.Choice[str], count: int = 15):
        """Fetch and display the top election candidates for the chosen sect."""
        school_id = int(sect_name.value)
        # Clamp count between 1 and 20
        count = max(1, min(20, count))

        await interaction.response.send_message(f"🗳️ Fetching top **{count}** election candidates for **{SCHOOL_NAMES[school_id]}**...")

        try:
            response = await get_sect_election_ranking(school_id, limit=count)
            if not response or response.get('code') != 0:
                embed = discord.Embed(
                    title="❌ API Error",
                    description="Failed to fetch election ranking data.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(content=None, embed=embed)
                return

            result = response.get('result', {})
            rank_list = result.get('rank_list', [])

            if not rank_list:
                embed = discord.Embed(
                    title=f"{SCHOOL_EMOTES.get(school_id, '')} {SCHOOL_NAMES[school_id]} Election",
                    description="No election data found for this sect. There may be no ongoing election.",
                    color=discord.Color.orange()
                )
                await interaction.edit_original_response(content=None, embed=embed)
                return

            sect_emoji = SCHOOL_EMOTES.get(school_id, "")
            embed = discord.Embed(
                title=f"{sect_emoji} {SCHOOL_NAMES[school_id]} — Top Election Candidates",
                color=0x9B59B6,
            )

            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            for idx, entry in enumerate(rank_list):
                player_info = entry.get('player_info', {})
                base = player_info.get('base', {})
                head = player_info.get('head', {})

                nickname = base.get('nickname', 'Unknown')
                level = base.get('level', '?')
                number_id = base.get('number_id', 'N/A')
                score = entry.get('score', 0)
                pid = entry.get('pid') or player_info.get('id')

                rank_medal = medals[idx] if idx < len(medals) else f"{idx+1}."

                # Build value string
                value_parts = [f"Lv.{level}  |  Votes: **{score:,}**  |  ID: {number_id}"]
                value_str = "  |  ".join(value_parts)
                embed.add_field(
                    name=f"{rank_medal} {nickname}",
                    value=value_str,
                    inline=False
                )

                # Add a separator after the 10th entry to mark the cutoff
                if idx == 9:
                    embed.add_field(name="━" * 30, value="*Entries beyond top 10*", inline=False)

            total_candidates = result.get('rank_total_len', len(rank_list))
            embed.set_footer(text=f"Total candidates: {total_candidates}")

            await interaction.edit_original_response(content=None, embed=embed)

        except Exception as e:
            logger.error(f"Sect election command failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to fetch election data: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(content=None, embed=embed)

    async def _init_database(self):
        (BASE_DIR / "data").mkdir(exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS monitor_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_player_counts (
                    ts INTEGER PRIMARY KEY,
                    total_members INTEGER NOT NULL,
                    online_count INTEGER NOT NULL,
                    guild_week_fame INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_player_snapshots (
                    ts INTEGER PRIMARY KEY,
                    snapshot_json TEXT NOT NULL
                )
            """)
            await db.commit()
    
    async def _load_config(self):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT key, value FROM monitor_config")
            rows = await cursor.fetchall()
            config = {row[0]: row[1] for row in rows}
            
            if 'channel_id' in config:
                self.monitor_channel = self.bot.get_channel(int(config['channel_id']))
            if 'message_id' in config and self.monitor_channel:
                try:
                    self.monitor_message = await self.monitor_channel.fetch_message(int(config['message_id']))
                except:
                    self.monitor_message = None
            if 'enabled' in config:
                self.monitor_enabled = config['enabled'] == 'true'
            if 'interval' in config:
                self.check_interval_minutes = int(config['interval'])
            if 'press_count' in config:
                self.online_players_button_presses = int(config['press_count'])
            else:
                self.online_players_button_presses = 0
    
    async def _save_config(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO monitor_config VALUES ('channel_id', ?)", (str(self.monitor_channel.id) if self.monitor_channel else None,))
            await db.execute("REPLACE INTO monitor_config VALUES ('message_id', ?)", (str(self.monitor_message.id) if self.monitor_message else None,))
            await db.execute("REPLACE INTO monitor_config VALUES ('enabled', ?)", ('true' if self.monitor_enabled else 'false',))
            await db.execute("REPLACE INTO monitor_config VALUES ('interval', ?)", (str(self.check_interval_minutes),))
            await db.execute("REPLACE INTO monitor_config VALUES ('press_count', ?)", (str(self.online_players_button_presses),))
            await db.commit()
    
    async def cog_load(self):
        await self._init_database()
        await self._load_config()
        # Initialize the affix mapping database
        await init_db()
        if self.monitor_enabled and self.monitor_channel:
            self.guild_monitor_task.start()
        # Always-on opponent-guild reminder (8 AM GMT+8 Sunday + Monday).
        if not self.gvg_league_notice_task.is_running():
            self.gvg_league_notice_task.start()

    async def cog_unload(self):
        if self.guild_monitor_task.is_running():
            self.guild_monitor_task.cancel()
        if self.gvg_league_notice_task.is_running():
            self.gvg_league_notice_task.cancel()

    async def _resolve_player_identifier(self, identifier: str) -> tuple:
        """
        Resolve a player identifier (number ID or nickname) to PID and hostnum.
        Smart routing: if exactly 10 digits → number ID API, else → nickname API.
        Returns (pid, hostnum, player_data_dict) or (None, None, None).
        """
        t0 = time.time()
        # Smart routing based on format
        if identifier.isdigit() and len(identifier) == 10:
            # Exactly 10 digits → treat as Number ID
            player_data = await get_player_info(identifier, fields=["base"], force_search=True)
            t1 = time.time()
            logger.debug(f"[timing] resolve_number_id_search: {t1 - t0:.3f}s")
            if player_data and player_data.get('result') and player_data['result'].get('id'):
                result = player_data['result']
                pid = result.get('id')
                hostnum = result.get('hostnum', 10595)
                logger.debug(f"Resolved identifier '{identifier}' to PID {pid} via number_id")
                return pid, hostnum, result

        # Otherwise → treat as nickname
        nickname_data = await find_people_by_nickname(identifier, force_search=True)
        t1 = time.time()
        logger.debug(f"[timing] resolve_nickname_search: {t1 - t0:.3f}s")
        if nickname_data and nickname_data.get('result'):
            result = nickname_data['result']
            pid = result.get('id')
            hostnum = result.get('hostnum', 10595)
            logger.debug(f"Resolved identifier '{identifier}' to PID {pid} via nickname")
            return pid, hostnum, result

        t1 = time.time()
        logger.debug(f"[timing] resolve_total_failed: {t1 - t0:.3f}s")
        logger.warning(f"Could not resolve identifier '{identifier}'")
        return None, None, None

    async def _fetch_player_profile_data(self, player_pid: str, player_hostnum: int, interaction: discord.Interaction = None) -> dict:
        """
        Fetch all player data and build a complete profile data dict.
        Returns dict with all fields needed for PlayerProfileView.
        
        The 'is_verified' flag refers to whether the VIEWED player has a bound Discord account,
        which controls Discord mention display. Access to stats is controlled by the command
        user's verification status in the calling command.
        """
        t0 = time.time()
        # Fetch full player data
        raw_data = await fetch_player_data_by_pid(player_pid, hostnum=player_hostnum)
        t1 = time.time()
        logger.debug(f"[timing] fetch_player_data_by_pid: {t1 - t0:.3f}s")
        if not raw_data:
            raise ValueError("Failed to fetch player data from API")

        data = raw_data.get('result', raw_data) if isinstance(raw_data, dict) else raw_data
        base_data = data.get('base', {})
        if not base_data and 'nickname' in data:
            base_data = data

        player_nickname = base_data.get('nickname', 'Unknown')
        player_number_id = base_data.get('number_id', 'N/A')
        ly_stage_name = base_data.get('ly_stage_name', '')
        lv = base_data.get('level', 0)
        is_invisible = base_data.get('invisible', False)
        is_online = base_data.get('is_online', 0) == 1
        oversea_tag = base_data.get('oversea_tag', 'N/A')
        online_hours = round(base_data.get('online_time', 0) / 3600, 1)
        create_time = base_data.get('create_time', 0)
        school_id = base_data.get('school', 0)
        body_type = base_data.get('body_type')
        homeworld_data = data.get('homeworld_data', {})
        achievement_data = data.get('achievement', {})
        club_data = data.get('club', {})
        fashion_data = data.get('fashion', {})

        # Check if the VIEWED player (not the command user) has a verified Discord account
        # This controls Discord mention display and fashion cover image availability
        viewed_player_verified = False
        discord_user_id = None
        t_db1 = time.time()
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT user_id FROM verified_members WHERE player_pid = ?", (player_pid,))
            row = await cursor.fetchone()
            viewed_player_verified = row is not None
            discord_user_id = row[0] if row else None
        logger.debug(f"[timing] db_lookup_viewed_player_verified: {time.time() - t_db1:.3f}s")
        
        # Check if the COMMAND USER (not the viewed player) is verified
        # This controls what stats the command user can see
        command_user_verified = False
        t_db2 = time.time()
        if interaction:
            async with aiosqlite.connect(DB_PATH) as conn2:
                cursor2 = await conn2.execute("SELECT user_id FROM verified_members WHERE user_id = ?", (interaction.user.id,))
                row2 = await cursor2.fetchone()
                command_user_verified = row2 is not None
        logger.debug(f"[timing] db_lookup_command_user_verified: {time.time() - t_db2:.3f}s")
        
        # --- Parallelize remaining independent fetches ---
        async def _fetch_likes():
            t0 = time.time()
            likes_count = 0
            likes_data_raw = {}
            try:
                likes_data = await get_topics_likes(target_uuid=player_pid, target_hostnum=player_hostnum)
                if likes_data and 'result' in likes_data:
                    likes_data_res = likes_data['result']
                    likes_count = sum(topic.get('n_likes', 0) for topic in likes_data_res.values())
                    likes_data_raw = likes_data_res
            except Exception:
                pass
            logger.debug(f"[timing] get_topics_likes: {time.time() - t0:.3f}s")
            return likes_count, likes_data_raw

        async def _fetch_like_history():
            t0 = time.time()
            likes_history = []
            try:
                # Get like history (returns list of PIDs who liked us)
                history_data = await get_like_history(str(player_pid), player_hostnum)
                if history_data and 'result' in history_data:
                    history = history_data['result'].get('history', [])
                    
                    # Extract unique PIDs and their hostnums
                    pid_hostnums = {}
                    for entry in history:
                        from_id = entry.get('fromid')
                        from_hostnum = entry.get('fromhostnum')
                        if from_id and from_hostnum:
                            pid_hostnums[from_id] = from_hostnum
                    
                    if pid_hostnums:
                        # Batch fetch player info for all unique PIDs
                        # Group by hostnum for efficient API call
                        hostnum_groups = defaultdict(list)
                        for pid, hostnum in pid_hostnums.items():
                            hostnum_groups[hostnum].append(pid)
                        
                        # Fetch player data in parallel batches
                        all_players = {}
                        fetch_tasks = []
                        for hnum, pids in hostnum_groups.items():
                            fetch_tasks.append(get_bulk_players_info(pids, fields=["base"], hostnum=hnum))
                        
                        batch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
                        
                        for result in batch_results:
                            if isinstance(result, dict) and result.get('code') == 0:
                                all_players.update(result.get('result', {}))
                        
                        # Normalize data
                        for entry in history:
                            from_id = entry.get('fromid', '')
                            timestamp = entry.get('ts', 0)
                            topic_id = entry.get('topic_id', 0)
                            
                            player_info = all_players.get(from_id, {})
                            base_info = player_info.get('base', {})
                            
                            likes_history.append({
                                'nickname': base_info.get('nickname', 'Unknown'),
                                'number_id': base_info.get('number_id', 'N/A'),
                                'level': base_info.get('level', 0),
                                'oversea_tag': base_info.get('oversea_tag', ''),
                                'topic_id': topic_id,
                                'timestamp': timestamp
                            })
                        
                        # Sort by timestamp (most recent first)
                        likes_history.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                        likes_history = likes_history[:30]  # Limit to 30 recent likes
            except Exception as e:
                logger.warning(f"Failed to get like history: {e}")
            logger.debug(f"[timing] get_like_history: {time.time() - t0:.3f}s")
            return likes_history

        async def _fetch_fashion_score():
            t0 = time.time()
            fashion_score = fashion_data.get('score', 0)
            logger.debug(f"[timing] get_fashion_score: {time.time() - t0:.3f}s")
            return fashion_score

        async def _fetch_homeland():
            t0 = time.time()
            homeland_info = None
            home_info = homeworld_data.get('home_info', {}) if isinstance(homeworld_data, dict) else {}
            home_id = next(iter(home_info.keys()), None)
            try:
                homeland_data = await get_homeland_info(hostnum2pids={player_hostnum: [home_id]})
                if homeland_data and 'result' in homeland_data:
                    result = homeland_data['result']
                    if home_id in result:
                        homeland_info = result[home_id]
                    elif isinstance(result, dict) and 'homeland_base' in result:
                        homeland_info = result
            except Exception as homeland_err:
                logger.warning(f"Failed to get homeland info: {homeland_err}")
            logger.debug(f"[timing] get_homeland_info: {time.time() - t0:.3f}s")
            return homeland_info

        async def _fetch_cover_image():
            t0 = time.time()
            cover_img = None
            cover_img_path = None
            if viewed_player_verified or command_user_verified:
                try:
                    fashion_data = await get_fashion_plan(player_pid, hostnum=player_hostnum)
                    logger.debug(f"[timing] get_fashion_plan: {time.time() - t0:.3f}s")
                    if fashion_data and fashion_data.get('code') == 0 and 'result' in fashion_data:
                        cover_img = fashion_data['result'].get('cover_img')
                        if cover_img:
                            try:
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(cover_img, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                                        if resp.status == 200:
                                            data_bytes = await resp.read()
                                            suffix = ".png"
                                            ct = resp.headers.get('Content-Type', '')
                                            if 'webp' in ct:
                                                suffix = ".webp"
                                            elif 'jpg' in ct or 'jpeg' in ct:
                                                suffix = ".jpg"
                                            temp_dir = BASE_DIR / "data" / "temp"
                                            temp_dir.mkdir(parents=True, exist_ok=True)
                                            fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="wwm_cover_", dir=str(temp_dir))
                                            with os.fdopen(fd, 'wb') as f:
                                                f.write(data_bytes)
                                            cover_img_path = tmp_path
                            except Exception as dl_err:
                                logger.warning(f"Failed to download cover image for {player_pid}: {dl_err}")
                except Exception as fashion_err:
                    logger.warning(f"Failed to get fashion cover image: {str(fashion_err)}")
            logger.debug(f"[timing] cover_image_total: {time.time() - t0:.3f}s")
            return cover_img, cover_img_path

        likes_result, fashion_score, homeland_info, cover_result, likes_history = await asyncio.gather(
            _fetch_likes(),
            _fetch_fashion_score(),
            _fetch_homeland(),
            _fetch_cover_image(),
            _fetch_like_history(),
        )
        likes_count, likes_data_raw = likes_result
        cover_img, cover_img_path = cover_result

        # Sect info
        school_emoji = ""
        school_name = None
        school_rank = None
        if school_id in SCHOOL_NAMES:
            school_emoji = SCHOOL_EMOTES.get(school_id, "")
            school_name = SCHOOL_NAMES[school_id]
            school_data = data.get('school', {})
            if isinstance(school_data, dict):
                school_status = school_data.get('status', 0)
                if school_status in SCHOOL_RANKING:
                    school_rank = SCHOOL_RANKING[school_status]

        # Masteries - always fetch (public stats, no verification gating)
        attr = data.get('attr', {})
        martial_mastery = round(attr.get('XIUWEI_KUNGFU', 0), 1)
        scholar_mastery = round(attr.get('XIUWEI_TRADE3', 0), 1)
        healer_mastery = round(attr.get('XIUWEI_TRADE4', 0), 1)
        explore_mastery = round(attr.get('XIUWEI_EXPLORE', 0), 1)

        # Attributes - ALWAYS fetch full stats
        attr_str = round(attr.get('STR', 0), 1)
        attr_con = round(attr.get('CON', 0), 1)
        attr_bas = round(attr.get('BAS', 0), 1)
        attr_cri = round(attr.get('CRI', 0), 1)
        attr_agi = round(attr.get('AGI', 0), 1)

        # Combat
        arena_1v1_rank = None
        arena_1v1_max_winning_streak = 0
        arena_1v1_total_num = 0
        arena_3v3_rank = None
        arena_3v3_total_num = 0
        group_strategy = 0
        group_strategy_total_num = 0
        assist_points = 0

        lunjian = data.get('lunjian', {})
        if lunjian and 'grade' in lunjian:
            arena_1v1_rank = PlayerProfileView._format_rank(lunjian['grade'], lunjian.get('small_grade', 0))
            arena_1v1_max_winning_streak = lunjian.get('max_winning_streak', 0)
            arena_1v1_total_num = lunjian.get('total_num', 0)
        lunjian3v3 = data.get('lunjian3v3_prop', {})
        if lunjian3v3 and 'grade' in lunjian3v3:
            arena_3v3_rank = PlayerProfileView._format_rank(lunjian3v3['grade'], lunjian3v3.get('small_grade', 0))
            arena_3v3_total_num = lunjian3v3.get('total_num', 0)
        fight_shoulder = data.get('fight_shoulder', {})
        if fight_shoulder and 'score' in fight_shoulder:
            group_strategy = fight_shoulder['score']
            group_strategy_total_num = fight_shoulder.get('total_num', 0)
        coop_score = data.get('coop_score', {})
        if coop_score and 'score' in coop_score:
            assist_points = coop_score['score']
        gameplay = data.get('gameplay_trail', {})

        # Kongfu data
        kongfu_main = None
        kongfu_sub = None
        kongfu_role = None
        try:
            from utility.api_constants import KONGFU_WEAPON_MAP
            kongfu_data = data.get('kongfu', {})
            if kongfu_data:
                main_id = kongfu_data.get('kongfu_main')
                sub_id = kongfu_data.get('kongfu_sub')
                if main_id:
                    kongfu_main = KONGFU_WEAPON_MAP.get(main_id, f"Unknown ({main_id})").get("name")
                    kongfu_main += " " + KONGFU_WEAPON_MAP.get(main_id, f"Unknown ({main_id})").get("emoji", "")
                if sub_id:
                    kongfu_sub = KONGFU_WEAPON_MAP.get(sub_id, f"Unknown ({sub_id})").get("name")
                    kongfu_sub += " " + KONGFU_WEAPON_MAP.get(sub_id, f"Unknown ({sub_id})").get("emoji", "")
                weapon_ids = get_kongfu_ids_from_player(data)
                if weapon_ids:
                    kongfu_role = classify_kongfu_role(weapon_ids) if weapon_ids else ""
        except Exception as kongfu_err:
            logger.warning(f"Failed to parse kongfu data: {kongfu_err}")

        # Guild info - always fetch (public game data)
        guild_name = None
        is_our_guild = False
        guild_level = 0
        guild_leader = None
        guild_vice_leader = None
        guild_members = 0
        guild_funds = 0
        guild_fame = 0
        guild_announcement = None
        
        t_guild = time.time()
        if club_data:
            try:
                player_club_id = club_data.get('club_id')
                club_hostnum = club_data.get('hostnum', 10103)
                
                if player_club_id:
                    guild_full_data = await get_full_guild_info(player_club_id, hostnum=club_hostnum)
                    logger.debug(f"[timing] get_full_guild_info: {time.time() - t_guild:.3f}s")
                    
                    if guild_full_data:
                        guild_result = guild_full_data.get('result', {})
                        guild_base = guild_result.get('base', {})
                        guild_name = guild_base.get('name', 'Unknown Guild')
                        guild_level = guild_base.get('level', 0)
                        guild_members = guild_base.get('member_num', 0)
                        guild_funds = guild_base.get('fund', 0)
                        guild_fame = guild_base.get('fame', 0)
                        guild_announcement = guild_result.get('gonggao_info', {}).get('msg', None)
                        
                        # Get leadership
                        member_list = guild_result.get('members', {}).get('members', {})
                        leader_pid = None
                        vice_leader_pid = None
                        for pid, member in member_list.items():
                            post_list = member.get('post', [])
                            if 1 in post_list:
                                leader_pid = pid
                            if 2 in post_list:
                                vice_leader_pid = pid
                        
                        pids_to_fetch = []
                        if leader_pid:
                            pids_to_fetch.append(leader_pid)
                        if vice_leader_pid:
                            pids_to_fetch.append(vice_leader_pid)
                        
                        if pids_to_fetch:
                            t_lead = time.time()
                            bulk_lookup = await get_bulk_players_info(pids_to_fetch, fields=["base"])
                            logger.debug(f"[timing] get_bulk_players_info_leadership: {time.time() - t_lead:.3f}s")
                            if bulk_lookup and bulk_lookup.get('code') == 0:
                                players = bulk_lookup.get('result', {})
                                if leader_pid and leader_pid in players:
                                    guild_leader = players[leader_pid].get('base', {}).get('nickname', 'Unknown')
                                if vice_leader_pid and vice_leader_pid in players:
                                    guild_vice_leader = players[vice_leader_pid].get('base', {}).get('nickname', 'Unknown')
                
                if player_club_id == CLUB_ID:
                    is_our_guild = True
                
            except Exception as club_err:
                logger.warning(f"Failed to get club info: {str(club_err)}")
        logger.debug(f"[timing] fetch_guild_info_total: {time.time() - t_guild:.3f}s")

        # Birthday - always fetch (public)
        birthday_str = None
        jieyi_name = None
        jieyi_text = None
        birthday_data = data.get('birthday', {})
        if birthday_data and isinstance(birthday_data, dict):
            visible_flag = birthday_data.get('visible', 0)
            if visible_flag == 0:
                month = birthday_data.get('month', 0)
                day = birthday_data.get('day', 0)
                if month > 0 and day > 0:
                    birthday_str = _format_birthday(month, day)
        
        jieyi = data.get('jieyi', {})
        jieyi_name = jieyi.get('jieyi_name')
        jieyi_text = jieyi.get('jieyi_text')

        # Head avatar
        head_avatar_path = None
        head_id_value = None
        body_type_val = None
        try:
            head_data = data.get('head', {}) if isinstance(data, dict) else {}
            if isinstance(head_data, dict):
                head_id_value = head_data.get('head')
                if head_id_value:
                    raw_bt = base_data.get('body_type') if isinstance(base_data, dict) else None
                    body_type_val = raw_bt if raw_bt in (0, 1) else None
                    candidates = []
                    if body_type_val in (0, 1):
                        from utility.avatar_paths import (
                            AVATARS_MAPPED_LOOKUP_ORDER_BY_BODY_TYPE,
                        )
                        for d in AVATARS_MAPPED_LOOKUP_ORDER_BY_BODY_TYPE[body_type_val]:
                            for ext in (".png", ".webp"):
                                candidates.append(d / f"{head_id_value}{ext}")
                    else:
                        from utility.avatar_paths import (
                            AVATARS_MAPPED_STILL_MALE_DIR,
                            AVATARS_MAPPED_ANIMATED_MALE_DIR,
                            AVATARS_MAPPED_STILL_FEMALE_DIR,
                            AVATARS_MAPPED_ANIMATED_FEMALE_DIR,
                            AVATARS_MAPPED_STILL_SHARED_DIR,
                            AVATARS_MAPPED_ANIMATED_SHARED_DIR,
                        )
                        for d in (
                            AVATARS_MAPPED_STILL_MALE_DIR,
                            AVATARS_MAPPED_ANIMATED_MALE_DIR,
                            AVATARS_MAPPED_STILL_FEMALE_DIR,
                            AVATARS_MAPPED_ANIMATED_FEMALE_DIR,
                            AVATARS_MAPPED_STILL_SHARED_DIR,
                            AVATARS_MAPPED_ANIMATED_SHARED_DIR,
                        ):
                            for ext in (".png", ".webp"):
                                candidates.append(d / f"{head_id_value}{ext}")
                    from utility.avatar_paths import AVATARS_MAPPED_DIR
                    for ext in (".png", ".webp"):
                        candidates.append(AVATARS_MAPPED_DIR / f"{head_id_value}{ext}")
                    for candidate in candidates:
                        if candidate.exists() and candidate.is_file():
                            head_avatar_path = str(candidate)
                            break
        except Exception as head_err:
            logger.warning(f"Failed to resolve head avatar: {head_err}")

        # Fetch player homestead info
        # Note: homeland_info is now fetched in parallel above

        logger.debug(f"[timing] _fetch_player_profile_data.total: {time.time() - t0:.3f}s")
        return {
            'player_pid': player_pid,
            'player_hostnum': player_hostnum,
            'player_nickname': player_nickname,
            'number_id': player_number_id,
            'discord_user_id': discord_user_id,  # Viewed player's Discord ID
            'ly_stage_name': ly_stage_name,
            'level': lv,
            'is_online': is_online,
            'is_invisible': is_invisible,
            'oversea_tag': oversea_tag,
            'online_hours': online_hours,
            'create_time': create_time,
            'player_signature': data.get('name_card', {}).get('sign'),
            'birthday_str': birthday_str,
            'jieyi_name': jieyi_name,
            'jieyi_text': jieyi_text,
            'likes_count': likes_count,
            'likes_data_raw': likes_data_raw,
            'likes_history': likes_history or [],
            'martial_mastery': martial_mastery,
            'scholar_mastery': scholar_mastery,
            'healer_mastery': healer_mastery,
            'explore_mastery': explore_mastery,
            'attr_str': attr_str,
            'attr_con': attr_con,
            'attr_bas': attr_bas,
            'attr_cri': attr_cri,
            'attr_agi': attr_agi,
            'school_emoji': school_emoji,
            'school_name': school_name,
            'school_rank': school_rank,
            'school_data': school_data,
            'fashion_score': fashion_score,
            'arena_1v1_rank': arena_1v1_rank,
            'arena_1v1_max_winning_streak': arena_1v1_max_winning_streak,
            'arena_1v1_total_num': arena_1v1_total_num,
            'arena_3v3_rank': arena_3v3_rank,
            'arena_3v3_total_num': arena_3v3_total_num,
            'group_strategy': group_strategy,
            'group_strategy_total_num': group_strategy_total_num,
            'assist_points': assist_points,
            'guild_name': guild_name,
            'is_our_guild': is_our_guild,
            'guild_level': guild_level,
            'guild_leader': guild_leader,
            'guild_vice_leader': guild_vice_leader,
            'guild_members': guild_members,
            'guild_funds': guild_funds,
            'guild_fame': guild_fame,
            'guild_announcement': guild_announcement,
            'kongfu_main': kongfu_main,
            'kongfu_sub': kongfu_sub,
            'kongfu_role': kongfu_role,
            'is_verified': command_user_verified,
            'head_avatar_path': head_avatar_path,
            'head_id': head_id_value,
            'body_type': body_type_val,
            'sender_pid': str(player_pid) if player_pid else None,
            'cover_img': cover_img,
            'cover_img_path': cover_img_path,
            'homeland_info': homeland_info or {},
            'achievement_data': achievement_data or {},
        }

    async def _build_player_profile_view(self, identifier: str, interaction: discord.Interaction = None, ephemeral: bool = False) -> tuple:
        """
        Complete player lookup workflow: resolve identifier, fetch data, build view.
        Returns (PlayerProfileView, files_list) or (None, None) on failure.
        """
        t0 = time.time()
        # Resolve identifier to PID
        pid, hostnum, _ = await self._resolve_player_identifier(identifier)
        t1 = time.time()
        logger.debug(f"[timing] _build_player_profile_view.resolve: {t1 - t0:.3f}s")
        if not pid:
            return None, None

        # Fetch all player data
        t0 = time.time()
        try:
            profile_data = await self._fetch_player_profile_data(pid, hostnum, interaction)
        except Exception as e:
            logger.error(f"Failed to fetch player profile data: {e}")
            return None, None
        t1 = time.time()
        logger.debug(f"[timing] _build_player_profile_view.fetch_data: {t1 - t0:.3f}s")

        # Create PlayerProfileView
        # discord_user_id = the VIEWED player's Discord (if they're bound), not the command user's
        view = PlayerProfileView(
            player_nickname=profile_data['player_nickname'],
            number_id=profile_data['number_id'],
            discord_user_id=profile_data['discord_user_id'],  # Viewed player's Discord ID
            ly_stage_name=profile_data['ly_stage_name'],
            level=profile_data['level'],
            is_online=profile_data['is_online'],
            is_invisible=profile_data['is_invisible'],
            oversea_tag=profile_data['oversea_tag'],
            online_hours=profile_data['online_hours'],
            create_time=profile_data['create_time'],
            player_signature=profile_data['player_signature'],
            cover_img=profile_data['cover_img'],
            cover_img_path=profile_data['cover_img_path'],
            birthday_str=profile_data['birthday_str'],
            jieyi_name=profile_data['jieyi_name'],
            jieyi_text=profile_data['jieyi_text'],
            likes_count=profile_data['likes_count'],
            likes_data_raw=profile_data['likes_data_raw'],
            likes_history=profile_data.get('likes_history', []),
            martial_mastery=profile_data['martial_mastery'],
            scholar_mastery=profile_data['scholar_mastery'],
            healer_mastery=profile_data['healer_mastery'],
            explore_mastery=profile_data['explore_mastery'],
            attr_str=profile_data['attr_str'],
            attr_con=profile_data['attr_con'],
            attr_bas=profile_data['attr_bas'],
            attr_cri=profile_data['attr_cri'],
            attr_agi=profile_data['attr_agi'],
            school_emoji=profile_data['school_emoji'],
            school_name=profile_data['school_name'],
            school_rank=profile_data['school_rank'],
            school_data=profile_data['school_data'],
            fashion_score=profile_data['fashion_score'],
            arena_1v1_rank=profile_data['arena_1v1_rank'],
            arena_1v1_max_winning_streak=profile_data['arena_1v1_max_winning_streak'],
            arena_1v1_total_num=profile_data['arena_1v1_total_num'],
            arena_3v3_rank=profile_data['arena_3v3_rank'],
            arena_3v3_total_num=profile_data['arena_3v3_total_num'],
            group_strategy=profile_data['group_strategy'],
            group_strategy_total_num=profile_data['group_strategy_total_num'],
            assist_points=profile_data['assist_points'],
            guild_name=profile_data['guild_name'],
            is_our_guild=profile_data['is_our_guild'],
            guild_level=profile_data['guild_level'],
            guild_leader=profile_data['guild_leader'],
            guild_vice_leader=profile_data['guild_vice_leader'],
            guild_members=profile_data['guild_members'],
            guild_funds=profile_data['guild_funds'],
            guild_fame=profile_data['guild_fame'],
            guild_announcement=profile_data['guild_announcement'],
            kongfu_main=profile_data['kongfu_main'],
            kongfu_sub=profile_data['kongfu_sub'],
            kongfu_role=profile_data['kongfu_role'],
            is_verified=profile_data['is_verified'],
            head_avatar_path=profile_data['head_avatar_path'],
            head_id=profile_data['head_id'],
            body_type=profile_data['body_type'],
            sender_pid=profile_data['sender_pid'],
            player_pid=profile_data['player_pid'],
            player_hostnum=profile_data['player_hostnum'],
            homeland_info=profile_data['homeland_info'],
            achievement_data=profile_data['achievement_data'],
        )

        files = view._resolve_files()
        return view, files

    @player_group.command(name="search", description="Search for a WWM player by Number ID or nickname")
    @app_commands.describe(
        identifier="Player's 10-digit Number ID or in-game nickname (auto-detects)"
    )
    async def player_search(self, interaction: discord.Interaction, identifier: str = None):
        """Search for a player and display their full profile."""
        await interaction.response.send_message("🔍 Searching for player...", ephemeral=False)

        # If no identifier provided, try to look up the caller's own account
        if not identifier or not identifier.strip():
            # Check if command user is verified (allows them to use this feature)
            async with aiosqlite.connect(DB_PATH) as conn:
                cursor = await conn.execute(
                    "SELECT character_uid FROM verified_members WHERE user_id = ?",
                    (interaction.user.id,)
                )
                row = await cursor.fetchone()

            if row is None:
                embed = discord.Embed(
                    title="❌ Account Not Bound",
                    description="You must provide either a **Number ID** or a **nickname** to search by, "
                                "or bind your account first in <#1469961307154288703> to look up yourself.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            # Command user is verified — use their own character_uid (Number ID)
            identifier = row[0]

        if not WWM_UID or not WWM_TOKEN:
            embed = discord.Embed(
                title="❌ API Not Configured",
                description="WWM API credentials are not set up properly. Please contact bot owner.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Use shared helper - it handles:
            # 1. Smart routing (10-digit → number_id API, else → nickname API)
            # 2. Full data fetching
            # 3. Correct discord_user_id (viewed player's Discord, not command user's)
            view, files = await self._build_player_profile_view(identifier, interaction, ephemeral=False)
            
            if not view:
                embed = discord.Embed(
                    title="❌ Player Not Found",
                    description=f"No player found matching '{identifier}'. Try a 10-digit Number ID or exact nickname.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return
            
            # Cache the original message on the view so on_timeout() can edit it
            view._original_message = await interaction.original_response()
            await interaction.edit_original_response(
                content=None,
                embed=None,
                view=view,
                attachments=files,
            )

        except Exception as e:
            logger.error(f"Player search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Search Error",
                description=f"Failed to search for player: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(content=None, embed=embed)

    @tasks.loop(minutes=1)
    async def guild_monitor_task(self):
        if not self.monitor_enabled or not self.monitor_channel:
            return
        
        try:
            guild_data = await get_full_guild_info(CLUB_ID)
            
            if not guild_data:
                logger.warning("Guild check returned no data")
                return
            
            board_data = await self._gather_status_data(guild_data)
            if not board_data:
                return
            
            now_ts = int(discord.utils.utcnow().timestamp())
            
            # Debug: trace the exact birthdays_this_week data being passed to the view
            for bday_entry in board_data['birthdays_this_week']:
                logger.debug(f"BIRTHDAY_VIEW_DATA: entry={bday_entry}")

            # Build the LayoutView board
            view = GuildStatusBoard(
                cog=self,
                guild_name=board_data['guild_name'],
                guild_level=board_data['guild_level'],
                member_count=board_data['member_count'],
                apprentice_count=board_data['apprentice_count'],
                funds=board_data['funds'],
                total_fame=board_data['total_fame'],
                week_fame=board_data['week_fame'],
                gvg_points=board_data['gvg_points'],
                online_count=board_data['online_count'],
                weekly_leaderboard=board_data['weekly_leaderboard'],
                pending_apps=board_data['pending_apps'],
                now_ts=now_ts,
                next_update_ts=now_ts + 60,
                birthdays_this_week=board_data['birthdays_this_week'],
                press_count=self.online_players_button_presses,
                league_info=board_data.get('league_info', {}),
            )
            
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO guild_player_counts (ts, total_members, online_count, guild_week_fame) VALUES (?, ?, ?, ?)",
                        (now_ts, board_data['member_count'], board_data['online_count'], board_data['week_fame'])
                    )
                    
                    if board_data['players_data'] is not None:
                        snapshot = []
                        for pid, player_data in board_data['players_data'].items():
                            base = player_data.get('base', {})
                            club = player_data.get('club', {})
                            player_entry = {
                                'pid': pid,
                                'nickname': base.get('nickname', 'Unknown'),
                                'level': base.get('level', 0),
                                'number_id': str(base.get('number_id', '')),
                                'is_online': base.get('is_online', 0) == 1,
                                'oversea_tag': str(base.get('oversea_tag', '')),
                                'online_time': base.get('online_time', 0),
                                'last_online_ts': base.get('last_online_ts', 0),
                                'liveness': club.get('liveness', 0),
                                'total_liveness': club.get('total_liveness', 0),
                                'contribution': club.get('contribution', 0),
                            }
                            snapshot.append(player_entry)
                        
                        await db.execute(
                            "INSERT OR IGNORE INTO guild_player_snapshots (ts, snapshot_json) VALUES (?, ?)",
                            (now_ts, json.dumps(snapshot, ensure_ascii=False))
                        )
                    
                    cleanup_ts = now_ts - 30 * 86400
                    await db.execute("DELETE FROM guild_player_counts WHERE ts < ?", (cleanup_ts,))
                    await db.execute("DELETE FROM guild_player_snapshots WHERE ts < ?", (cleanup_ts,))
                    await db.commit()

            except Exception as e:
                logger.warning(f"Failed to record player count: {e}")
            
            # Assign birthday roles to verified Discord members
            birthday_pids = [entry[4] for entry in board_data['birthdays_this_week'] if len(entry) >= 5]
            await self._assign_birthday_roles(birthday_pids)

            # Edit the existing message with the new LayoutView (clear legacy fields)
            await self.monitor_message.edit(content=None, embeds=[], attachments=[], view=view)
            logger.debug("Guild status message updated successfully")
            
            # Notify about new pending applications
            applys = guild_data.get('result', {}).get('applys', {}).get('apply_dict', {})
            if applys:
                new_applications = {}
                for pid, app in applys.items():
                    if pid not in self.last_known_applications:
                        new_applications[pid] = app
                
                if new_applications:
                    try:
                        channel = self.bot.get_channel(self.pending_apps_channel_id)
                        if channel:
                            embed = discord.Embed(
                                title="📥 New Guild Applications",
                                color=discord.Color.blue(),
                                timestamp=discord.utils.utcnow()
                            )
                            app_lines = []
                            for pid, app in new_applications.items():
                                nickname = app.get('nickname', 'Unknown')
                                app_lines.append(f"• {nickname}")
                            embed.description = f"There are **{len(applys)}** pending applications total.\n\n**New applications:**\n" + "\n".join(app_lines)
                            await channel.send(embed=embed)
                    except Exception as e:
                        logger.error(f"Failed to send pending applications notification: {e}")
                
                self.last_known_applications = dict(applys)
            else:
                if self.last_known_applications:
                    logger.debug("All pending applications have been resolved")
                self.last_known_applications = {}
            
            if self.last_guild_state is not None:
                diff = DeepDiff(self.last_guild_state, guild_data, ignore_order=True, exclude_paths=["root['timestamp']"])
                if diff:
                    logger.debug(f"Guild changes detected: {list(diff.keys())}")
                    await self._process_changes(diff, guild_data)
            
            self.last_guild_state = guild_data
            
        except Exception as e:
            logger.error(f"Guild monitor task failed: {str(e)}", exc_info=True)

    async def _assign_birthday_roles(self, birthday_pids: list):
        """Assign birthday role to verified Discord members whose characters have birthdays this week,
        and remove the role from members who no longer have a birthday this week."""
        try:
            guild = self.bot.get_guild(settings.DISCORD_SERVER_ID) if hasattr(settings, 'DISCORD_SERVER_ID') else None
            if not guild:
                return

            birthday_role = guild.get_role(BIRTHDAY_ROLE_ID)
            if not birthday_role:
                logger.debug(f"Birthday role {BIRTHDAY_ROLE_ID} not found in guild")
                return

            # Gather the set of Discord user IDs that should keep the role this week
            verified_user_ids = set()
            if birthday_pids:
                async with aiosqlite.connect(DB_PATH) as conn:
                    placeholders = ','.join('?' * len(birthday_pids))
                    cursor = await conn.execute(
                        f"SELECT user_id, player_pid FROM verified_members WHERE player_pid IN ({placeholders})",
                        birthday_pids
                    )
                    verified_rows = await cursor.fetchall()

                # Assign role to verified members whose characters have birthdays this week
                for user_id, player_pid in verified_rows:
                    verified_user_ids.add(user_id)
                    member = guild.get_member(user_id)
                    if not member:
                        continue
                    try:
                        if birthday_role not in member.roles:
                            await member.add_roles(birthday_role)
                            logger.debug(f"🎂 Assigned birthday role to {member} (PID: {player_pid})")
                    except Exception as e:
                        logger.error(f"Failed to assign birthday role to {member}: {e}")

            # Remove the birthday role from any member who currently has it but
            # should NOT have it this week (previous weeks' birthdays, left guild, etc.)
            for member in guild.members:
                if birthday_role in member.roles and member.id not in verified_user_ids:
                    try:
                        await member.remove_roles(birthday_role)
                        logger.debug(f"🗑️ Removed birthday role from {member} (no birthday this week)")
                    except Exception as e:
                        logger.error(f"Failed to remove birthday role from {member}: {e}")
        except Exception as e:
            logger.error(f"Birthday role assignment failed: {e}")

    async def _gather_status_data(self, guild_data):
        """Extract structured status data from guild API response.
        
        Returns a dict with all fields needed to build the GuildStatusBoard,
        or None if data is invalid.
        """
        result = guild_data.get('result', {})
        base = result.get('base', {})
        members = result.get('members', {})
        play = result.get('play', {})
        
        member_list = members.get('members', {})
        member_count = members.get('member_num', 0)
        
        now = discord.utils.utcnow().timestamp()
        
        online = 0
        players_data = None
        
        all_pids = list(member_list.keys())
        
        try:
            bulk_data = await get_bulk_players_info(all_pids, fields=["base", "club", "birthday"])
            if bulk_data and bulk_data.get('code') == 0:
                players_data = bulk_data.get('result', {})
                for pid, player_data in players_data.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online += 1
        except Exception as e:
            logger.warning(f"Failed to get bulk player data, falling back to estimate: {e}")
            for pid, member in member_list.items():
                last_online = member.get('last_online_ts', 0)
                if now - last_online < 7200:
                    online += 1

        # Calculate current schedule week start (Monday 5:00 AM GMT+8)
        now_utc_ts = int(discord.utils.utcnow().timestamp())
        GMT8_OFFSET = 8 * 3600
        gmt8_now_ts = now_utc_ts + GMT8_OFFSET
        gmt8_dt = datetime.datetime.fromtimestamp(gmt8_now_ts, tz=datetime.timezone.utc)
        adjusted_dt = gmt8_dt - datetime.timedelta(hours=5)
        monday_dt = adjusted_dt - datetime.timedelta(days=adjusted_dt.weekday())
        week_start_gmt8 = monday_dt.replace(hour=5, minute=0, second=0, microsecond=0)
        week_start_ts = int(week_start_gmt8.timestamp() - GMT8_OFFSET)

        weekly_leaderboard = []
        
        if players_data is not None:
            for pid, member in member_list.items():
                nickname = member.get('nickname', 'Unknown')
                weekly_points = 0
                
                if pid in players_data:
                    player_data = players_data[pid]
                    club_data = player_data.get('club', {})
                    base_data = player_data.get('base', {})
                    weekly_points = club_data.get('liveness', 0)
                    if 'nickname' in base_data:
                        nickname = base_data.get('nickname', nickname)
                
                last_online = member.get('last_online_ts', 0)
                if last_online < week_start_ts:
                    continue
                
                weekly_leaderboard.append((nickname, weekly_points))
        else:
            for pid, member in member_list.items():
                nickname = member.get('nickname', 'Unknown')
                club_data = member.get('club', {})
                weekly_points = club_data.get('liveness', 0)
                last_online = member.get('last_online_ts', 0)
                if last_online < week_start_ts:
                    continue
                weekly_leaderboard.append((nickname, weekly_points))

        # Sort by points descending
        weekly_leaderboard.sort(key=lambda x: x[1], reverse=True)

        applys = result.get('applys', {}).get('apply_dict', {})

        # ---- Birthday This Week ----
        # Calculate schedule week boundaries (Monday 5:00 AM GMT+8 → next Monday 5:00 AM GMT+8)
        today_dt = gmt8_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end_gmt8 = week_start_gmt8 + datetime.timedelta(days=7)
        current_year = gmt8_dt.year
        birthdays_this_week = []

        def birthday_in_week(b_month, b_day, week_start, week_end):
            """Check if month/day falls within the schedule week date range (handles year wrap)."""
            for yr in (current_year, current_year + 1):
                try:
                    bd = datetime.datetime(yr, b_month, b_day, tzinfo=datetime.timezone.utc)
                except ValueError:
                    continue
                if week_start <= bd < week_end:
                    return True
            return False

        def days_until_birthday(b_month, b_day):
            """Calculate days until the next occurrence of month/day from today (for display)."""
            for yr in (current_year, current_year + 1):
                try:
                    bd = datetime.datetime(yr, b_month, b_day, tzinfo=datetime.timezone.utc)
                except ValueError:
                    continue
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if bd >= now_utc:
                    return (bd - now_utc).days
            return 9999

        birthdays_this_week = []
        birthday_pids = []
        if players_data is not None:
            for pid, player_data in players_data.items():
                birthday_data = player_data.get('birthday', {})
                if not isinstance(birthday_data, dict):
                    continue
                visible_flag = birthday_data.get('visible', 0)
                if visible_flag != 0:
                    continue
                month = birthday_data.get('month', 0)
                day = birthday_data.get('day', 0)
                if month <= 0 or day <= 0:
                    continue
                if birthday_in_week(month, day, week_start_gmt8, week_end_gmt8):
                    nickname = player_data.get('base', {}).get('nickname', member_list.get(pid, {}).get('nickname', 'Unknown'))
                    days = days_until_birthday(month, day)
                    birthdays_this_week.append((nickname, month, day, days, pid))
                    birthday_pids.append(pid)

            # Sort by days then month/day
            birthdays_this_week.sort(key=lambda x: (x[3], x[1], x[2]))

        ranked_match_score = play.get('pk_match_info', {}).get('battle_score', 0)
        league_info = play.get('league_info', {}) or {}

        return {
            'guild_name': base.get('name', 'Unknown'),
            'guild_level': base.get('level', 0),
            'member_count': member_count,
            'apprentice_count': members.get('apprentice_num', 0),
            'funds': base.get('fund', 0),
            'total_fame': base.get('fame', 0),
            'week_fame': base.get('week_fame', 0),
            'gvg_points': play.get('pk_match_info', {}).get('battle_score', 0),
            'online_count': online,
            'weekly_leaderboard': weekly_leaderboard,
            'pending_apps': len(applys),
            'players_data': players_data,
            'birthdays_this_week': birthdays_this_week,
            'ranked_match_score': ranked_match_score,
            'league_info': league_info,
        }
    
    async def _process_changes(self, diff, new_data):
        changes = []
        
        if 'iterable_item_added' in diff:
            for path, item in diff['iterable_item_added'].items():
                if 'members' in path and isinstance(item, dict) and 'nickname' in item:
                    changes.append(f"✅ **New Member Joined:** {item.get('nickname')}")
        
        if 'iterable_item_removed' in diff:
            for path, item in diff['iterable_item_removed'].items():
                if 'members' in path and isinstance(item, dict) and 'nickname' in item:
                    changes.append(f"❌ **Member Left:** {item.get('nickname')}")
        
        if 'values_changed' in diff:
            for path, change in diff['values_changed'].items():
                if 'building' in path and 'lv' in path:
                    changes.append(f"🏗️ **Building Upgraded:** Level {change['old_value']} → {change['new_value']}")
        
        if 'values_changed' in diff:
            for path, change in diff['values_changed'].items():
                if path.endswith('base.level'):
                    changes.append(f"⭐ **GUILD LEVEL UP!** {change['old_value']} → {change['new_value']}")
        
        if 'iterable_item_added' in diff:
            for path, item in diff['iterable_item_added'].items():
                if 'apply_dict' in path:
                    changes.append(f"📥 **New Guild Application:** {item.get('nickname', 'Unknown')}")
        
        if 'values_changed' in diff:
            for path, change in diff['values_changed'].items():
                if 'gonggao_info.msg' in path:
                    changes.append(f"📢 **Guild Announcement Updated!**")
        
        if changes:
            embed = discord.Embed(
                title="🏰 Guild Activity",
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            embed.description = "\n\n".join(changes)
            await self.monitor_channel.send(embed=embed)
    
    @guild_monitor_task.before_loop
    async def before_guild_monitor(self):
        await self.bot.wait_until_ready()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM monitor_config WHERE key = 'message_id'")
            row = await cursor.fetchone()
            if row and self.monitor_channel:
                try:
                    self.monitor_message = await self.monitor_channel.fetch_message(int(row[0]))
                except:
                    # Message not found or other error — create fresh V2 message
                    guild_data = await get_full_guild_info(CLUB_ID)
                    if guild_data:
                        board_data = await self._gather_status_data(guild_data)
                        if board_data:
                            now_ts = int(discord.utils.utcnow().timestamp())
                            view = GuildStatusBoard(
                                cog=self,
                                guild_name=board_data['guild_name'],
                                guild_level=board_data['guild_level'],
                                member_count=board_data['member_count'],
                                apprentice_count=board_data['apprentice_count'],
                                funds=board_data['funds'],
                                total_fame=board_data['total_fame'],
                                week_fame=board_data['week_fame'],
                                gvg_points=board_data['gvg_points'],
                                online_count=board_data['online_count'],
                                weekly_leaderboard=board_data['weekly_leaderboard'],
                                pending_apps=board_data['pending_apps'],
                                now_ts=now_ts,
                                next_update_ts=now_ts + 60,
                                birthdays_this_week=board_data['birthdays_this_week'],
                                press_count=self.online_players_button_presses,
                                league_info=board_data.get('league_info', {}),
                            )
                            self.monitor_message = await self.monitor_channel.send(content=None, embeds=[], view=view)
                            await self._save_config()
                            self.last_guild_state = guild_data
    
    @guild_group.command(name="set-channel", description="Set channel for guild monitor notifications")
    @admin_or_staff()
    async def set_monitor_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.monitor_channel = channel
        
        guild_data = await get_full_guild_info(CLUB_ID)
        if guild_data:
            board_data = await self._gather_status_data(guild_data)
            if board_data:
                now_ts = int(discord.utils.utcnow().timestamp())
                view = GuildStatusBoard(
                    cog=self,
                    guild_name=board_data['guild_name'],
                    guild_level=board_data['guild_level'],
                    member_count=board_data['member_count'],
                    apprentice_count=board_data['apprentice_count'],
                    funds=board_data['funds'],
                    total_fame=board_data['total_fame'],
                    week_fame=board_data['week_fame'],
                    gvg_points=board_data['gvg_points'],
                    online_count=board_data['online_count'],
                    weekly_leaderboard=board_data['weekly_leaderboard'],
                    pending_apps=board_data['pending_apps'],
                    now_ts=now_ts,
                    next_update_ts=now_ts + 60,
                    birthdays_this_week=board_data['birthdays_this_week'],
                    press_count=self.online_players_button_presses,
                    league_info=board_data.get('league_info', {}),
                )
            self.monitor_message = await channel.send(content=None, embeds=[], view=view)
            self.last_guild_state = guild_data
        
        await self._save_config()
        await interaction.response.send_message(f"✅ Guild monitor channel set to {channel.mention}. Status board created.", ephemeral=True)
        logger.info(f"Guild monitor channel set to {channel.id} by {interaction.user}")
    
    @guild_group.command(name="toggle", description="Enable or disable guild monitoring")
    @admin_or_staff()
    async def toggle_monitor(self, interaction: discord.Interaction):
        self.monitor_enabled = not self.monitor_enabled
        
        if self.monitor_enabled:
            if not self.guild_monitor_task.is_running():
                self.guild_monitor_task.start()
            status = "✅ ENABLED"
        else:
            if self.guild_monitor_task.is_running():
                self.guild_monitor_task.cancel()
            status = "❌ DISABLED"
        
        await self._save_config()
        await interaction.response.send_message(f"Guild monitor is now {status}", ephemeral=True)
        logger.info(f"Guild monitor toggled to {self.monitor_enabled} by {interaction.user}")
    
    @guild_group.command(name="force-check", description="Run an immediate guild check")
    @admin_or_staff()
    async def force_guild_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        guild_data = await get_full_guild_info(CLUB_ID)
        
        if not guild_data:
            await interaction.followup.send("❌ Failed to retrieve guild data")
            return
        
        self.last_guild_state = guild_data
        await interaction.followup.send("✅ Guild data refreshed successfully. Next check will detect changes from this state.")
    
    @guild_group.command(name="status", description="View current guild monitor status")
    async def monitor_status(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🏰 Guild Monitor Status", color=discord.Color.blurple())
        
        embed.add_field(name="Status", value="✅ Running" if self.monitor_enabled and self.guild_monitor_task.is_running() else "❌ Stopped", inline=True)
        embed.add_field(name="Check Interval", value=f"{self.check_interval_minutes} minutes", inline=True)
        embed.add_field(name="Channel", value=self.monitor_channel.mention if self.monitor_channel else "Not set", inline=True)
        embed.add_field(name="Last State", value="Stored" if self.last_guild_state else "Not initialized", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @guild_group.command(name="search", description="Search for a guild by Player ID")
    @app_commands.describe(player_id="Search using a player's 10-digit Number ID (finds their guild)")
    async def guild_search(self, interaction: discord.Interaction, player_id: str):
        await interaction.response.send_message("🔍 Searching for player...")

        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT 1 FROM verified_members WHERE user_id = ?", (interaction.user.id,))
            row = await cursor.fetchone()
            is_verified = row is not None

        if not is_verified:
            embed = discord.Embed(
                title="❌ Account Not Bound",
                description="You must bind your WWM game account before you can use this command.\n\nUse the account binding system first.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        if not WWM_UID or not WWM_TOKEN:
            embed = discord.Embed(
                title="❌ API Not Configured",
                description="WWM API credentials are not set up properly. Please contact bot owner.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        target_guild_id = None
        target_hostnum = 10103

        try:
            if not player_id.isdigit() or len(player_id) != 10:
                embed = discord.Embed(
                    title="❌ Invalid Player ID",
                    description="Player ID must be exactly 10 digits long",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            await interaction.edit_original_response(content="✅ Found player\n🏰 Looking up guild info...")
            
            player_data = await get_player_info(player_id)
            
            if not player_data or 'result' not in player_data:
                embed = discord.Embed(title="❌ Player not found", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return

            player_result = player_data['result']
            player_pid = player_result.get('id')

            if not player_pid:
                embed = discord.Embed(title="❌ Failed to get player data", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return

            club_data = await get_club_hostnums(player_pid)
            
            if not club_data or 'result' not in club_data:
                embed = discord.Embed(title="❌ Player is not in any guild", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return

            result_data = club_data['result']
            player_club_data = result_data.get(player_pid, {})
            club_info = player_club_data.get('club', {})
            
            target_guild_id = club_info.get('club_id')
            target_hostnum = club_info.get('hostnum', 10103)

            if not target_guild_id:
                embed = discord.Embed(title="❌ Player is not in any guild", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return

            await interaction.edit_original_response(content="✅ Found player\n🏰 Looking up guild info...\n📋 Loading guild data...")
            
            guild_data = await get_full_guild_info(target_guild_id, hostnum=target_hostnum)
            
            if not guild_data or 'result' not in guild_data:
                embed = discord.Embed(title="❌ Guild not found", color=discord.Color.red())
                await interaction.edit_original_response(content=None, embed=embed)
                return

            result = guild_data['result']
            base = result.get('base', {})
            members = result.get('members', {})
            play = result.get('play', {})
            create_ts = base.get('create_ts', 0)

            embed = discord.Embed(title="🏰 Guild Profile", color=discord.Color.og_blurple())
            embed.description = f"**{base.get('name', 'Unknown Guild')}**"
            embed.add_field(name="📛 Guild Name", value=f"`{base.get('name', 'Unknown')}`", inline=True)
            embed.add_field(name="⭐ Level", value=f"`{base.get('level', 0)}`", inline=True)
            embed.add_field(name="📅 Creation Date", value=f"<t:{create_ts}:R>" if create_ts else "Unknown", inline=True)
            embed.add_field(name="👥 Members", value=f"`{members.get('member_num', 0)} / 100`", inline=True)
            embed.add_field(name="💰 Guild Funds", value=f"`{base.get('fund', 0):,}`", inline=True)
            embed.add_field(name="📈 Total Fame", value=f"`{base.get('fame', 0):,}`", inline=True)
            embed.add_field(name="🔥 Weekly Activity", value=f"`{base.get('week_fame', 0):,}`", inline=True)
            embed.add_field(name="🏆 Ranked Points", value=f"`{play.get('pk_match_info', {}).get('battle_score', 0)}`", inline=True)

            leader_name = "None"
            vice_leader_name = "None"
            leader_pid = "None"
            vice_leader_pid = "None"
            
            member_list = members.get('members', {})
            for pid, member in member_list.items():
                post_list = member.get('post', [])
                if 1 in post_list:
                    leader_pid = pid
                if 2 in post_list:
                    vice_leader_pid = pid

            pids_to_fetch = []
            if leader_pid != "None":
                pids_to_fetch.append(leader_pid)
            if vice_leader_pid != "None":
                pids_to_fetch.append(vice_leader_pid)

            if pids_to_fetch:
                from utility.wwm import get_bulk_players_info
                bulk_data = await get_bulk_players_info(pids_to_fetch, fields=["base"])
                if bulk_data and bulk_data.get('code') == 0:
                    players = bulk_data.get('result', {})
                    if leader_pid in players:
                        leader_base = players[leader_pid].get('base', {})
                        leader_name = leader_base.get('nickname', 'Unknown')
                    if vice_leader_pid in players:
                        vice_base = players[vice_leader_pid].get('base', {})
                        vice_leader_name = vice_base.get('nickname', 'Unknown')

            logger.debug(f"=== GUILD LEADERSHIP FOUND ===")
            logger.debug(f"Guild Leader: {leader_name} | PID: {leader_pid}")
            logger.debug(f"Vice Leader: {vice_leader_name} | PID: {vice_leader_pid}")

            online = 0
            all_pids = list(member_list.keys())
            from utility.wwm import get_bulk_players_info
            bulk_data = await get_bulk_players_info(all_pids, fields=["base"])
            if bulk_data and bulk_data.get('code') == 0:
                players = bulk_data.get('result', {})
                for pid, player_data in players.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online += 1

            embed.add_field(name="👑 Guild Leader", value=f"`{leader_name}`", inline=True)
            embed.add_field(name="⚔️ Vice Leader", value=f"`{vice_leader_name}`", inline=True)
            embed.add_field(name="🟢 Online Now", value=f"`{online} / {members.get('member_num', 0)}`", inline=True)

            announcement = result.get('gonggao_info', {}).get('msg')
            if announcement and announcement.strip():
                embed.add_field(name="📢 Guild Announcement", value=f"`{announcement}`", inline=False)

            await interaction.edit_original_response(content=None, embed=embed)

        except Exception as e:
            logger.error(f"Guild search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Search Failed",
                description=f"An error occurred while searching: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @guild_group.command(name="search-name", description="Search for a guild by name or guild ID")
    @app_commands.describe(query="The guild name or numeric guild ID to search for")
    async def guild_search_name(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        
        try:
            if not query or len(query.strip()) == 0:
                embed = discord.Embed(title="❌ Invalid Query", description="Please provide a guild name or guild ID to search for.", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return
            
            search_term = query.strip()
            
            # ── If the input is purely numeric, treat it as a guild ID ──
            if search_term.isdigit():
                await interaction.edit_original_response(content="🔍 Looking up guild by ID...")
                
                guild_lookup = await get_club_by_number_id(int(search_term))
                
                if not guild_lookup:
                    embed = discord.Embed(title="❌ Guild Not Found", description=f"No guild found with ID `{search_term}`", color=discord.Color.red())
                    await interaction.followup.send(embed=embed)
                    return
                
                club_id = guild_lookup.get('club_id')
                hostnum = guild_lookup.get('hostnum', 10103)
                
                if not club_id:
                    embed = discord.Embed(title="❌ Invalid Guild Data", color=discord.Color.red())
                    await interaction.followup.send(embed=embed)
                    return
                
                await interaction.edit_original_response(content="🔍 Looking up guild by ID...\n📋 Loading guild data...")
                
                guild_data = await get_full_guild_info(club_id, hostnum=hostnum)
                
                if not guild_data or 'result' not in guild_data:
                    embed = discord.Embed(title="❌ Guild not found or API error", color=discord.Color.red())
                    await interaction.followup.send(embed=embed)
                    return
                
                result = guild_data['result']
                base = result.get('base', {})
                members = result.get('members', {})
                play = result.get('play', {})
                create_ts = base.get('create_ts', 0)
                
                leader_name = "None"
                vice_leader_name = "None"
                leader_pid = "None"
                vice_leader_pid = "None"
                
                member_list = members.get('members', {})
                for pid, member in member_list.items():
                    post_list = member.get('post', [])
                    if 1 in post_list:
                        leader_pid = pid
                    if 2 in post_list:
                        vice_leader_pid = pid
                
                pids_to_fetch = []
                if leader_pid != "None":
                    pids_to_fetch.append(leader_pid)
                if vice_leader_pid != "None":
                    pids_to_fetch.append(vice_leader_pid)
                
                if pids_to_fetch:
                    bulk_data = await get_bulk_players_info(pids_to_fetch, fields=["base"])
                    if bulk_data and bulk_data.get('code') == 0:
                        players = bulk_data.get('result', {})
                        if leader_pid in players:
                            leader_base = players[leader_pid].get('base', {})
                            leader_name = leader_base.get('nickname', 'Unknown')
                        if vice_leader_pid in players:
                            vice_base = players[vice_leader_pid].get('base', {})
                            vice_leader_name = vice_base.get('nickname', 'Unknown')
                
                online = 0
                all_pids = list(member_list.keys())
                bulk_data = await get_bulk_players_info(all_pids, fields=["base"])
                if bulk_data and bulk_data.get('code') == 0:
                    players = bulk_data.get('result', {})
                    for pid, player_data in players.items():
                        player_base = player_data.get('base', {})
                        if player_base.get('is_online', 0) == 1:
                            online += 1
                
                announcement = result.get('gonggao_info', {}).get('msg')
                
                # Use GuildProfileView instead of embed
                view = GuildProfileView(
                    guild_name=base.get('name', 'Unknown Guild'),
                    guild_level=base.get('level', 0),
                    member_count=members.get('member_num', 0),
                    member_max=100,
                    create_ts=create_ts,
                    funds=base.get('fund', 0),
                    total_fame=base.get('fame', 0),
                    week_fame=base.get('week_fame', 0),
                    gvg_points=play.get('pk_match_info', {}).get('battle_score', 0),
                    leader_name=leader_name,
                    vice_leader_name=vice_leader_name,
                    online_count=online,
                    announcement=announcement,
                )
                await interaction.edit_original_response(content=None, embed=None, view=view)
                return
            
            # ── Otherwise, treat it as a guild name search ──
            clubs = await get_club_by_name(search_term, limit=5)
            
            if not clubs or len(clubs) == 0:
                embed = discord.Embed(title="❌ No Results", description=f"No guilds found matching `{search_term}`", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return
            
            club_ids = [club.get('club_id') for club in clubs]
            hostnums = [club.get('hostnum', 10103) for club in clubs]
            guild_infos = await get_club_brief_info_batch(club_ids, hostnums) or []
            
            guild_info_map = {}
            for info in guild_infos:
                info_club_id = info.get('club_id')
                if info_club_id:
                    guild_info_map[info_club_id] = info
            
            valid_clubs = []
            valid_infos = []
            for club in clubs:
                cid = club.get('club_id')
                if cid in guild_info_map:
                    valid_clubs.append(club)
                    valid_infos.append(guild_info_map[cid])
            
            if len(valid_clubs) == 0:
                embed = discord.Embed(title="❌ No Active Guilds Found", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return
            
            removed_count = len(clubs) - len(valid_clubs)
            
            # Build search results text for the V2 view
            result_lines = []
            for idx, info in enumerate(valid_infos, 1):
                guild_name = info.get('base', {}).get('name', 'Unknown')
                member_num = info.get('members', {}).get('member_num', '?')
                apprentice_num = info.get('members', {}).get('apprentice_num', '?')
                result_lines.append(f"**{idx}.** **{guild_name}** — 👥 `{member_num}` 🎓 `{apprentice_num}`")
            
            results_text = "\n".join(result_lines)
            description = f"Found **{len(valid_clubs)}** active guild(s) matching `{search_term}`"
            if removed_count > 0:
                description += f"\n*({removed_count} deleted guild(s) filtered out)*"
            
            # Send the V2 view with search results embedded
            view = GuildSearchSelectView(valid_clubs, valid_infos, self, header=f"# 🔍 Guild Search Results\n{description}\n\n### Results\n{results_text}")
            await interaction.followup.send(content=None, embed=None, view=view)
            
        except Exception as e:
            logger.error(f"Guild name search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(title="❌ Search Failed", description=f"An error occurred while searching: `{str(e)}`", color=discord.Color.red())
            await interaction.followup.send(embed=embed)

    @guild_group.command(name="stats", description="Display graphs of guild statistics over time")
    @admin_or_staff()
    @app_commands.describe(type="Type of graph to display", period="Time range for the graph")
    @app_commands.choices(type=[
        app_commands.Choice(name="🟢 Online Players", value="online"),
        app_commands.Choice(name="🌐 Online by Region Over Time", value="online_by_region"),
        app_commands.Choice(name="🔥 Liveness Gain Over Time", value="liveness_gain"),
    ])
    @app_commands.choices(period=[
        app_commands.Choice(name="Today (5am GMT+8 to now)", value="today"),
        app_commands.Choice(name="This Week (current schedule week)", value="week"),
        app_commands.Choice(name="Last 7 Days", value="7days"),
    ])
    async def guild_stats(self, interaction: discord.Interaction, type: str = "online", period: str = "today"):
        await interaction.response.defer()
        
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import MaxNLocator
            import datetime as dt
            import numpy as np
            from collections import Counter
            import json
            
            now_utc = dt.datetime.now(dt.timezone.utc)
            GMT8_OFFSET = 8 * 3600
            now_ts = int(now_utc.timestamp())
            
            if period == "today":
                gmt8_now = now_ts + GMT8_OFFSET
                gmt8_dt = dt.datetime.fromtimestamp(gmt8_now, tz=dt.timezone.utc)
                schedule_start = gmt8_dt.replace(hour=5, minute=0, second=0, microsecond=0)
                if gmt8_dt.hour < 5:
                    schedule_start -= dt.timedelta(days=1)
                start_ts = int(schedule_start.timestamp() - GMT8_OFFSET)
            elif period == "week":
                gmt8_now = now_ts + GMT8_OFFSET
                gmt8_dt = dt.datetime.fromtimestamp(gmt8_now, tz=dt.timezone.utc)
                adjusted = gmt8_dt - dt.timedelta(hours=5)
                monday = adjusted - dt.timedelta(days=adjusted.weekday())
                schedule_start = monday.replace(hour=5, minute=0, second=0, microsecond=0)
                start_ts = int(schedule_start.timestamp() - GMT8_OFFSET)
            else:
                start_ts = now_ts - 7 * 86400
            
            period_labels = {"today": "Today", "week": "This Week", "7days": "Last 7 Days"}
            
            schedule_events = []
            try:
                async with aiosqlite.connect(SCHEDULE_DB_PATH) as sched_db:
                    sched_db.row_factory = aiosqlite.Row
                    cursor = await sched_db.execute(
                        "SELECT event_name, timestamp FROM schedule_events WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
                        (start_ts, now_ts)
                    )
                    all_rows = await cursor.fetchall()
                    for row in all_rows:
                        name = row['event_name']
                        if any(kw in name for kw in ["Guild Party", "Showdown", "Breaking Army"]):
                            schedule_events.append((name, row['timestamp']))
            except Exception as sched_err:
                logger.warning(f"Failed to fetch schedule events for graph: {sched_err}")
            
            if type == "online":
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT ts, online_count, total_members FROM guild_player_counts WHERE ts >= ? ORDER BY ts ASC",
                        (start_ts,)
                    )
                    rows = await cursor.fetchall()
                
                if not rows:
                    await interaction.followup.send("❌ No data available for the selected time range.")
                    return
                
                timestamps = [row['ts'] for row in rows]
                dates = [dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc) for ts in timestamps]
                y_values = [row['online_count'] for row in rows]
                
                plt.style.use('dark_background')
                fig, ax = plt.subplots(figsize=(12, 6))
                ax.fill_between(dates, y_values, alpha=0.3, color='#2ECC71')
                ax.plot(dates, y_values, color='#2ECC71', linewidth=2, marker='', linestyle='-')
                
                if len(y_values) >= 10:
                    window = min(10, len(y_values) // 3)
                    if window > 1:
                        weights = np.ones(window) / window
                        smoothed = np.convolve(y_values, weights, mode='valid')
                        smooth_dates = dates[window-1:]
                        ax.plot(smooth_dates, smoothed, color='#FFD700', linewidth=1.5, linestyle='--', alpha=0.7, label='Trend')
                
                ax.set_facecolor('#1a1a2e')
                fig.patch.set_facecolor('#1a1a2e')
                ax.grid(True, alpha=0.2, color='white')
                ax.set_xlabel('Time (GMT+8)', color='white', fontsize=12)
                ax.set_ylabel('Online Players', color='white', fontsize=12)
                ax.set_title(f'Online Players Over Time - {period_labels.get(period, "Custom")}', color='white', fontsize=14, fontweight='bold')
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))
                
                if period == "today":
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
                elif period == "week":
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%a %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
                    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=2))
                else:
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
                    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=2))
                
                for ev_name, ev_ts in schedule_events:
                    ev_date = dt.datetime.fromtimestamp(ev_ts, tz=dt.timezone.utc)
                    color = '#FFD700' if 'Guild Party' in ev_name else '#FF6B6B' if 'Showdown' in ev_name else '#BB8FCE'
                    ax.axvline(x=ev_date, color=color, linestyle=':', linewidth=1, alpha=0.7)
                    y_top = max(y_values)
                    short_name = ev_name.replace(' (***', '(').replace('***)', ')')
                    ax.text(ev_date, y_top, short_name, rotation=90, fontsize=6, color=color, alpha=0.8,
                            verticalalignment='bottom', horizontalalignment='center')
                
                plt.xticks(rotation=45, color='white')
                plt.yticks(color='white')
                
                ax.legend(['Online Players', 'Trend'], loc='upper right', facecolor='#1a1a2e', edgecolor='white', labelcolor='white')
                plt.tight_layout()
                
                import io
                buf = io.BytesIO()
                plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
                buf.seek(0)
                plt.close()
                
                file = discord.File(buf, filename='stats_graph.png')
                
                peak_val = max(y_values)
                avg_val = round(sum(y_values) / len(y_values), 1)
                
                embed = discord.Embed(title=":bar_chart: Online Players", color=discord.Color.green())
                embed.add_field(name=":chart_with_upwards_trend: Peak", value=f"`{peak_val} players`", inline=True)
                embed.add_field(name=":chart_with_downwards_trend: Average", value=f"`{avg_val} players`", inline=True)
                embed.add_field(name=":bar_chart: Data Points", value=f"`{len(rows)}`", inline=True)
                embed.set_image(url="attachment://stats_graph.png")
                embed.set_footer(text=f"Time range: {period_labels.get(period, 'Custom')} | Data recorded every 1 minute")
                
                await interaction.followup.send(embed=embed, file=file)
            
            elif type == "online_by_region":
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT ts, snapshot_json FROM guild_player_snapshots WHERE ts >= ? ORDER BY ts ASC",
                        (start_ts,)
                    )
                    rows = await cursor.fetchall()
                
                if not rows:
                    await interaction.followup.send("❌ No snapshot data available for the selected time range.")
                    return
                
                region_labels = {
                    "": "Unknown", "CN": "CN (Mainland China)", "AS": "AS (Asia)",
                    "EU": "EU (Europe)", "JP": "JP (Japan)",
                    "KR": "KR (South Korea)", "NA": "NA (North America)",
                    "SA": "SA (South America)", "OC": "OC (Oceania)", "OTHER": "Other",
                }
                
                timestamps = []
                region_online_series = defaultdict(list)
                all_region_tags = set()
                
                for row in rows:
                    snapshot = json.loads(row['snapshot_json'])
                    ts = row['ts']
                    timestamps.append(ts)
                    for p in snapshot:
                        if p.get('is_online', False):
                            tag = str(p.get('oversea_tag', ''))
                            if tag == "NAW":
                                tag = "NA"
                            elif tag in ("SEA", "HMT"):
                                tag = "AS"
                            all_region_tags.add(tag)
                
                for row in rows:
                    snapshot = json.loads(row['snapshot_json'])
                    region_online = Counter()
                    for p in snapshot:
                        if p.get('is_online', False):
                            tag = str(p.get('oversea_tag', ''))
                            if tag == "NAW":
                                tag = "NA"
                            elif tag in ("SEA", "HMT"):
                                tag = "AS"
                            region_online[tag] += 1
                    for tag in all_region_tags:
                        region_online_series[tag].append(region_online.get(tag, 0))
                
                if not region_online_series:
                    await interaction.followup.send("❌ No online players found in the selected time range.")
                    return
                
                dates = [dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc) for ts in timestamps]
                sorted_regions = sorted(region_online_series.keys(), key=lambda tag: sum(region_online_series[tag]), reverse=True)
                
                region_colors = ['#3498DB', '#E74C3C', '#2ECC71', '#F39C12', '#9B59B6', '#1ABC9C', '#E67E22', '#ECF0F1', '#7F8C8D', '#2980B9', '#C0392B', '#27AE60', '#D35400']
                color_map = {tag: region_colors[i % len(region_colors)] for i, tag in enumerate(sorted_regions)}
                
                plt.style.use('dark_background')
                fig, ax = plt.subplots(figsize=(14, 7))
                ax.set_facecolor('#1a1a2e')
                fig.patch.set_facecolor('#1a1a2e')
                ax.grid(True, alpha=0.2, color='white')
                
                for tag in sorted_regions:
                    series = region_online_series[tag]
                    color = color_map[tag]
                    label = region_labels.get(tag, f"? {tag}")
                    ax.fill_between(dates, series, alpha=0.25, color=color)
                    ax.plot(dates, series, color=color, linewidth=1.5, marker='', linestyle='-', label=label)
                
                ax.set_xlabel('Time (GMT+8)', color='white', fontsize=12)
                ax.set_ylabel('Online Players', color='white', fontsize=12)
                ax.set_title(f'Online Players by Region Over Time - {period_labels.get(period, "Custom")}', color='white', fontsize=14, fontweight='bold')
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))
                
                if period == "today":
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
                elif period == "week":
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%a %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
                    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=2))
                else:
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
                    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=2))
                
                max_y = max(max(region_online_series[tag]) for tag in sorted_regions) if sorted_regions else 0
                for ev_name, ev_ts in schedule_events:
                    ev_date = dt.datetime.fromtimestamp(ev_ts, tz=dt.timezone.utc)
                    color = '#FFD700' if 'Guild Party' in ev_name else '#FF6B6B' if 'Showdown' in ev_name else '#BB8FCE'
                    ax.axvline(x=ev_date, color=color, linestyle=':', linewidth=1, alpha=0.7)
                    short_name = ev_name.replace(' (***', '(').replace('***)', ')')
                    ax.text(ev_date, max_y, short_name, rotation=90, fontsize=6, color=color, alpha=0.8,
                            verticalalignment='bottom', horizontalalignment='center')
                
                plt.xticks(rotation=45, color='white')
                plt.yticks(color='white')
                ax.legend(loc='upper left', bbox_to_anchor=(1.02, 1), facecolor='#1a1a2e', edgecolor='white', labelcolor='white', fontsize=9)
                plt.tight_layout(rect=[0, 0, 0.85, 1])
                
                import io
                buf = io.BytesIO()
                plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
                buf.seek(0)
                plt.close()
                
                file = discord.File(buf, filename='stats_graph.png')
                
                embed = discord.Embed(title=":globe_with_meridians: Online Players by Region Over Time", color=discord.Color.og_blurple())
                embed.add_field(name=":bar_chart: Data Points", value=f"`{len(rows)}`", inline=True)
                embed.add_field(name=":earth_asia: Regions Tracked", value=f"`{len(sorted_regions)}`", inline=True)
                embed.set_image(url="attachment://stats_graph.png")
                embed.set_footer(text=f"Time range: {period_labels.get(period, 'Custom')} | Data recorded every 1 minute")
                
                await interaction.followup.send(embed=embed, file=file)
            
            elif type == "liveness_gain":
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT ts, snapshot_json FROM guild_player_snapshots WHERE ts >= ? ORDER BY ts ASC",
                        (start_ts,)
                    )
                    rows = await cursor.fetchall()
                
                if not rows:
                    await interaction.followup.send("❌ No snapshot data available for the selected time range.")
                    return
                
                if len(rows) < 2:
                    await interaction.followup.send("❌ Need at least 2 snapshots to calculate liveness gain (data records every 1 minute).")
                    return
                
                sample_step = max(1, len(rows) // 200)
                sampled_rows = rows[::sample_step]
                if sampled_rows[-1] is not rows[-1]:
                    sampled_rows.append(rows[-1])
                
                timestamps = [row['ts'] for row in sampled_rows]
                dates = [dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc) for ts in timestamps]
                num_points = len(timestamps)
                
                baseline_snapshot = json.loads(sampled_rows[0]['snapshot_json'])
                all_player_nicknames = {}
                
                for p in baseline_snapshot:
                    pid = p.get('pid')
                    all_player_nicknames[pid] = p.get('nickname', 'Unknown')
                
                for row in sampled_rows[1:]:
                    snapshot = json.loads(row['snapshot_json'])
                    for p in snapshot:
                        pid = p.get('pid')
                        if pid and pid not in all_player_nicknames:
                            all_player_nicknames[pid] = p.get('nickname', 'Unknown')
                
                total_gains = {}
                for pid in list(all_player_nicknames.keys()):
                    last_lv = None
                    total_gain = 0
                    for row in sampled_rows:
                        snapshot = json.loads(row['snapshot_json'])
                        curr_lv = None
                        for p in snapshot:
                            if p.get('pid') == pid:
                                curr_lv = p.get('liveness', 0)
                                break
                        if curr_lv is None:
                            continue
                        if last_lv is not None:
                            diff = curr_lv - last_lv
                            if diff > 0:
                                total_gain += diff
                        last_lv = curr_lv
                    if total_gain >= 0:
                        total_gains[pid] = total_gain
                
                if not total_gains:
                    await interaction.followup.send("❌ No liveness gains detected in the selected time range.")
                    return
                
                sorted_players = sorted(total_gains.items(), key=lambda x: x[1], reverse=True)
                top_n = sorted_players[:10]
                top_pids = {pid for pid, _ in top_n}
                
                top_nicknames = {}
                for pid, _ in top_n:
                    top_nicknames[pid] = all_player_nicknames.get(pid, f"PID:{pid[:8]}")
                
                def compute_cumulative_gain(pid, sampled_rows, initial_lv=None):
                    series = []
                    last_lv = initial_lv
                    cumulative = 0
                    for row in sampled_rows:
                        snapshot = json.loads(row['snapshot_json'])
                        curr_lv = None
                        for p in snapshot:
                            if p.get('pid') == pid:
                                curr_lv = p.get('liveness', 0)
                                break
                        if curr_lv is None:
                            if series:
                                series.append(series[-1])
                            else:
                                series.append(0)
                            continue
                        if last_lv is not None:
                            diff = curr_lv - last_lv
                            if diff < -100:
                                cumulative = 0
                            elif diff > 0:
                                cumulative += diff
                        series.append(cumulative)
                        last_lv = curr_lv
                    return series
                
                cumulative_series = {}
                for pid in top_pids:
                    cumulative_series[pid] = compute_cumulative_gain(pid, sampled_rows)
                
                num_other_players = len(total_gains) - len(top_n)
                all_other_pids = [pid for pid in total_gains if pid not in top_pids]
                combined_series = [0] * num_points
                
                for pid in all_other_pids:
                    series = compute_cumulative_gain(pid, sampled_rows)
                    for idx in range(num_points):
                        combined_series[idx] += series[idx]
                
                plt.style.use('dark_background')
                fig, ax = plt.subplots(figsize=(14, 7))
                ax.set_facecolor('#1a1a2e')
                fig.patch.set_facecolor('#1a1a2e')
                ax.grid(True, alpha=0.2, color='white')
                
                top_colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA07A', '#98D8C8', '#F7DC6F', '#BB8FCE', '#85C1E9', '#F0B27A', '#82E0AA']
                
                for i, (pid, gain) in enumerate(top_n):
                    color = top_colors[i % len(top_colors)]
                    label = top_nicknames[pid]
                    ax.plot(dates, cumulative_series[pid], color=color, linewidth=2, marker='', linestyle='-', label=label)
                    ax.fill_between(dates, cumulative_series[pid], alpha=0.1, color=color)
                
                ax.plot(dates, combined_series, color='#7F8C8D', linewidth=1.5, marker='', linestyle='--', alpha=0.8, label=f'Everyone Else ({len(total_gains) - len(top_n)} players)')
                ax.fill_between(dates, combined_series, alpha=0.05, color='#7F8C8D')
                
                ax.set_xlabel('Time (GMT+8)', color='white', fontsize=12)
                ax.set_ylabel('Cumulative Liveness Gained', color='white', fontsize=12)
                ax.set_title(f'Liveness Gain Over Time - {period_labels.get(period, "Custom")}', color='white', fontsize=14, fontweight='bold')
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))
                
                if period == "today":
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
                elif period == "week":
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%a %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
                else:
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                    ax.xaxis.set_major_locator(mdates.HourLocator(interval=12))
                
                plt.xticks(rotation=45, color='white')
                plt.yticks(color='white')
                ax.legend(loc='upper left', bbox_to_anchor=(1.02, 1), facecolor='#1a1a2e', edgecolor='white', labelcolor='white', fontsize=9)
                plt.tight_layout(rect=[0, 0, 0.85, 1])
                
                import io
                buf = io.BytesIO()
                plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
                buf.seek(0)
                plt.close()
                
                file = discord.File(buf, filename='stats_graph.png')
                
                top_gainers_text = "\n".join(
                    f"🥇 {top_nicknames[pid]}: +{gain:,}" if i == 0 else
                    f"🥈 {top_nicknames[pid]}: +{gain:,}" if i == 1 else
                    f"🥉 {top_nicknames[pid]}: +{gain:,}" if i == 2 else
                    f"{i+1}. {top_nicknames[pid]}: +{gain:,}"
                    for i, (pid, gain) in enumerate(top_n)
                )
                
                total_guild_gain = sum(total_gains.values())
                
                embed = discord.Embed(title="🔥 Liveness Gain Over Time", color=discord.Color.orange())
                embed.add_field(name="📊 Data Points", value=f"`{len(rows)}`", inline=True)
                embed.add_field(name="🏆 Players Tracked", value=f"`{len(total_gains)}`", inline=True)
                embed.add_field(name="📈 Total Guild Gain", value=f"`+{total_guild_gain:,}`", inline=True)
                embed.add_field(name="🏅 Top Gainers", value=f"```{top_gainers_text}```", inline=False)
                embed.set_image(url="attachment://stats_graph.png")
                embed.set_footer(text=f"Time range: {period_labels.get(period, 'Custom')} | Each player's line starts from 0")
                
                await interaction.followup.send(embed=embed, file=file)
        
        except ImportError as e:
            await interaction.followup.send(f"❌ Missing dependency: `{e}`. Please ensure matplotlib and numpy are installed.")
        except Exception as e:
            logger.error(f"Failed to generate stats graph: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to generate graph: `{str(e)}`")

    @guild_group.command(name="info", description="Get detailed guild info with member activity stats and leader profiles")
    @admin_or_staff()
    @app_commands.describe(
        search="Optional guild name or numeric ID to search for (leave empty for our guild)"
    )
    async def guild_info(self, interaction: discord.Interaction, search: str = None):
        """Display comprehensive guild information including activity stats, league data, and leader profiles."""
        await interaction.response.defer()

        try:
            # Step 1: Resolve guild
            target_club_id = CLUB_ID
            target_hostnum = 10103
            guild_name_for_header = None

            if search:
                search_term = search.strip()
                if search_term.isdigit():
                    guild_lookup = await get_club_by_number_id(int(search_term))
                    if guild_lookup:
                        target_club_id = guild_lookup.get('club_id')
                        target_hostnum = guild_lookup.get('hostnum', 10103)
                else:
                    clubs = await get_club_by_name(search_term, limit=1)
                    if clubs and len(clubs) > 0:
                        target_club_id = clubs[0].get('club_id')
                        target_hostnum = clubs[0].get('hostnum', 10103)

            if not target_club_id:
                await interaction.followup.send("❌ Could not find the specified guild.")
                return

            # Step 2: Fetch guild data with all fields
            guild_data = await get_full_guild_info(target_club_id, hostnum=target_hostnum)

            if not guild_data or 'result' not in guild_data:
                await interaction.followup.send("❌ Failed to fetch guild data or guild not found.")
                return

            result = guild_data['result']
            base = result.get('base', {})
            members = result.get('members', {})
            play = result.get('play', {})

            # Step 3: Extract core info
            guild_name = base.get('name', 'Unknown Guild')
            guild_level = base.get('level', 0)
            member_count = members.get('member_num', 0)
            apprentice_count = members.get('apprentice_num', 0)
            create_ts = base.get('create_ts', 0)
            funds = base.get('fund', 0)
            total_fame = base.get('fame', 0)

            # Step 4: Ranked match + league stats
            pk_match = play.get('pk_match_info', {})
            ranked_match_score = pk_match.get('battle_score', 0)

            league_info = play.get('league_info', {}) or {}
            league_score = league_info.get('small_score', 0)
            league_rank = league_info.get('rank', 0)
            league_wins = league_info.get('win_count', 0)

            # Step 5: Identify leader and vice PIDs + number_ids
            member_list = members.get('members', {})
            leader_pid = None
            vice_pid = None
            for pid, member in member_list.items():
                post_list = member.get('post', [])
                if 1 in post_list:
                    leader_pid = pid
                if 2 in post_list:
                    vice_pid = pid

            # Step 6: Fetch ALL members' data for activity + leadership info
            all_pids = list(member_list.keys())
            bulk_data = await get_bulk_players_info(all_pids, fields=["base", "club"])

            leader_name = "None"
            leader_number_id = None
            vice_name = "None"
            vice_number_id = None
            active_members = []

            now_ts = int(discord.utils.utcnow().timestamp())
            seven_days_ago = now_ts - (7 * 24 * 3600)

            if bulk_data and bulk_data.get('code') == 0:
                players_result = bulk_data.get('result', {})

                for pid, player_data in players_result.items():
                    data_base = player_data.get('base', {})
                    data_club = player_data.get('club', {})

                    nickname = data_base.get('nickname', 'Unknown')
                    number_id = str(data_base.get('number_id', ''))
                    level = data_base.get('level', 0)
                    is_online = data_base.get('is_online', 0) == 1
                    logout_time = data_base.get('logout_time', 0) or 0
                    liveness = data_club.get('liveness', 0)

                    # Track leader/vice number_ids
                    if pid == leader_pid:
                        leader_name = nickname
                        leader_number_id = number_id
                    if pid == vice_pid:
                        vice_name = nickname
                        vice_number_id = number_id

                    # Check if active in last 7 days
                    if is_online or logout_time >= seven_days_ago:
                        active_members.append({
                            'pid': pid,
                            'nickname': nickname,
                            'level': level,
                            'number_id': number_id,
                            'is_online': is_online,
                            'logout_time': logout_time,
                            'liveness': liveness,
                        })

            # Step 7: Calculate average activity
            online_count_7d = len(active_members)
            avg_activity = 0
            if active_members:
                total_liveness = sum(m.get('liveness', 0) for m in active_members)
                avg_activity = total_liveness // len(active_members)

            # Step 7b: Compute inactive lists by week with schedule-aware validity
            this_week_start = self.get_weekly_reset_ts()
            last_week_start = this_week_start - 7 * 24 * 3600

            inactive_this_week = []
            inactive_last_week = []

            for pid, data in players_result.items():
                data_club = data.get('club', {})
                data_base = data.get('base', {})
                nickname = data_base.get('nickname', 'Unknown')
                number_id = str(data_base.get('number_id', ''))
                level = data_base.get('level', 0)
                is_online = data_base.get('is_online', 0) == 1
                logout_time = data_base.get('logout_time', 0) or 0
                liveness = data_club.get('liveness', 0)
                last_liveness = data_club.get('last_liveness', 0)

                valid_this_week = is_online or logout_time >= this_week_start
                valid_last_week = logout_time >= last_week_start

                # Last week score: last_liveness if data updated this week, else liveness
                last_week_score = last_liveness if valid_this_week else liveness

                if valid_this_week and liveness == 0:
                    inactive_this_week.append({
                        'pid': pid, 'nickname': nickname, 'level': level,
                        'number_id': number_id, 'is_online': is_online,
                        'logout_time': logout_time, 'liveness': liveness,
                    })
                if valid_last_week and last_week_score == 0:
                    inactive_last_week.append({
                        'pid': pid, 'nickname': nickname, 'level': level,
                        'number_id': number_id, 'is_online': is_online,
                        'logout_time': logout_time, 'liveness': last_week_score,
                    })

            # Step 8: Extract guild area and build the view
            guild_area = base.get('classify_info', {}).get('area', None)

            view = GuildInfoView(
                cog=self,
                guild_name=guild_name,
                guild_level=guild_level,
                member_count=member_count,
                apprentice_count=apprentice_count,
                create_ts=create_ts,
                funds=funds,
                total_fame=total_fame,
                ranked_match_score=ranked_match_score,
                league_score=league_score,
                league_rank=league_rank,
                league_wins=league_wins,
                leader_name=leader_name,
                leader_number_id=leader_number_id if leader_number_id else None,
                vice_name=vice_name,
                vice_number_id=vice_number_id if vice_number_id else None,
                online_count_7d=online_count_7d,
                total_members=member_count + apprentice_count,
                avg_activity=avg_activity,
                active_members=active_members,
                inactive_this_week=inactive_this_week,
                inactive_last_week=inactive_last_week,
                guild_area=guild_area,
            )

            await interaction.followup.send(view=view)

        except Exception as e:
            logger.error(f"Guild info command failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to load guild info: `{str(e)}`")

    @guild_group.command(name="region", description="Sort and display guild members grouped by region (admin only)")
    @admin_or_staff()
    @app_commands.describe(name="Optional guild name to search for (leave empty to use our guild)")
    async def guild_region(self, interaction: discord.Interaction, name: str = None):
        await interaction.response.defer()
        if name:
            try:
                search_term = name.strip()
                if not search_term:
                    await interaction.followup.send("❌ Please provide a valid guild name")
                    return
                clubs = await get_club_by_name(search_term, limit=5)
                if not clubs or len(clubs) == 0:
                    embed = discord.Embed(title="❌ No Results", color=discord.Color.red())
                    await interaction.followup.send(embed=embed)
                    return
                club_ids = [club.get('club_id') for club in clubs]
                hostnums = [club.get('hostnum', 10103) for club in clubs]
                guild_infos = await get_club_brief_info_batch(club_ids, hostnums) or []
                guild_info_map = {}
                for info in guild_infos:
                    info_club_id = info.get('club_id')
                    if info_club_id:
                        guild_info_map[info_club_id] = info
                valid_clubs = []
                valid_infos = []
                for club in clubs:
                    cid = club.get('club_id')
                    if cid in guild_info_map:
                        valid_clubs.append(club)
                        valid_infos.append(guild_info_map[cid])
                if len(valid_clubs) == 0:
                    embed = discord.Embed(title="❌ No Active Guilds Found", color=discord.Color.red())
                    await interaction.followup.send(embed=embed)
                    return
                # Build result text for V2 header
                result_lines = []
                for idx, info in enumerate(valid_infos, 1):
                    guild_name = info.get('base', {}).get('name', 'Unknown')
                    member_num = info.get('members', {}).get('member_num', '?')
                    apprentice_num = info.get('members', {}).get('apprentice_num', '?')
                    result_lines.append(f"**{idx}.** **{guild_name}** — 👥 `{member_num}` 🎓 `{apprentice_num}`")
                view = GuildRegionSelectView(valid_clubs, valid_infos, self)
                await interaction.followup.send(content=None, embed=None, view=view)
            except Exception as e:
                logger.error(f"Guild region search failed: {str(e)}", exc_info=True)
                embed = discord.Embed(title="❌ Search Failed", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
            return

        try:
            guild_data = await get_full_guild_info(CLUB_ID)
            if not guild_data or 'result' not in guild_data:
                await interaction.followup.send("❌ Failed to fetch guild data")
                return
            result = guild_data['result']
            members = result.get('members', {}).get('members', {})
            all_uids = list(members.keys())
            if not all_uids:
                await interaction.followup.send("❌ No members found in guild")
                return
            bulk_data = await get_bulk_players_info(all_uids, fields=["base"])
            if not bulk_data or bulk_data.get('code') != 0:
                await interaction.followup.send("❌ Failed to fetch player info")
                return
            players_result = bulk_data.get('result', {})
            tag_map = {"": "Unknown", "CN": "🇨🇳 CN (Mainland China)", "AS": "🌏 AS (Asia)", "EU": "🇪🇺 EU (Europe)", "HMT": "🇭🇰 HMT (Hong Kong/Macau/Taiwan)", "JP": "🇯🇵 JP (Japan)", "KR": "🇰🇷 KR (South Korea)", "NA": "🇺🇸 NA (North America)", "NAW": "🌎 NAW (North America West)", "SA": "🌎 SA (South America)", "SEA": "🌏 SEA (Southeast Asia)", "OC": "🌏 OC (Oceania)", "OTHER": "🌍 Other"}
            def get_region_label(tag): return tag_map.get(tag, f"❓ {tag}")
            regions = defaultdict(list)
            for pid, player_data in players_result.items():
                base = player_data.get('base', {})
                regions[str(base.get('oversea_tag', ''))].append({
                    'pid': pid, 'number_id': str(base.get('number_id', '')), 'nickname': base.get('nickname', 'Unknown'),
                    'level': base.get('level', 0), 'is_online': base.get('is_online', 0) == 1, 'oversea_tag': str(base.get('oversea_tag', '')),
                })
            guild_name = result.get('base', {}).get('name', 'Our Guild')
            view = GuildRegionSummaryView(guild_name, regions, tag_map, self)
            await interaction.followup.send(content=None, embed=None, view=view)
        except Exception as e:
            logger.error(f"Guild region command failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to display guild regions: `{str(e)}`")

    def get_weekly_reset_ts(self):
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))

        # Monday of the current week at 5:00 AM GMT+8
        monday_5am = (now - datetime.timedelta(days=now.weekday())).replace(hour=5, minute=0, second=0, microsecond=0)
        if now < monday_5am:
            monday_5am -= datetime.timedelta(days=7)

        return int(monday_5am.timestamp())
    
    @guild_group.command(name="inactive", description="List guild members that did not meet the minimum activity requirement in the past week (admin only)")
    @admin_or_staff()
    async def guild_inactive(self, interaction: discord.Interaction, point_threshold: int = 1500):
        await interaction.response.defer()
        try:
            guild_data = await get_full_guild_info(CLUB_ID)
            if not guild_data or 'result' not in guild_data:
                await interaction.followup.send("❌ Failed to fetch guild data")
                return
            result = guild_data.get('result', {})
            members = result.get('members', {})
            member_list = members.get('members', {})
            all_pids = list(member_list.keys())

            bulk_data = await get_bulk_players_info(all_pids, fields=["club", "base"])
            if bulk_data and bulk_data.get('code') == 0:
                players_result = bulk_data.get('result', {})
                logger.debug(f"Fetched club info for {len(players_result)} players in bulk")
                inactive_last_week = []
                inactive_this_week = []
                for pid, data in players_result.items():
                    data_club = data.get('club', {})
                    data_base = data.get('base', {})
                    nickname = data_base.get('nickname', 'Unknown')
                    number_id = data_base.get('number_id', 0)
                    is_online = data_base.get('is_online', 0)
                    logout_time = data_base.get('logout_time', 0)
                    post = data_club.get('post', [])
                    # Check if player has absent role (10005)
                    if post and isinstance(post, list):
                        has_absent_role = (True if 10005 in post else False)
                        logger.debug(f"Player {nickname} (PID: {pid}) has post data, absent role: {has_absent_role}")
                    else:
                        has_absent_role = False

                    # If the player is online or has logged out within this week (using day1 5am gmt+8 as cutoff)
                    # find reset time for the current week (every Monday 5am GMT+8) and compare with logout_time to determine if they were active last week or this week
                    
                    if is_online or logout_time >= self.get_weekly_reset_ts():
                        liveness = data_club.get('liveness', 0)
                        last_liveness = data_club.get('last_liveness', 0)
                        if last_liveness < point_threshold:
                            inactive_last_week.append((nickname, last_liveness, logout_time, number_id, has_absent_role))
                        if liveness < point_threshold:
                            inactive_this_week.append((nickname, liveness, logout_time, number_id, has_absent_role))
                    # elif check if logout last week but not this week
                    elif logout_time >= self.get_weekly_reset_ts() - 7*24*3600:
                        # If they logged out last week but not this week, last_liveness = liveness, and their liveness = 0
                        last_liveness = data_club.get('liveness', 0)
                        liveness = 0
                        if last_liveness < point_threshold:
                            inactive_last_week.append((nickname, last_liveness, logout_time, number_id, has_absent_role))
                        if liveness < point_threshold:
                            inactive_this_week.append((nickname, liveness, logout_time, number_id, has_absent_role))
                    else:
                        # If they logged out more than 2 weeks ago, we can consider them inactive for both weeks
                        last_liveness = 0
                        liveness = 0
                        if last_liveness < point_threshold:
                            inactive_last_week.append((nickname, last_liveness, logout_time, number_id, has_absent_role))
                        if liveness < point_threshold:
                            inactive_this_week.append((nickname, liveness, logout_time, number_id, has_absent_role))
                inactive_last_week.sort(key=lambda x: x[1], reverse=True)
                inactive_this_week.sort(key=lambda x: x[1], reverse=True)
                # use container to send the list, pageinate if necessary


                view = Inactive(inactive_last_week, inactive_this_week, point_threshold)
                await interaction.followup.send(content="", view=view)


        except Exception as e:
            logger.error(f"Failed to fetch guild or player data: {str(e)}", exc_info=True)
            await interaction.followup.send("❌ Failed to fetch guild or player data")
            return

    async def _build_opponent_reminder_view(
        self,
        ping_mention: Optional[str] = None,
    ) -> "OpponentGuildView":
        """Build a Components V2 view describing the current opponent guild from our league data.

        Steps (mirrors test/test_new_get_club_info._api.py):
          1. Fetch our own guild info (CLUB_ID) and read play.league_info.duishou
             to find the opponent's club_id + club_host.
          2. Fetch the opponent's full guild info with that hostnum.
          3. Resolve leader/vice-leader nicknames and count online members.
          4. Return a populated OpponentGuildView (or a graceful fallback view on failure).

        The optional ``ping_mention`` (a pre-rendered role mention like ``<@&123>``)
        is embedded inside the V2 layout, since ``channel.send(content=...)`` is
        rejected on IS_COMPONENTS_V2 messages.
        """
        pulled_at_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

        # 1. Our guild data
        our_data = await get_full_guild_info(CLUB_ID)
        if not our_data or 'result' not in our_data:
            logger.warning("Opponent reminder: failed to fetch our guild data")
            return OpponentGuildView(
                title="⚔️ Opponent Guild",
                sections=[TextDisplay("❌ Failed to fetch our guild data.")],
                accent=OpponentGuildView.ACCENT_ERROR,
                pulled_at_ts=pulled_at_ts,
                error=True,
                ping_mention=ping_mention,
            )

        play = our_data['result'].get('play', {}) or {}
        league = play.get('league_info', {}) or {}
        duishou = league.get('duishou') or {}

        # 2. No opponent this period — still return a (grey) view so we know the check ran
        if not duishou or not duishou.get('club_id'):
            return OpponentGuildView(
                title="⚔️ Opponent Guild",
                sections=[TextDisplay("No current opponent found (no league/showdown data).")],
                accent=OpponentGuildView.ACCENT_GREY,
                pulled_at_ts=pulled_at_ts,
                no_opponent=True,
                ping_mention=ping_mention,
            )

        opponent_club_id = duishou['club_id']
        opponent_hostnum = duishou.get('club_host', 10103)

        # 3. Opponent guild data
        opp_data = await get_full_guild_info(opponent_club_id, hostnum=opponent_hostnum)
        if not opp_data or 'result' not in opp_data:
            logger.warning(f"Opponent reminder: failed to fetch opponent guild {opponent_club_id}")
            return OpponentGuildView(
                title="⚔️ Opponent Guild",
                sections=[TextDisplay("Found an opponent but failed to load their guild data.")],
                accent=OpponentGuildView.ACCENT_ERROR,
                pulled_at_ts=pulled_at_ts,
                error=True,
                ping_mention=ping_mention,
            )

        result = opp_data['result']
        base = result.get('base', {})
        members = result.get('members', {})
        member_list = members.get('members', {})
        create_ts = base.get('create_ts', 0)

        # 4. Resolve leader & vice-leader nicknames
        leader_pid = None
        vice_leader_pid = None
        for pid, member in member_list.items():
            post_list = member.get('post', [])
            if 1 in post_list:
                leader_pid = pid
            if 2 in post_list:
                vice_leader_pid = pid

        leader_name = "None"
        vice_leader_name = "None"
        pids_to_fetch = [pid for pid in (leader_pid, vice_leader_pid) if pid]
        if pids_to_fetch:
            bulk = await get_bulk_players_info(pids_to_fetch, fields=["base"])
            if bulk and bulk.get('code') == 0:
                players = bulk.get('result', {})
                if leader_pid in players:
                    leader_name = players[leader_pid].get('base', {}).get('nickname', 'Unknown')
                if vice_leader_pid in players:
                    vice_leader_name = players[vice_leader_pid].get('base', {}).get('nickname', 'Unknown')

        # 5. Count online members
        online = 0
        all_pids = list(member_list.keys())
        if all_pids:
            bulk = await get_bulk_players_info(all_pids, fields=["base"])
            if bulk and bulk.get('code') == 0:
                for pid, pdata in bulk.get('result', {}).items():
                    if pdata.get('base', {}).get('is_online', 0) == 1:
                        online += 1

        # 6. Build the V2 view — grouped TextDisplay blocks
        creation_value = f"<t:{create_ts}:R>" if create_ts else "Unknown"
        member_total = members.get('member_num', 0)
        guild_name = base.get('name', 'Unknown')
        level = base.get('level', 0)
        total_fame = base.get('fame', 0)
        week_fame = base.get('week_fame', 0)

        identity_text = (
            f"📛 **Name:** {guild_name}\n"
            f"⭐ **Level:** {level}  👥 **Members:** {member_total}/100"
        )
        activity_text = (
            f"📈 **Total Fame:** {total_fame:,}\n"
            f"🔥 **Weekly Activity:** {week_fame:,}"
        )
        leadership_text = (
            f"👑 **Guild Leader:** {leader_name}\n"
            f"⚔️ **Vice Leader:** {vice_leader_name}\n"
            f"🟢 **Online Now:** {online}/{member_total}"
        )

        sections = [
            TextDisplay(identity_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(activity_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(leadership_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(f"📅 **Created:** {creation_value}"),
        ]

        return OpponentGuildView(
            title=f"⚔️ Opponent Guild — {guild_name}",
            sections=sections,
            accent=OpponentGuildView.ACCENT_RED,
            pulled_at_ts=pulled_at_ts,
            ping_mention=ping_mention,
        )

    @guild_group.command(name="league", description="Show the current opponent guild from our league/showdown data")
    async def guild_league(self, interaction: discord.Interaction):
        """Test command: fetch and display the current opponent guild info.

        This uses the same helper that the scheduled Sunday/Monday 8 AM GMT+8
        reminder will use, so the output here is exactly what the reminder will post.
        """
        await interaction.response.defer()
        try:
            view = await self._build_opponent_reminder_view()
            await interaction.followup.send(view=view)
        except Exception as e:
            logger.error(f"Guild league command failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to load opponent data: `{e}`")

    @tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=GMT8_TZ))
    async def gvg_league_notice_task(self):
        """Post the opponent-guild V2 view to GVG_LEAGUE_NOTICE_CHANNEL_ID, pinging
        GVG_PING_ROLE_ID. Fires daily at 8 AM GMT+8; only sends on Sunday and
        Monday (the days flanking the typical GvG weekend). Silently skips
        if there is no current opponent (no `duishou` data).
        """
        try:
            now_gmt8 = datetime.datetime.now(GMT8_TZ)
            # weekday(): Mon=0 ... Sun=6
            if now_gmt8.weekday() not in (0, 6):
                return

            channel = self.bot.get_channel(settings.GVG_LEAGUE_NOTICE_CHANNEL_ID)
            if not channel:
                logger.error(
                    f"GvG league notice channel {settings.GVG_LEAGUE_NOTICE_CHANNEL_ID} not found"
                )
                return

            # Resolve ping role (optional — silently skip the mention if missing)
            ping_role = None
            if getattr(settings, "GVG_PING_ROLE_ID", None):
                ping_role = (
                    channel.guild.get_role(settings.GVG_PING_ROLE_ID)
                    if channel.guild else None
                )

            # The role mention has to be carried inside the V2 layout, not
            # via channel.send(content=...) — Components V2 messages reject
            # the `content` field.
            view = await self._build_opponent_reminder_view(
                ping_mention=ping_role.mention if ping_role else None,
            )

            # If there's no current opponent, silently skip the send + ping.
            if view.no_opponent:
                logger.info(
                    "GvG league notice skipped: no current opponent (no duishou data)."
                )
                return

            await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    roles=[ping_role] if ping_role else []
                ),
            )
            logger.info(
                f"✅ GvG league notice sent to channel {channel.id} "
                f"(weekday={now_gmt8.weekday()})"
            )
        except Exception as e:
            logger.error(f"GvG league notice task failed: {e}", exc_info=True)

    @gvg_league_notice_task.before_loop
    async def before_gvg_league_notice(self):
        await self.bot.wait_until_ready()


class OpponentGuildView(LayoutView):
    """Components V2 LayoutView showing the current opponent guild profile.

    All copy uses TextDisplay blocks inside a single Container. There is no
    `embed`/`content` — this is a pure V2 message. The footer only carries a
    human-readable "pulled at" timestamp, never internal IDs/hostnums.
    """

    ACCENT_RED = 0xE74C3C
    ACCENT_GREY = 0x95A5A6
    ACCENT_ERROR = 0xC0392B

    def __init__(
        self,
        title: str,
        sections: list,
        accent: int,
        pulled_at_ts: int,
        error: bool = False,
        no_opponent: bool = False,
        ping_mention: Optional[str] = None,
    ):
        super().__init__(timeout=180 if not error else None)
        # Flags used by the scheduled gvg_league_notice_task to decide
        # whether to ping / send at all.
        self.error = error
        self.no_opponent = no_opponent

        # In Components V2 messages, `content` is not allowed (IS_COMPONENTS_V2
        # flag). The role ping therefore has to be carried inside the layout
        # as a TextDisplay rather than via channel.send(content=...).
        inner_items: list = []
        if ping_mention:
            inner_items.append(TextDisplay(ping_mention))
        inner_items.extend([
            TextDisplay(f"# {title}"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ])
        inner_items.extend(sections)
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        inner_items.append(TextDisplay(f"*Pulled: <t:{pulled_at_ts}:R>*"))

        container = Container(*inner_items, accent_color=accent)
        self.add_item(container)


class GuildProfileView(LayoutView):
    """Components V2 LayoutView for displaying a guild profile (name, level, members, leadership, etc.).

    Used by guild_search, guild_search_name, and the selection views.
    """
    ACCENT_BLURPLE = 0x5865F2

    def __init__(
        self,
        guild_name: str,
        guild_level: int,
        member_count: int,
        member_max: int = 100,
        create_ts: int = 0,
        funds: int = 0,
        total_fame: int = 0,
        week_fame: int = 0,
        gvg_points: int = 0,
        leader_name: str = "None",
        vice_leader_name: str = "None",
        online_count: int = 0,
        announcement: str = None,
        timeout: int = 180,
    ):
        super().__init__(timeout=timeout)

        inner_items = []

        # Title + Name
        inner_items.append(TextDisplay(f"# 🏰 Guild Profile\n\n📛 **Name:** __**{guild_name}**__"))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Core stats
        inner_items.append(TextDisplay(
            f"⭐ **Level:** __{guild_level}__    👥 **Members:** __{member_count}/{member_max}__\n"
            f"📅 **Created:** <t:{create_ts}:R>" if create_ts else f"⭐ **Level:** __{guild_level}__    👥 **Members:** __{member_count}/{member_max}__"
        ))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Finances
        inner_items.append(TextDisplay(
            f"💰 **Funds:** __{funds:,}__    📈 **Fame:** __{total_fame:,}__\n"
            f"🔥 **Weekly:** __{week_fame:,}__    🏆 **Ranked:** __{gvg_points:,}__"
        ))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Leadership
        inner_items.append(TextDisplay(
            f"👑 **Leader:** __{leader_name}__    ⚔️ **Vice Leader:** __{vice_leader_name}__\n"
            f"🟢 **Online Now:** __{online_count}/{member_count}__"
        ))

        # Announcement
        if announcement and announcement.strip():
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
            inner_items.append(TextDisplay(f"📢 **Announcement:** {announcement}"))

        container = Container(*inner_items, accent_color=self.ACCENT_BLURPLE)
        self.add_item(container)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class GuildDetailView(LayoutView):
    """Components V2 LayoutView for showing a detail page with a back button.

    Generic re-usable view: shows a title, body text, and a back button
    that restores the previous view.
    """

    def __init__(self, title: str, body: str, accent: int, back_view, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.back_view = back_view

        inner_items = [
            TextDisplay(f"# {title}\n\n{body}"),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        back_row = ActionRow()
        back_btn = discord.ui.Button(label="🔙 Back", style=discord.ButtonStyle.secondary, custom_id="guild_detail_back")
        back_btn.callback = self._handle_back
        back_row.add_item(back_btn)
        inner_items.append(back_row)

        container = Container(*inner_items, accent_color=accent)
        self.add_item(container)

    async def _handle_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.edit_original_response(view=self.back_view)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True


class GuildInfoView(LayoutView):
    """Components V2 LayoutView for /guild info — comprehensive guild profile with active member tracking."""

    ITEMS_PER_PAGE = 10
    ACCENT = 0x5865F2

    def __init__(
        self,
        cog,
        guild_name: str,
        guild_level: int,
        member_count: int,
        apprentice_count: int,
        create_ts: int,
        funds: int,
        total_fame: int,
        ranked_match_score: int,
        league_score: int,
        league_rank: int,
        league_wins: int,
        leader_name: str,
        leader_number_id: str = None,
        vice_name: str = None,
        vice_number_id: str = None,
        online_count_7d: int = 0,
        total_members: int = 0,
        avg_activity: int = 0,
        active_members: list = None,
        inactive_this_week: list = None,
        inactive_last_week: list = None,
        guild_area: str = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_name = guild_name
        self.leader_name = leader_name
        self.leader_number_id = leader_number_id
        self.vice_name = vice_name
        self.vice_number_id = vice_number_id
        self.online_count_7d = online_count_7d
        self.total_members = total_members
        self.avg_activity = avg_activity

        # Active members data: list of dicts with keys:
        # nickname, level, number_id, is_online, logout_time, liveness
        self.active_members = active_members or []
        self.inactive_this_week = inactive_this_week or []
        self.inactive_last_week = inactive_last_week or []

        # Pagination + sort state
        self.page = 0
        self.sort_by = "points_desc"  # points_desc, points_asc, name_az, logout_newest, logout_oldest
        self.display_mode = "active"  # active, inactive_this_week, inactive_last_week

        # Build the overview — combine text into fewer TextDisplays to stay under 40-child limit
        self._identity_text = (
            f"# 🏰 Guild Info — {guild_name}\n\n"
            f"📛 **Name:** __**{guild_name}**__\n"
            f"⭐ **Level:** __{guild_level}__    👥 **Members:** __{member_count}/100__    🎓 **Apps:** __{apprentice_count}__\n"
            + (f"🌍 **Area:** __{guild_area}__\n" if guild_area else "")
            + (f"📅 **Created:** <t:{create_ts}:R>" if create_ts else "📅 **Created:** Unknown")
        )

        self._stats_text = (
            f"💰 **Funds:** __{funds:,}__    📈 **Prosperity:** __{total_fame:,}__\n"
            f"🏆 **Ranked Match:** __{ranked_match_score:,}__    ⚔️ **League:** __{league_score:,}__\n"
            f"⚔️ **League Rank:** __{league_rank:,}__    **Wins:** __{league_wins:,}__"
        )

        # Compute richer activity stats
        zero_activity_count = 0
        max_activity = 0
        median_activity = 0
        if active_members:
            liveness_values = sorted([m.get('liveness', 0) for m in active_members])
            zero_activity_count = sum(1 for v in liveness_values if v == 0)
            max_activity = liveness_values[-1] if liveness_values else 0
            n = len(liveness_values)
            median_activity = (
                liveness_values[n // 2]
                if n % 2 == 1
                else (liveness_values[n // 2 - 1] + liveness_values[n // 2]) // 2
            )

        self._activity_summary_text = (
            f"📅 **Active (last 7d):** __{online_count_7d}/{total_members}__\n"
            f"💤 **Zero Activity:** __{zero_activity_count}__\n"
            f"📊 **Avg:** __{avg_activity:,}__ pts    "
            f"📈 **Median:** __{median_activity:,}__ pts    "
            f"🏆 **Peak:** __{max_activity:,}__ pts"
        )

        self.inner_items = [
            TextDisplay(self._identity_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(self._stats_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(self._activity_summary_text),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        # Leader/Vice — Section with button accessory
        if leader_number_id:
            leader_btn = Button(
                label=f"🔍 View Profile",
                style=discord.ButtonStyle.secondary,
                custom_id="guild_info_view_leader",
            )
            leader_btn.callback = self._handle_view_leader
            leader_section = Section(
                TextDisplay(f"👑 **Leader:** __{leader_name}__"),
                accessory=leader_btn,
            )
            self.inner_items.append(leader_section)
        else:
            self.inner_items.append(TextDisplay(f"👑 **Leader:** __{leader_name}__"))

        if vice_number_id:
            vice_btn = Button(
                label=f"🔍 View Profile",
                style=discord.ButtonStyle.secondary,
                custom_id="guild_info_view_vice",
            )
            vice_btn.callback = self._handle_view_vice
            vice_section = Section(
                TextDisplay(f"⚔️ **Vice:** __{vice_name or 'None'}__"),
                accessory=vice_btn,
            )
            self.inner_items.append(vice_section)
            self.inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        else:
            self.inner_items.append(TextDisplay(f"⚔️ **Vice:** __{vice_name or 'None'}__"))
            self.inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # View toggle buttons
        toggle_row = ActionRow()
        active_btn = Button(
            style=discord.ButtonStyle.primary if self.display_mode == "active" else discord.ButtonStyle.secondary,
            label=f"Active (7d)",
            custom_id="guild_info_mode_active",
            disabled=self.display_mode == "active",
        )
        active_btn.callback = self._handle_mode_active
        toggle_row.add_item(active_btn)

        inactive_this_week_btn = Button(
            style=discord.ButtonStyle.primary if self.display_mode == "inactive_this_week" else discord.ButtonStyle.secondary,
            label=f"Inactive This Week ({len(self.inactive_this_week)})",
            custom_id="guild_info_mode_this_week",
            disabled=self.display_mode == "inactive_this_week",
        )
        inactive_this_week_btn.callback = self._handle_mode_this_week
        toggle_row.add_item(inactive_this_week_btn)

        inactive_last_week_btn = Button(
            style=discord.ButtonStyle.primary if self.display_mode == "inactive_last_week" else discord.ButtonStyle.secondary,
            label=f"Inactive Last Week ({len(self.inactive_last_week)})",
            custom_id="guild_info_mode_last_week",
            disabled=self.display_mode == "inactive_last_week",
        )
        inactive_last_week_btn.callback = self._handle_mode_last_week
        toggle_row.add_item(inactive_last_week_btn)
        self.inner_items.append(toggle_row)
        self.inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Active member list header
        self.inner_items.append(TextDisplay(f"### Active Members (last 7 days) — sorted by activity ▼"))

        # Build sort + pagination controls (also adds the container to the view)
        self._rebuild_member_list()

    def _get_sorted_members(self):
        """Return the current member list sorted by current sort criterion."""
        if self.display_mode == "inactive_this_week":
            members = list(self.inactive_this_week)
        elif self.display_mode == "inactive_last_week":
            members = list(self.inactive_last_week)
        else:
            members = list(self.active_members)

        if self.sort_by == "points_desc":
            members.sort(key=lambda m: m.get('liveness', 0), reverse=True)
        elif self.sort_by == "points_asc":
            members.sort(key=lambda m: m.get('liveness', 0))
        elif self.sort_by == "name_az":
            members.sort(key=lambda m: m.get('nickname', '').lower())
        elif self.sort_by == "logout_newest":
            members.sort(key=lambda m: m.get('logout_time', 0), reverse=True)
        elif self.sort_by == "logout_oldest":
            members.sort(key=lambda m: m.get('logout_time', 0))
        return members

    def _rebuild_member_list(self):
        """Build the initial member list page. Called from __init__."""
        self._rebuild_page()

    def _rebuild_page(self):
        """Rebuild the entire GuildInfoView layout from scratch."""
        sorted_members = self._get_sorted_members()
        total_pages = max(1, (len(sorted_members) + self.ITEMS_PER_PAGE - 1) // self.ITEMS_PER_PAGE)
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * self.ITEMS_PER_PAGE
        end = start + self.ITEMS_PER_PAGE
        page_items = sorted_members[start:end]

        # Build member lines
        member_lines = []
        for m in page_items:
            online_icon = "🟢" if m.get('is_online') else "⚫"
            nick = m.get('nickname', 'Unknown')
            lv = m.get('level', 0)
            pts = m.get('liveness', 0)
            member_lines.append(f"{online_icon} **{nick}** Lv.{lv} | Activity: **{pts:,}** pts")
        members_text = "\n".join(member_lines) if member_lines else "*No active members found.*"

        # Header text
        if self.display_mode == "inactive_this_week":
            header_text = "### Inactive This Week — sorted by activity ▼"
        elif self.display_mode == "inactive_last_week":
            header_text = "### Inactive Last Week — sorted by activity ▼"
        else:
            header_text = "### Active Members (last 7 days) — sorted by activity ▼"

        # Build the full layout from scratch, including the toggle row before the header.
        new_inner = [
            TextDisplay(self._identity_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(self._stats_text),
            Separator(spacing=discord.SeparatorSpacing.small),
            TextDisplay(self._activity_summary_text),
            Separator(spacing=discord.SeparatorSpacing.small),
        ]

        # Leader / Vice sections (preserved from init)
        if self.leader_number_id:
            leader_btn = Button(
                label="🔍 View Profile",
                style=discord.ButtonStyle.secondary,
                custom_id="guild_info_view_leader",
            )
            leader_btn.callback = self._handle_view_leader
            leader_section = Section(
                TextDisplay(f"👑 **Leader:** __{self.leader_name}__"),
                accessory=leader_btn,
            )
            new_inner.append(leader_section)
        else:
            new_inner.append(TextDisplay(f"👑 **Leader:** __{self.leader_name}__"))

        if self.vice_number_id:
            vice_btn = Button(
                label="🔍 View Profile",
                style=discord.ButtonStyle.secondary,
                custom_id="guild_info_view_vice",
            )
            vice_btn.callback = self._handle_view_vice
            vice_section = Section(
                TextDisplay(f"⚔️ **Vice:** __{self.vice_name or 'None'}__"),
                accessory=vice_btn,
            )
            new_inner.append(vice_section)
            new_inner.append(Separator(spacing=discord.SeparatorSpacing.small))
        else:
            new_inner.append(TextDisplay(f"⚔️ **Vice:** __{self.vice_name or 'None'}__"))
            new_inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Toggle buttons
        toggle_row = ActionRow()
        active_btn = Button(
            style=discord.ButtonStyle.primary if self.display_mode == "active" else discord.ButtonStyle.secondary,
            label=f"Active (7d)",
            custom_id="guild_info_mode_active",
            disabled=self.display_mode == "active",
        )
        active_btn.callback = self._handle_mode_active
        toggle_row.add_item(active_btn)

        inactive_this_week_btn = Button(
            style=discord.ButtonStyle.primary if self.display_mode == "inactive_this_week" else discord.ButtonStyle.secondary,
            label=f"Inactive This Week ({len(self.inactive_this_week)})",
            custom_id="guild_info_mode_this_week",
            disabled=self.display_mode == "inactive_this_week",
        )
        inactive_this_week_btn.callback = self._handle_mode_this_week
        toggle_row.add_item(inactive_this_week_btn)

        inactive_last_week_btn = Button(
            style=discord.ButtonStyle.primary if self.display_mode == "inactive_last_week" else discord.ButtonStyle.secondary,
            label=f"Inactive Last Week ({len(self.inactive_last_week)})",
            custom_id="guild_info_mode_last_week",
            disabled=self.display_mode == "inactive_last_week",
        )
        inactive_last_week_btn.callback = self._handle_mode_last_week
        toggle_row.add_item(inactive_last_week_btn)
        new_inner.append(toggle_row)
        new_inner.append(Separator(spacing=discord.SeparatorSpacing.small))

        # Header
        new_inner.append(TextDisplay(header_text))

        # Member list text
        new_inner.append(TextDisplay(members_text))

        # Sort select
        sort_options = [
            discord.SelectOption(label="Points (High→Low)", value="points_desc", emoji="⬇️"),
            discord.SelectOption(label="Points (Low→High)", value="points_asc", emoji="⬆️"),
            discord.SelectOption(label="Name (A→Z)", value="name_az", emoji="🔤"),
            discord.SelectOption(label="Logout (Newest)", value="logout_newest", emoji="🆕"),
            discord.SelectOption(label="Logout (Oldest)", value="logout_oldest", emoji="⏰"),
        ]
        sort_row = ActionRow()
        sort_select = Select(
            placeholder=f"Sort: {self.sort_by}",
            options=sort_options,
            custom_id="guild_info_sort",
        )
        sort_select.callback = self._handle_sort
        sort_row.add_item(sort_select)
        new_inner.append(sort_row)

        # Pagination row
        nav_row = ActionRow()
        prev_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="◀ Prev",
            custom_id="guild_info_prev",
            disabled=self.page <= 0,
        )
        prev_btn.callback = self._handle_prev
        nav_row.add_item(prev_btn)

        page_label = Button(
            style=discord.ButtonStyle.secondary,
            label=f"Page {self.page + 1}/{total_pages}",
            custom_id="guild_info_page_label",
            disabled=True,
        )
        nav_row.add_item(page_label)

        next_btn = Button(
            style=discord.ButtonStyle.secondary,
            label="Next ▶",
            custom_id="guild_info_next",
            disabled=self.page >= total_pages - 1,
        )
        next_btn.callback = self._handle_next
        nav_row.add_item(next_btn)
        new_inner.append(nav_row)

        self.clear_items()
        container = Container(*new_inner, accent_color=self.ACCENT)
        self.add_item(container)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Container):
                for sub in child.children:
                    if isinstance(sub, ActionRow):
                        for item in sub.children:
                            if isinstance(item, discord.ui.Button):
                                item.disabled = True

    async def _handle_mode_active(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.display_mode = "active"
        self.page = 0
        self.sort_by = "points_desc"
        self._rebuild_page()
        await interaction.edit_original_response(view=self)

    async def _handle_mode_this_week(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.display_mode = "inactive_this_week"
        self.page = 0
        self.sort_by = "points_asc"
        self._rebuild_page()
        await interaction.edit_original_response(view=self)

    async def _handle_mode_last_week(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.display_mode = "inactive_last_week"
        self.page = 0
        self.sort_by = "points_asc"
        self._rebuild_page()
        await interaction.edit_original_response(view=self)

    async def _handle_sort(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected = interaction.data.get("values", ["points_desc"])
        self.sort_by = selected[0]
        self.page = 0
        self._rebuild_page()
        await interaction.edit_original_response(view=self)

    async def _handle_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            self._rebuild_page()
        await interaction.edit_original_response(view=self)

    async def _handle_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        sorted_members = self._get_sorted_members()
        total_pages = max(1, (len(sorted_members) + self.ITEMS_PER_PAGE - 1) // self.ITEMS_PER_PAGE)
        if self.page < total_pages - 1:
            self.page += 1
            self._rebuild_page()
        await interaction.edit_original_response(view=self)

    async def _show_player_profile(self, interaction: discord.Interaction, number_id: str, nickname_label: str):
        """Fetch player full data and show an ephemeral PlayerProfileView with all stats
        (masteries, attributes, combat, kongfu, guild, etc.) — matching /player search output."""
        await interaction.response.defer(ephemeral=True)

        try:
            # Use the shared helper on the cog (fix: was self._build_player_profile_view)
            view, files = await self.cog._build_player_profile_view(number_id, interaction, ephemeral=True)
            
            if not view:
                await interaction.followup.send(f"❌ Could not load profile for {nickname_label}", ephemeral=True)
                return
            
            view._original_message = None
            await interaction.followup.send(view=view, files=files, ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to show profile for {nickname_label} (number_id={number_id}): {e}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to load profile: `{str(e)}`", ephemeral=True)

    async def _handle_view_leader(self, interaction: discord.Interaction):
        await self._show_player_profile(interaction, self.leader_number_id, self.leader_name)

    async def _handle_view_vice(self, interaction: discord.Interaction):
        await self._show_player_profile(interaction, self.vice_number_id, self.vice_name)


from cogs.view_registry import register

# Self-register persistent views for restart recovery
register(GuildStatusBoard, cog=None, guild_name="", guild_level=0, member_count=0,
         apprentice_count=0, funds=0, total_fame=0, week_fame=0,
         gvg_points=0, online_count=0, weekly_leaderboard=[],
         pending_apps=0, now_ts=0, next_update_ts=0)


async def setup(bot: commands.Bot):
    await bot.add_cog(WWMCog(bot))
