import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import aiosqlite
import json
from collections import defaultdict
from deepdiff import DeepDiff

import settings
from utility.wwm import get_player_info, get_club_hostnums, get_full_guild_info, get_fashion_plan, get_club_by_name, get_bulk_players_info, get_club_brief_info_batch
from settings import WWM_UID, WWM_TOKEN, WWM_API_URL, logger, CLUB_ID, BASE_DIR

DB_PATH = BASE_DIR / "data" / "guild_verification.db"
SCHEDULE_DB_PATH = BASE_DIR / "data" / "schedule.db"


class OnlinePlayersButton(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
    
    @discord.ui.button(label="Check Online Players", style=discord.ButtonStyle.green, emoji="🟢", custom_id="online_players_button")
    async def check_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ALWAYS DEFER FIRST - Discord only gives 3 seconds to respond
        await interaction.response.defer(ephemeral=True)
        
        # Check if user has the guild member role
        GUILD_MEMBER_ROLE_ID = settings.GUILD_MEMBER_ROLE_ID
        
        member_role = discord.utils.get(interaction.user.roles, id=GUILD_MEMBER_ROLE_ID)
        if not member_role:
            await interaction.followup.send("❌ You are not guild member", ephemeral=True)
            return
        
        loading_msg = await interaction.followup.send("🔄 Getting player list...", ephemeral=True, wait=True)
        
        try:
            if not self.cog.last_guild_state:
                await loading_msg.edit(content="❌ Guild data not initialized, please try again shortly")
                return
            
            result = self.cog.last_guild_state.get('result', {})
            members = result.get('members', {})
            member_list = members.get('members', {})
            
            from utility.wwm import get_bulk_players_info
            all_pids = list(member_list.keys())
            bulk_data = get_bulk_players_info(all_pids, fields=["base"])
            
            online_player_names = []
            if bulk_data and bulk_data.get('code') == 0:
                players = bulk_data.get('result', {})
                for pid, player_data in players.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online_player_names.append(player_base.get('nickname', 'Unknown'))
            
            if online_player_names:
                lines = []
                lines.append(f"### 🟢 ONLINE PLAYERS ({len(online_player_names)}):")
                lines.append("```")
                for name in sorted(online_player_names):
                    lines.append(f"✅ {name}")
                lines.append("```")
                await loading_msg.edit(content="\n".join(lines))
            else:
                await loading_msg.edit(content="🔴 No players are currently online")
                
        except Exception as e:
            logger.error(f"Failed to fetch online players: {str(e)}")
            await loading_msg.edit(content="❌ Failed to retrieve online players list")


class GuildRegionSummaryView(discord.ui.View):
    """Summary view: shows 5 members per region with buttons to expand each region fully."""
    def __init__(self, guild_name: str, regions: dict, tag_map: dict, cog, original_embed=None):
        super().__init__(timeout=120)
        self.guild_name = guild_name
        self.regions = regions
        self.tag_map = tag_map
        self.cog = cog
        self.original_embed = original_embed

        sorted_tags = sorted(regions.keys(), key=lambda t: self._region_label(t))

        for idx, tag in enumerate(sorted_tags):
            label = f"{self._region_label(tag)} ({len(regions[tag])})"
            button = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"region_detail_{idx}"
            )
            button.callback = self._make_detail_callback(tag)
            self.add_item(button)

    def _region_label(self, tag):
        return self.tag_map.get(tag, f"❓ {tag}")

    def _build_summary_embed(self):
        sorted_tags = sorted(self.regions.keys(), key=lambda t: self._region_label(t))
        total_members = sum(len(m) for m in self.regions.values())

        embed = discord.Embed(
            title=f"🌍 {self.guild_name} — Members by Region",
            color=discord.Color.og_blurple()
        )
        embed.description = f"**Total members:** {total_members}  |  **Regions found:** {len(sorted_tags)}" + \
                            "\n*Click a region button below to see full list*"

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

            embed.add_field(
                name=f"{region_label}  ({len(member_list)} members, 🟢 {online_count} online)",
                value=f"```{preview_text}```",
                inline=False
            )
        return embed

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

            members_text = "\n".join(lines)

            embed = discord.Embed(
                title=f"🌍 {region_label} — {self.guild_name}",
                description=f"**{len(members)} members** | 🟢 {online_count} online",
                color=discord.Color.og_blurple()
            )

            chunk_size = 950
            chunks = [members_text[i:i+chunk_size] for i in range(0, len(members_text), chunk_size)]
            for i, chunk in enumerate(chunks):
                embed.add_field(
                    name=f"📋 Members (part {i+1}/{len(chunks)})" if len(chunks) > 1 else "📋 Members",
                    value=f"```{chunk}```",
                    inline=False
                )

            back_view = discord.ui.View(timeout=120)
            back_button = discord.ui.Button(
                label="🔙 Back to Summary",
                style=discord.ButtonStyle.primary,
                custom_id=f"region_back_{tag}"
            )
            async def back_cb(back_interaction: discord.Interaction):
                await back_interaction.response.defer()
                summary_embed = self._build_summary_embed()
                await back_interaction.edit_original_response(embed=summary_embed, view=self)
            back_button.callback = back_cb
            back_view.add_item(back_button)

            await interaction.edit_original_response(embed=embed, view=back_view)
        return callback

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class GuildRegionSelectView(discord.ui.View):
    """View with buttons for selecting a guild to view region breakdown"""
    def __init__(self, clubs: list, guild_infos: list, cog):
        super().__init__(timeout=60)
        self.cog = cog
        self.clubs = clubs
        self.guild_infos = guild_infos
        
        for idx, club in enumerate(clubs[:5]):
            guild_name = "Unknown"
            if guild_infos and idx < len(guild_infos):
                info = guild_infos[idx]
                guild_name = info.get('base', {}).get('name', 'Unknown')
            
            label = f"{idx + 1}. {guild_name[:45]}" if len(guild_name) > 45 else f"{idx + 1}. {guild_name}"
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"guild_region_select_{idx}"
            )
            button.callback = self.make_callback(idx)
            self.add_item(button)
    
    def make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_guild_select(interaction, idx)
        return callback
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="guild_region_select_cancel", row=4)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            guild_data = get_full_guild_info(club_id, hostnum=hostnum)
            if not guild_data or 'result' not in guild_data:
                await interaction.followup.send("❌ Guild not found or API error")
                return

            result = guild_data['result']
            members = result.get('members', {}).get('members', {})
            all_uids = list(members.keys())

            if not all_uids:
                await interaction.followup.send("❌ No members found in guild")
                return

            bulk_data = get_bulk_players_info(all_uids, fields=["base"])
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
            total_members = sum(len(m) for m in regions.values())
            sorted_tags = sorted(regions.keys(), key=lambda t: get_region_label(t))

            embed = discord.Embed(
                title=f"🌍 {guild_name} — Members by Region",
                color=discord.Color.og_blurple()
            )
            embed.description = f"**Total members:** {total_members}  |  **Regions found:** {len(sorted_tags)}"

            for tag in sorted_tags:
                member_list = regions[tag]
                sorted_members = sorted(member_list, key=lambda m: (not m['is_online'], m['nickname'].lower()))
                online_count = sum(1 for m in member_list if m['is_online'])
                region_label = get_region_label(tag)

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

                embed.add_field(
                    name=f"{region_label}  ({len(member_list)} members, 🟢 {online_count} online)",
                    value=f"```{preview_text}```",
                    inline=False
                )

            view = GuildRegionSummaryView(guild_name, regions, tag_map, self.cog)
            await interaction.edit_original_response(content=None, embed=embed, view=view)

        except Exception as e:
            logger.error(f"Guild region select failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to load region data: `{str(e)}`")
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class GuildSearchSelectView(discord.ui.View):
    """View with buttons for selecting a guild from search results"""
    def __init__(self, clubs: list, guild_infos: list, cog):
        super().__init__(timeout=60)
        self.cog = cog
        self.clubs = clubs
        self.guild_infos = guild_infos
        
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
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"guild_select_{idx}"
            )
            button.callback = self.make_callback(idx)
            self.add_item(button)
    
    def make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_guild_select(interaction, idx)
        return callback
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="guild_select_cancel", row=4)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            guild_data = get_full_guild_info(club_id, hostnum=hostnum)
            
            if not guild_data or 'result' not in guild_data:
                await loading_msg.edit(content="❌ Guild not found or API error")
                return
            
            result = guild_data['result']
            base = result.get('base', {})
            members = result.get('members', {})
            play = result.get('play', {})
            
            embed = discord.Embed(
                title="🏰 Guild Profile",
                color=discord.Color.og_blurple()
            )
            
            embed.description = f"**{base.get('name', 'Unknown Guild')}**"
            
            embed.add_field(name="📛 Guild Name", value=f"`{base.get('name', 'Unknown')}`", inline=True)
            embed.add_field(name="⭐ Level", value=f"`{base.get('level', 0)}`", inline=True)
            embed.add_field(name="👥 Members", value=f"`{members.get('member_num', 0)} / 100`", inline=True)
            embed.add_field(name="💰 Guild Funds", value=f"`{base.get('fund', 0):,}`", inline=True)
            embed.add_field(name="📈 Total Fame", value=f"`{base.get('fame', 0):,}`", inline=True)
            embed.add_field(name="🔥 Weekly Activity", value=f"`{base.get('week_fame', 0):,}`", inline=True)
            embed.add_field(name="⚔️ GvG Points", value=f"`{play.get('pk_match_info', {}).get('battle_score', 0)}`", inline=True)
            
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
                bulk_data = get_bulk_players_info(pids_to_fetch, fields=["base"])
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
            bulk_data = get_bulk_players_info(all_pids, fields=["base"])
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
            
            await interaction.edit_original_response(content=None, embed=embed, view=None)
            await loading_msg.edit(content="✅ Guild found!")
            
        except Exception as e:
            logger.error(f"Guild detail fetch failed: {str(e)}", exc_info=True)
            await loading_msg.edit(content=f"❌ Failed to load guild details: `{str(e)}`")
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class WWMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_guild_state = None
        self.monitor_channel = None
        self.monitor_enabled = False
        self.check_interval_minutes = 2
        self.monitor_message = None
        self.online_button_view = OnlinePlayersButton(self)
        self.db_path = BASE_DIR / "data" / "guild_monitor.db"

    player_group = app_commands.Group(
        name="player",
        description="WWM Player search commands"
    )
    
    guild_group = app_commands.Group(
        name="guild",
        description="Guild monitoring commands"
    )

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
    
    async def _save_config(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO monitor_config VALUES ('channel_id', ?)", (str(self.monitor_channel.id) if self.monitor_channel else None,))
            await db.execute("REPLACE INTO monitor_config VALUES ('message_id', ?)", (str(self.monitor_message.id) if self.monitor_message else None,))
            await db.execute("REPLACE INTO monitor_config VALUES ('enabled', ?)", ('true' if self.monitor_enabled else 'false',))
            await db.execute("REPLACE INTO monitor_config VALUES ('interval', ?)", (str(self.check_interval_minutes),))
            await db.commit()
    
    async def cog_load(self):
        await self._init_database()
        await self._load_config()
        if self.monitor_enabled and self.monitor_channel:
            self.guild_monitor_task.start()
    
    async def cog_unload(self):
        if self.guild_monitor_task.is_running():
            self.guild_monitor_task.cancel()

    @player_group.command(name="search", description="Search for a WWM player by their Number ID")
    @app_commands.describe(number_id="The player's 10-digit Number ID")
    async def player_search(self, interaction: discord.Interaction, number_id: str):
        await interaction.response.send_message("🔍 Searching for player...")

        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT 1 FROM verified_members WHERE user_id = ?", (interaction.user.id,))
            row = await cursor.fetchone()
            is_verified = row is not None

        if not number_id.isdigit() or len(number_id) != 10:
            embed = discord.Embed(
                title="❌ Invalid Number ID",
                description="Number ID must be exactly 10 digits long",
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

        try:
            await interaction.edit_original_response(content="✅ Found player\n📦 Loading player profile...")
            
            raw_data = get_player_info(number_id, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            
            if not raw_data:
                embed = discord.Embed(title="❌ Player not found", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return

            logger.debug(f"API Response received: {str(raw_data)}")

            embed = discord.Embed(title="👤 Player Profile", color=discord.Color.og_blurple())
            
            try:
                player_pid = None
                player_hostnum = 10403
                
                if isinstance(raw_data, dict):
                    data = raw_data.get('result', raw_data)
                    player_pid = data.get('id')
                    if 'hostnum' in data:
                        player_hostnum = data.get('hostnum', 10403)
                
                if player_pid:
                    fashion_data = get_fashion_plan(player_pid, hostnum=player_hostnum)
                    if fashion_data:
                        if fashion_data.get('code') == 0 and 'result' in fashion_data:
                            cover_img = fashion_data['result'].get('cover_img')
                            if cover_img:
                                embed.set_image(url=cover_img)
            except Exception as fashion_err:
                logger.warning(f"Failed to get fashion cover image: {str(fashion_err)}")

            if isinstance(raw_data, dict):
                if 'result' in raw_data:
                    data = raw_data.get('result', {})
                else:
                    data = raw_data
                
                base_data = data.get('base', {})
                if isinstance(base_data, list) and len(base_data) > 0:
                    base_data = base_data[0]
                if not base_data and 'nickname' in data:
                    base_data = data
                
                nickname = base_data.get('nickname', data.get('nickname', 'Unknown'))
                embed.description = f"**{nickname}**"
                
                embed.add_field(name="📛 Nickname", value=f"`{nickname}`", inline=True)
                embed.add_field(name="🏆 Level", value=f"`{base_data.get('level', 0)}`", inline=True)
                embed.add_field(name="🆔 Number ID", value=f"`{base_data.get('number_id', number_id)}`", inline=True)

                name_card = data.get('name_card', {})
                player_signature = name_card.get('sign', None)
                if player_signature and player_signature.strip():
                    embed.add_field(name="✍️ Player Signature", value=f"`{player_signature}`", inline=False)

                if is_verified:
                    attr = data.get('attr', {})
                    embed.add_field(name="⚔️ Martial Mastery", value=f"`{round(attr.get('XIUWEI_KUNGFU', 0), 1)}`", inline=True)
                    embed.add_field(name="📚 Scholar Mastery", value=f"`{round(attr.get('XIUWEI_TRADE3', 0), 1)}`", inline=True)
                    embed.add_field(name="💚 Healer Mastery", value=f"`{round(attr.get('XIUWEI_TRADE4', 0), 1)}`", inline=True)
                    embed.add_field(name="🗺️ Exploration Mastery", value=f"`{round(attr.get('XIUWEI_EXPLORE', 0), 1)}`", inline=True)
                    embed.add_field(name="🥊 Power", value=f"`{round(attr.get('STR', 0), 1)}`", inline=True)
                    embed.add_field(name="🛡️ Body", value=f"`{round(attr.get('CON', 0), 1)}`", inline=True)
                    embed.add_field(name="⚡ Momentum", value=f"`{round(attr.get('BAS', 0), 1)}`", inline=True)
                    embed.add_field(name="💨 Agility", value=f"`{round(attr.get('CRI', 0), 1)}`", inline=True)
                    embed.add_field(name="🔰 Defense", value=f"`{round(attr.get('AGI', 0), 1)}`", inline=True)
                    embed.add_field(name="🌍 Region", value=f"`{base_data.get('oversea_tag', 'N/A')}`", inline=True)
                    embed.add_field(name="⌛ Total Online Time", value=f"`{round(base_data.get('online_time', 0) / 3600, 1)} hours`", inline=True)
                else:
                    embed.set_footer(text="🔗 Bind your account to view full stats, combat power and details. Go to #1501139237594992780 to link your game account.")

                status_lines = []
                is_online = base_data.get('is_online', 0)
                if is_online == 1:
                    status_lines.append("`🟢 ONLINE NOW`")
                else:
                    status_lines.append("`🔴 Offline`")
                
                gameplay = data.get('gameplay_trail', {})
                played = gameplay.get('played', [])
                for match in played:
                    if 'grade' in match and 'score' in match:
                        status_lines.append(f"⚔️ PvP Grade: `{match['grade']}` | Score: `{match['score']}`")
                        break
                
                if status_lines:
                    embed.add_field(name="📋 Status", value="\n".join(status_lines), inline=False)
            
            player_pid = data.get('id')
            if player_pid:
                try:
                    await interaction.edit_original_response(content="✅ Found player\n📦 Loading player profile...\n🏰 Checking guild info...")
                    club_data = get_club_hostnums(player_pid)
                    
                    guild_name = "No Guild"
                    member_status = "❌ Not Guild Member"
                    player_club_id = None
                    club_hostnum = 10103
                    
                    if club_data:
                        result_data = club_data.get('result', {})
                        player_club_data = result_data.get(player_pid, {})
                        club_info = player_club_data.get('club', {})
                        player_club_id = club_info.get('club_id')
                        club_hostnum = club_info.get('hostnum', 10103)
                    
                    if player_club_id:
                        await interaction.edit_original_response(content="✅ Found player\n📦 Loading player profile...\n🏰 Checking guild info...\n📋 Loading guild data...")
                        guild_full_data = get_full_guild_info(player_club_id, hostnum=club_hostnum)
                        
                        if guild_full_data:
                            guild_base = guild_full_data.get('result', {}).get('base', {})
                            guild_name = guild_base.get('name', 'Unknown Guild')
                        
                        if player_club_id == CLUB_ID:
                            member_status = f"✅ **Guild Member**"
                            embed.color = discord.Color.green()
                        else:
                            member_status = "❌ Not In Our Guild"
                    
                    status_text = f"{member_status}\n🏰 Guild: `{guild_name}`"
                    embed.add_field(name="👥 Member Status", value=status_text, inline=False)
                    
                except Exception as club_err:
                    logger.warning(f"Failed to get club info: {str(club_err)}")
            else:
                embed.description = f"```\n{str(raw_data)}\n```"

            await interaction.edit_original_response(content=None, embed=embed)

        except Exception as e:
            logger.error(f"API Request failed: {str(e)}")
            embed = discord.Embed(
                title="❌ API Error",
                description=f"Failed to connect to WWM API: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(content=None, embed=embed)

    @tasks.loop(minutes=1)
    async def guild_monitor_task(self):
        if not self.monitor_enabled or not self.monitor_channel:
            return
        
        try:
            guild_data = get_full_guild_info(CLUB_ID)
            
            if not guild_data:
                logger.warning("Guild check returned no data")
                return
            
            status_message, embeds, online_count, member_count, players_data = self._build_status_board(guild_data)
            
            try:
                now_ts = int(discord.utils.utcnow().timestamp())
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO guild_player_counts (ts, total_members, online_count, guild_week_fame) VALUES (?, ?, ?, ?)",
                        (now_ts, member_count, online_count, guild_data.get('result', {}).get('base', {}).get('week_fame', 0))
                    )
                    
                    if players_data is not None:
                        snapshot = []
                        for pid, player_data in players_data.items():
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
            
            await self.monitor_message.edit(content=status_message, embeds=embeds, view=self.online_button_view)
            logger.debug("Guild status message updated successfully")
            
            if self.last_guild_state is not None:
                diff = DeepDiff(self.last_guild_state, guild_data, ignore_order=True, exclude_paths=["root['timestamp']"])
                if diff:
                    logger.debug(f"Guild changes detected: {list(diff.keys())}")
                    await self._process_changes(diff, guild_data)
            
            self.last_guild_state = guild_data
            
        except Exception as e:
            logger.error(f"Guild monitor task failed: {str(e)}", exc_info=True)

    def _build_status_board(self, guild_data):
        result = guild_data.get('result', {})
        base = result.get('base', {})
        members = result.get('members', {})
        activity = result.get('activity', {})
        play = result.get('play', {})
        
        member_list = members.get('members', {})
        member_count = members.get('member_num', 0)
        
        now = discord.utils.utcnow().timestamp()
        
        online = 0
        online_player_names = []
        players_data = None
        
        all_pids = list(member_list.keys())
        
        from utility.wwm import get_bulk_players_info
        try:
            bulk_data = get_bulk_players_info(all_pids, fields=["base", "club"])
            if bulk_data and bulk_data.get('code') == 0:
                players_data = bulk_data.get('result', {})
                for pid, player_data in players_data.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online += 1
                        online_player_names.append(player_base.get('nickname', 'Unknown'))
        except Exception as e:
            logger.warning(f"Failed to get bulk player data, falling back to estimate: {e}")
            for pid, member in member_list.items():
                last_online = member.get('last_online_ts', 0)
                if now - last_online < 7200:
                    online += 1
        
        lines = []
        lines.append("## 🏰 **GUILD LIVE STATUS**")
        lines.append("```ansi")
        lines.append("╔═════════════════════════════════════════╗")
        lines.append(f"║ 📛 Name: {base.get('name', 'Unknown'):<40}")
        lines.append(f"║ ⭐ Guild Level: {base.get('level', 0):<40}")
        lines.append(f"║ 👥 Members: {member_count}/100{' ':<32}")
        lines.append(f"║ 🎓 Apprentices: {members.get('apprentice_num', 0):<34}")
        lines.append(f"║ 💰 Guild Funds: {result.get('base', {}).get('fund', 0):,}{' ':<25}")
        lines.append(f"║ 📈 Total Fame: {result.get('base', {}).get('fame', 0):,}{' ':<28}")
        lines.append(f"║ 🔥 Weekly Activity: {result.get('base', {}).get('week_fame', 0):,}{' ':<23}")
        lines.append(f"║ ⚔️ GvG Points: {play.get('pk_match_info', {}).get('battle_score', 0):<32}")
        lines.append(f"║ 🟢 Online Now: {online}/{member_count}{' ':<30}")
        lines.append("╚═════════════════════════════════════════╝")
        lines.append("```")

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
                
                weekly_leaderboard.append( (-weekly_points, nickname, weekly_points) )
        else:
            for pid, member in member_list.items():
                nickname = member.get('nickname', 'Unknown')
                club_data = member.get('club', {})
                weekly_points = club_data.get('liveness', 0)
                weekly_leaderboard.append( (-weekly_points, nickname, weekly_points) )

        weekly_leaderboard.sort()

        lines.append("\n## 🔥 WEEKLY ACTIVITY POINTS - TOP 10")
        lines.append("```")
        
        for rank, (neg_points, name, points) in enumerate(weekly_leaderboard[:10], 1):
            if rank == 1:
                rank_text = "🥇"
            elif rank == 2:
                rank_text = "🥈"
            elif rank == 3:
                rank_text = "🥉"
            else:
                rank_text = f"{rank}."
            lines.append(f"{rank_text} {name}: {points:,}")
        
        lines.append("```")

        embeds = []

        applys = result.get('applys', {}).get('apply_dict', {})
        if len(applys) > 0:
            lines.append(f"\n### 📋 **PENDING APPLICATIONS: {len(applys)}**")
            lines.append("```ansi")
            for pid, app in applys.items():
                lines.append(f"✅ {app.get('nickname', 'Unknown')}")
            lines.append("```")
        
        lines.append(f"⏱️ Last Updated: <t:{int(now)}:R>")
        lines.append(f"🔄 Next Update: <t:{int(now) + 60}:R>")
        
        return "\n".join(lines), embeds, online, member_count, players_data
    
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
                    guild_data = get_full_guild_info(CLUB_ID)
                    if guild_data:
                        status_message, embeds, _, _, _ = self._build_status_board(guild_data)
                        self.monitor_message = await self.monitor_channel.send(content=status_message, embeds=embeds, view=self.online_button_view)
                        await self._save_config()
                        self.last_guild_state = guild_data
    
    @guild_group.command(name="set-channel", description="Set channel for guild monitor notifications")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_monitor_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.monitor_channel = channel
        
        guild_data = get_full_guild_info(CLUB_ID)
        if guild_data:
            status_message, embeds, _, _, _ = self._build_status_board(guild_data)
            self.monitor_message = await channel.send(content=status_message, embeds=embeds, view=self.online_button_view)
            self.last_guild_state = guild_data
        
        await self._save_config()
        await interaction.response.send_message(f"✅ Guild monitor channel set to {channel.mention}. Status board created.", ephemeral=True)
        logger.info(f"Guild monitor channel set to {channel.id} by {interaction.user}")
    
    @guild_group.command(name="toggle", description="Enable or disable guild monitoring")
    @app_commands.checks.has_permissions(administrator=True)
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
    @app_commands.checks.has_permissions(administrator=True)
    async def force_guild_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        guild_data = get_full_guild_info(CLUB_ID)
        
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
            
            player_data = get_player_info(player_id)
            
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

            club_data = get_club_hostnums(player_pid)
            
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
            
            guild_data = get_full_guild_info(target_guild_id, hostnum=target_hostnum)
            
            if not guild_data or 'result' not in guild_data:
                embed = discord.Embed(title="❌ Guild not found", color=discord.Color.red())
                await interaction.edit_original_response(content=None, embed=embed)
                return

            result = guild_data['result']
            base = result.get('base', {})
            members = result.get('members', {})
            play = result.get('play', {})

            embed = discord.Embed(title="🏰 Guild Profile", color=discord.Color.og_blurple())
            embed.description = f"**{base.get('name', 'Unknown Guild')}**"
            embed.add_field(name="📛 Guild Name", value=f"`{base.get('name', 'Unknown')}`", inline=True)
            embed.add_field(name="⭐ Level", value=f"`{base.get('level', 0)}`", inline=True)
            embed.add_field(name="👥 Members", value=f"`{members.get('member_num', 0)} / 100`", inline=True)
            embed.add_field(name="💰 Guild Funds", value=f"`{base.get('fund', 0):,}`", inline=True)
            embed.add_field(name="📈 Total Fame", value=f"`{base.get('fame', 0):,}`", inline=True)
            embed.add_field(name="🔥 Weekly Activity", value=f"`{base.get('week_fame', 0):,}`", inline=True)
            embed.add_field(name="⚔️ GvG Points", value=f"`{play.get('pk_match_info', {}).get('battle_score', 0)}`", inline=True)

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
                bulk_data = get_bulk_players_info(pids_to_fetch, fields=["base"])
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
            bulk_data = get_bulk_players_info(all_pids, fields=["base"])
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

    @guild_group.command(name="search-name", description="Search for a guild by name (shows up to 5 results to choose from)")
    @app_commands.describe(name="The guild name to search for")
    async def guild_search_name(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        
        try:
            if not name or len(name.strip()) == 0:
                embed = discord.Embed(title="❌ Invalid Name", description="Please provide a guild name to search for.", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return
            
            search_term = name.strip()
            clubs = get_club_by_name(search_term, limit=5)
            
            if not clubs or len(clubs) == 0:
                embed = discord.Embed(title="❌ No Results", description=f"No guilds found matching `{search_term}`", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
                return
            
            club_ids = [club.get('club_id') for club in clubs]
            hostnums = [club.get('hostnum', 10103) for club in clubs]
            guild_infos = get_club_brief_info_batch(club_ids, hostnums) or []
            
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
            
            embed = discord.Embed(
                title="🔍 Guild Search Results",
                description=f"Found **{len(valid_clubs)}** active guild(s) matching `{search_term}`" +
                            (f"\n*({removed_count} deleted guild(s) filtered out)*" if removed_count > 0 else "") +
                            "\n\nSelect a button below to view the guild details.",
                color=discord.Color.og_blurple()
            )
            
            result_lines = []
            for idx, info in enumerate(valid_infos, 1):
                guild_name = info.get('base', {}).get('name', 'Unknown')
                member_num = info.get('members', {}).get('member_num', '?')
                apprentice_num = info.get('members', {}).get('apprentice_num', '?')
                result_lines.append(f"**{idx}.** **{guild_name}** — 👥 `{member_num}` 🎓 `{apprentice_num}`")
            
            embed.add_field(name="📋 Results", value="\n".join(result_lines), inline=False)
            embed.set_footer(text="⏳ This selection will expire in 60 seconds")
            
            view = GuildSearchSelectView(valid_clubs, valid_infos, self)
            await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            logger.error(f"Guild name search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(title="❌ Search Failed", description=f"An error occurred while searching: `{str(e)}`", color=discord.Color.red())
            await interaction.followup.send(embed=embed)

    @guild_group.command(name="stats", description="Display graphs of guild statistics over time")
    @app_commands.checks.has_permissions(administrator=True)
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
                    "EU": "EU (Europe)", "HMT": "HMT (HK/Macau/Taiwan)", "JP": "JP (Japan)",
                    "KR": "KR (South Korea)", "NA": "NA (North America)", "NAW": "NAW (North America West)",
                    "SA": "SA (South America)", "SEA": "SEA (Southeast Asia)", "OC": "OC (Oceania)", "OTHER": "Other",
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
                            all_region_tags.add(tag)
                
                for row in rows:
                    snapshot = json.loads(row['snapshot_json'])
                    region_online = Counter()
                    for p in snapshot:
                        if p.get('is_online', False):
                            tag = str(p.get('oversea_tag', ''))
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
                
                # We need max_y here; in by_region we can take max across all regions
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

    @guild_group.command(name="region", description="Sort and display guild members grouped by region (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(name="Optional guild name to search for (leave empty to use our guild)")
    async def guild_region(self, interaction: discord.Interaction, name: str = None):
        await interaction.response.defer()
        if name:
            try:
                search_term = name.strip()
                if not search_term:
                    await interaction.followup.send("❌ Please provide a valid guild name")
                    return
                clubs = get_club_by_name(search_term, limit=5)
                if not clubs or len(clubs) == 0:
                    embed = discord.Embed(title="❌ No Results", color=discord.Color.red())
                    await interaction.followup.send(embed=embed)
                    return
                club_ids = [club.get('club_id') for club in clubs]
                hostnums = [club.get('hostnum', 10103) for club in clubs]
                guild_infos = get_club_brief_info_batch(club_ids, hostnums) or []
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
                embed = discord.Embed(title="🔍 Guild Search for Region View", color=discord.Color.og_blurple())
                result_lines = []
                for idx, info in enumerate(valid_infos, 1):
                    guild_name = info.get('base', {}).get('name', 'Unknown')
                    member_num = info.get('members', {}).get('member_num', '?')
                    apprentice_num = info.get('members', {}).get('apprentice_num', '?')
                    result_lines.append(f"**{idx}.** **{guild_name}** — 👥 `{member_num}` 🎓 `{apprentice_num}`")
                embed.add_field(name="📋 Results", value="\n".join(result_lines), inline=False)
                embed.set_footer(text="⏳ This selection will expire in 60 seconds")
                view = GuildRegionSelectView(valid_clubs, valid_infos, self)
                await interaction.followup.send(embed=embed, view=view)
            except Exception as e:
                logger.error(f"Guild region search failed: {str(e)}", exc_info=True)
                embed = discord.Embed(title="❌ Search Failed", color=discord.Color.red())
                await interaction.followup.send(embed=embed)
            return

        try:
            guild_data = get_full_guild_info(CLUB_ID)
            if not guild_data or 'result' not in guild_data:
                await interaction.followup.send("❌ Failed to fetch guild data")
                return
            result = guild_data['result']
            members = result.get('members', {}).get('members', {})
            all_uids = list(members.keys())
            if not all_uids:
                await interaction.followup.send("❌ No members found in guild")
                return
            bulk_data = get_bulk_players_info(all_uids, fields=["base"])
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
            total_members = sum(len(m) for m in regions.values())
            sorted_tags = sorted(regions.keys(), key=lambda t: get_region_label(t))
            embed = discord.Embed(title=f"🌍 {guild_name} — Members by Region", color=discord.Color.og_blurple())
            embed.description = f"**Total members:** {total_members}  |  **Regions found:** {len(sorted_tags)}" + "\n*Click a region button below to see full list*"
            for tag in sorted_tags:
                member_list = regions[tag]
                sorted_members = sorted(member_list, key=lambda m: (not m['is_online'], m['nickname'].lower()))
                online_count = sum(1 for m in member_list if m['is_online'])
                region_label = get_region_label(tag)
                preview = sorted_members[:5]
                remaining = len(sorted_members) - 5
                lines = []
                for m in preview:
                    lines.append(f"{'🟢' if m['is_online'] else '⚫'} Lv{m['level']:<3} | {m['nickname']:<25} | ID: {m.get('number_id', 'N/A')}")
                preview_text = "\n".join(lines)
                if remaining > 0:
                    preview_text += f"\n... and {remaining} more"
                embed.add_field(name=f"{region_label}  ({len(member_list)} members, 🟢 {online_count} online)", value=f"```{preview_text}```", inline=False)
            view = GuildRegionSummaryView(guild_name, regions, tag_map, self)
            await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Guild region command failed: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to display guild regions: `{str(e)}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(WWMCog(bot))