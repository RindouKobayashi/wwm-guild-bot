import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import sqlite3
import json
from deepdiff import DeepDiff

from utility.wwm import get_player_info, get_club_hostnums, get_full_guild_info, get_fashion_plan
from settings import WWM_UID, WWM_TOKEN, WWM_API_URL, logger, CLUB_ID, BASE_DIR

DB_PATH = BASE_DIR / "data" / "guild_verification.db"


class OnlinePlayersButton(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
    
    @discord.ui.button(label="Check Online Players", style=discord.ButtonStyle.green, emoji="🟢", custom_id="online_players_button")
    async def check_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ALWAYS DEFER FIRST - Discord only gives 3 seconds to respond
        await interaction.response.defer(ephemeral=True)
        
        # Check if user has the guild member role
        GUILD_MEMBER_ROLE_ID = 1501140557299318864
        
        member_role = discord.utils.get(interaction.user.roles, id=GUILD_MEMBER_ROLE_ID)
        if not member_role:
            await interaction.followup.send("❌ You are not guild member", ephemeral=True)
            return
        
        # Fetch live online players list
        try:
            guild_data = get_full_guild_info(CLUB_ID)
            if not guild_data:
                await interaction.followup.send("❌ Failed to retrieve guild data", ephemeral=True)
                return
            
            result = guild_data.get('result', {})
            members = result.get('members', {})
            member_list = members.get('members', {})
            
            # Get REAL live online status
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
            
            # Build response
            if online_player_names:
                lines = []
                lines.append(f"### 🟢 ONLINE PLAYERS ({len(online_player_names)}):")
                lines.append("```")
                for name in sorted(online_player_names):
                    lines.append(f"✅ {name}")
                lines.append("```")
                await interaction.followup.send("\n".join(lines), ephemeral=True)
            else:
                await interaction.followup.send("🔴 No players are currently online", ephemeral=True)
                
        except Exception as e:
            logger.error(f"Failed to fetch online players: {str(e)}")
            await interaction.followup.send("❌ Failed to retrieve online players list", ephemeral=True)


class WWMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_guild_state = None
        self.monitor_channel = None
        self.monitor_enabled = False
        self.check_interval_minutes = 2
        self.monitor_message = None
        self.online_button_view = OnlinePlayersButton(self)
        
        # Load saved config
        self.db_path = BASE_DIR / "data" / "guild_monitor.db"
        self._init_database()
        

    player_group = app_commands.Group(
        name="player",
        description="WWM Player search commands"
    )
    
    guild_group = app_commands.Group(
        name="guild",
        description="Guild monitoring commands"
    )

    @player_group.command(
        name="search",
        description="Search for a WWM player by their Number ID"
    )
    @app_commands.describe(
        number_id="The player's 10-digit Number ID"
    )
    async def player_search(self, interaction: discord.Interaction, number_id: str):
        await interaction.response.defer(thinking=True)

        # Check if user has bound their account
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT 1 FROM verified_members WHERE user_id = ?", (interaction.user.id,))
        is_verified = c.fetchone() is not None
        conn.close()

        if not is_verified:
            embed = discord.Embed(
                title="❌ Account Not Bound",
                description="You must bind your WWM game account before you can use this command.\n\nUse the account binding system first.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        # Validate input
        if not number_id.isdigit() or len(number_id) != 10:
            embed = discord.Embed(
                title="❌ Invalid Number ID",
                description="Number ID must be exactly 10 digits long",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        # Check if config is set
        if not WWM_UID or not WWM_TOKEN:
            embed = discord.Embed(
                title="❌ API Not Configured",
                description="WWM API credentials are not set up properly. Please contact bot owner.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Get player info using utility function
            raw_data = get_player_info(number_id, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            
            if not raw_data:
                embed = discord.Embed(
                    title="❌ Player not found",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            # Debug: log full response structure
            logger.debug(f"API Response received: {str(raw_data)}")

            # Build response embed
            embed = discord.Embed(
                title="👤 Player Profile",
                color=discord.Color.og_blurple()
            )
            
            # Get fashion plan with cover image
            try:
                player_pid = None
                if isinstance(raw_data, dict):
                    data = raw_data.get('result', raw_data)
                    player_pid = data.get('id')
                
                if player_pid:
                    fashion_data = get_fashion_plan(player_pid)
                    if fashion_data:
                        if fashion_data.get('code') == 0 and 'result' in fashion_data:
                            cover_img = fashion_data['result'].get('cover_img')
                            if cover_img:
                                embed.set_image(url=cover_img)
                                logger.debug(f"Successfully added cover image: {cover_img}")
                        elif fashion_data.get('code') == 2:
                            logger.debug("Fashion plan API requires valid user session token (code 2)")
                        else:
                            logger.debug(f"Fashion plan returned code: {fashion_data.get('code')}")
            except Exception as fashion_err:
                logger.warning(f"Failed to get fashion cover image: {str(fashion_err)}")
                # Continue without image - don't fail whole request

            # Handle different API response structures
            if isinstance(raw_data, dict):
                # Check if data is nested inside result field
                if 'result' in raw_data:
                    data = raw_data.get('result', {})
                else:
                    data = raw_data
                
                base_data = data.get('base', {})
                
                # If base is list, take first item
                if isinstance(base_data, list) and len(base_data) > 0:
                    base_data = base_data[0]
                    
                # Fallback: check if base is directly on root
                if not base_data and 'nickname' in data:
                    base_data = data
                
                # Main player info
                nickname = base_data.get('nickname', data.get('nickname', 'Unknown'))
                embed.description = f"**{nickname}**"
                
                embed.add_field(
                    name="📛 Nickname",
                    value=f"`{nickname}`",
                    inline=True
                )
                
                embed.add_field(
                    name="🏆 Level",
                    value=f"`{base_data.get('level', 0)}`",
                    inline=True
                )
                
                embed.add_field(
                    name="🆔 Number ID",
                    value=f"`{base_data.get('number_id', number_id)}`",
                    inline=True
                )
                
                embed.add_field(
                    name="⚔️ Martial Mastery",
                    value=f"`{round(base_data.get('max_xiuwei_kungfu', 0), 1)}`",
                    inline=True
                )
                
                embed.add_field(
                    name="🌍 Region",
                    value=f"`{base_data.get('oversea_tag', 'N/A')}`",
                    inline=True
                )
                
                embed.add_field(
                    name="⌛ Total Online Time",
                    value=f"`{round(base_data.get('online_time', 0) / 3600, 1)} hours`",
                    inline=True
                )

                # Player signature / bio
                name_card = data.get('name_card', {})
                player_signature = name_card.get('sign', None)
                if player_signature and player_signature.strip():
                    embed.add_field(
                        name="✍️ Player Signature",
                        value=f"`{player_signature}`",
                        inline=False
                    )

                # Extra interesting fields
                status_lines = []
                
                # Online status
                is_online = base_data.get('is_online', 0)
                if is_online == 1:
                    status_lines.append("`🟢 ONLINE NOW`")
                else:
                    status_lines.append("`🔴 Offline`")
                
                # PvP Grade
                gameplay = data.get('gameplay_trail', {})
                played = gameplay.get('played', [])
                for match in played:
                    if 'grade' in match and 'score' in match:
                        status_lines.append(f"⚔️ PvP Grade: `{match['grade']}` | Score: `{match['score']}`")
                        break
                
                if status_lines:
                    embed.add_field(
                        name="📋 Status",
                        value="\n".join(status_lines),
                        inline=False
                    )
                
                # Get club info using utility function
                player_pid = data.get('id')
                if player_pid:
                    try:
                        club_data = get_club_hostnums(player_pid)
                        
                        if club_data:
                            result_data = club_data.get('result', {})
                            player_club_data = result_data.get(player_pid, {})
                            club_info = player_club_data.get('club', {})
                            player_club_id = club_info.get('club_id')
                            club_hostnum = club_info.get('hostnum', 10103)
                            
                            guild_name = "No Guild"
                            member_status = "❌ Not Guild Member"
                            
                            if player_club_id:
                                # Fetch actual guild name using correct hostnum from club data
                                guild_full_data = get_full_guild_info(player_club_id, hostnum=club_hostnum)
                                if guild_full_data:
                                    guild_base = guild_full_data.get('result', {}).get('base', {})
                                    guild_name = guild_base.get('name', 'Unknown Guild')
                                
                                # Check if member of our guild
                                if player_club_id == CLUB_ID:
                                    member_status = f"✅ **Guild Member**"
                                    embed.color = discord.Color.green()
                                else:
                                    member_status = "❌ Not In Our Guild"
                            
                            # Display both status and guild name
                            status_text = f"{member_status}\n🏰 Guild: `{guild_name}`"
                            
                            embed.add_field(
                                name="👥 Member Status",
                                value=status_text,
                                inline=False
                            )
                            
                    except Exception as club_err:
                        logger.warning(f"Failed to get club info: {str(club_err)}")
                
            else:
                embed.description = f"```\n{str(raw_data)}\n```"

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"API Request failed: {str(e)}")
            embed = discord.Embed(
                title="❌ API Error",
                description=f"Failed to connect to WWM API: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Player search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Search Failed",
                description=f"An error occurred while searching: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)


    def _init_database(self):
        (BASE_DIR / "data").mkdir(exist_ok=True)
        with sqlite3.connect(self.db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS monitor_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            db.commit()
    
    async def _load_config(self):
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute("SELECT key, value FROM monitor_config")
            config = {row[0]: row[1] for row in cursor.fetchall()}
            
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
    
    def _save_config(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("REPLACE INTO monitor_config VALUES ('channel_id', ?)", (str(self.monitor_channel.id) if self.monitor_channel else None,))
            db.execute("REPLACE INTO monitor_config VALUES ('message_id', ?)", (str(self.monitor_message.id) if self.monitor_message else None,))
            db.execute("REPLACE INTO monitor_config VALUES ('enabled', ?)", ('true' if self.monitor_enabled else 'false',))
            db.execute("REPLACE INTO monitor_config VALUES ('interval', ?)", (str(self.check_interval_minutes),))
            db.commit()
    
    async def cog_load(self):
        await self._load_config()
        if self.monitor_enabled and self.monitor_channel:
            self.guild_monitor_task.start()
    
    async def cog_unload(self):
        if self.guild_monitor_task.is_running():
            self.guild_monitor_task.cancel()
    
    @tasks.loop(minutes=1)
    async def guild_monitor_task(self):
        if not self.monitor_enabled or not self.monitor_channel:
            return
        
        try:
            guild_data = get_full_guild_info(CLUB_ID)
            
            if not guild_data:
                logger.warning("Guild check returned no data")
                return
            
            # Build and update status board
            status_message, embeds = self._build_status_board(guild_data)
            
            await self.monitor_message.edit(content=status_message, embeds=embeds, view=self.online_button_view)
            logger.debug("Guild status message updated successfully")
            
            # Check for changes and send alerts
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
        
        # Get REAL live online status from player API
        from utility.wwm import get_bulk_players_info
        
        now = discord.utils.utcnow().timestamp()
        
        online = 0
        online_player_names = []
        
        try:
            # Get all member pids
            all_pids = list(member_list.keys())
            
            # Bulk fetch real live online status
            bulk_data = get_bulk_players_info(all_pids, fields=["base"])
            
            if bulk_data and bulk_data.get('code') == 0:
                players = bulk_data.get('result', {})
                
                for pid, player_data in players.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online += 1
                        online_player_names.append(player_base.get('nickname', 'Unknown'))
            
        except Exception as e:
            logger.warning(f"Failed to get real online status, falling back to estimate: {e}")
            # Fallback to old estimation method
            now = discord.utils.utcnow().timestamp()
            for pid, member in member_list.items():
                last_online = member.get('last_online_ts', 0)
                if now - last_online < 7200: # 2 hours
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
        
        
        # Pending Applications (Text Only)
        applys = result.get('applys', {}).get('apply_dict', {})
        if len(applys) > 0:
            lines.append(f"\n### 📋 **PENDING APPLICATIONS: {len(applys)}**")
            lines.append("```ansi")
            for pid, app in applys.items():
                lines.append(f"✅ {app.get('nickname', 'Unknown')}")
            lines.append("```")
        
        # Footer timestamps
        lines.append(f"⏱️ Last Updated: <t:{int(now)}:R>")
        lines.append(f"🔄 Next Update: <t:{int(now) + 60}:R>")
        
        return "\n".join(lines), []
    
    async def _process_changes(self, diff, new_data):
        changes = []
        
        # Member joined
        if 'iterable_item_added' in diff:
            for path, item in diff['iterable_item_added'].items():
                if 'members' in path and isinstance(item, dict) and 'nickname' in item:
                    changes.append(f"✅ **New Member Joined:** {item.get('nickname')}")
        
        # Member left
        if 'iterable_item_removed' in diff:
            for path, item in diff['iterable_item_removed'].items():
                if 'members' in path and isinstance(item, dict) and 'nickname' in item:
                    changes.append(f"❌ **Member Left:** {item.get('nickname')}")
        
        # Building level up
        if 'values_changed' in diff:
            for path, change in diff['values_changed'].items():
                if 'building' in path and 'lv' in path:
                    changes.append(f"🏗️ **Building Upgraded:** Level {change['old_value']} → {change['new_value']}")
        
        # Guild level up
        if 'values_changed' in diff:
            for path, change in diff['values_changed'].items():
                if path.endswith('base.level'):
                    changes.append(f"⭐ **GUILD LEVEL UP!** {change['old_value']} → {change['new_value']}")
        
        # New applications
        if 'iterable_item_added' in diff:
            for path, item in diff['iterable_item_added'].items():
                if 'apply_dict' in path:
                    changes.append(f"📥 **New Guild Application:** {item.get('nickname', 'Unknown')}")
        
        # Guild announcement changed
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
        
        # Load message on first run
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute("SELECT value FROM monitor_config WHERE key = 'message_id'")
            row = cursor.fetchone()
            if row and self.monitor_channel:
                try:
                    self.monitor_message = await self.monitor_channel.fetch_message(int(row[0]))
                except:
                    # Create new message if old one not found
                    guild_data = get_full_guild_info(CLUB_ID)
                    if guild_data:
                        status_message, embeds = self._build_status_board(guild_data)
                        self.monitor_message = await self.monitor_channel.send(content=status_message, embeds=embeds, view=self.online_button_view)
                        self._save_config()
                        self.last_guild_state = guild_data
    
    @guild_group.command(name="set-channel", description="Set channel for guild monitor notifications")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_monitor_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.monitor_channel = channel
        
        # Create initial message
        guild_data = get_full_guild_info(CLUB_ID)
        if guild_data:
            status_message, embeds = self._build_status_board(guild_data)
            self.monitor_message = await channel.send(content=status_message, embeds=embeds, view=self.online_button_view)
            self.last_guild_state = guild_data
        
        self._save_config()
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
        
        self._save_config()
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

    @guild_group.command(
        name="search",
        description="Search for a guild by Player ID"
    )
    @app_commands.describe(
        player_id="Search using a player's 10-digit Number ID (finds their guild)"
    )
    async def guild_search(self, interaction: discord.Interaction, player_id: str):
        await interaction.response.defer(thinking=True)

        # Check if user has bound their account
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT 1 FROM verified_members WHERE user_id = ?", (interaction.user.id,))
        is_verified = c.fetchone() is not None
        conn.close()

        if not is_verified:
            embed = discord.Embed(
                title="❌ Account Not Bound",
                description="You must bind your WWM game account before you can use this command.\n\nUse the account binding system first.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        # Check if API is configured
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
            # Validate player number id format
            if not player_id.isdigit() or len(player_id) != 10:
                embed = discord.Embed(
                    title="❌ Invalid Player ID",
                    description="Player ID must be exactly 10 digits long",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            # Get player info to find their guild
            player_data = get_player_info(player_id)
            
            if not player_data or 'result' not in player_data:
                embed = discord.Embed(
                    title="❌ Player not found",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            player_result = player_data['result']
            player_pid = player_result.get('id')

            if not player_pid:
                embed = discord.Embed(
                    title="❌ Failed to get player data",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            # Get club info for this player
            club_data = get_club_hostnums(player_pid)
            
            if not club_data or 'result' not in club_data:
                embed = discord.Embed(
                    title="❌ Player is not in any guild",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            result_data = club_data['result']
            player_club_data = result_data.get(player_pid, {})
            club_info = player_club_data.get('club', {})
            
            target_guild_id = club_info.get('club_id')
            target_hostnum = club_info.get('hostnum', 10103)

            if not target_guild_id:
                embed = discord.Embed(
                    title="❌ Player is not in any guild",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            # Now fetch full guild information
            guild_data = get_full_guild_info(target_guild_id, hostnum=target_hostnum)
            
            if not guild_data or 'result' not in guild_data:
                embed = discord.Embed(
                    title="❌ Guild not found",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            result = guild_data['result']
            base = result.get('base', {})
            members = result.get('members', {})
            play = result.get('play', {})

            # Build response embed
            embed = discord.Embed(
                title="🏰 Guild Profile",
                color=discord.Color.og_blurple()
            )

            embed.description = f"**{base.get('name', 'Unknown Guild')}**"

            embed.add_field(
                name="📛 Guild Name",
                value=f"`{base.get('name', 'Unknown')}`",
                inline=True
            )

            embed.add_field(
                name="⭐ Level",
                value=f"`{base.get('level', 0)}`",
                inline=True
            )

            embed.add_field(
                name="👥 Members",
                value=f"`{members.get('member_num', 0)} / 100`",
                inline=True
            )

            embed.add_field(
                name="💰 Guild Funds",
                value=f"`{base.get('fund', 0):,}`",
                inline=True
            )

            embed.add_field(
                name="📈 Total Fame",
                value=f"`{base.get('fame', 0):,}`",
                inline=True
            )

            embed.add_field(
                name="🔥 Weekly Activity",
                value=f"`{base.get('week_fame', 0):,}`",
                inline=True
            )

            embed.add_field(
                name="⚔️ GvG Points",
                value=f"`{play.get('pk_match_info', {}).get('battle_score', 0)}`",
                inline=True
            )

            # Find guild leadership
            leader_name = "None"
            vice_leader_name = "None"
            leader_pid = "None"
            vice_leader_pid = "None"
            
            member_list = members.get('members', {})
            for pid, member in member_list.items():
                post_list = member.get('post', [])
                
                # ACTUAL POST IDs FROM LIVE DATA:
                # 1 = Guild Master / Leader
                # 2 = Vice Leader / Deputy
                if 1 in post_list:
                    leader_pid = pid
                if 2 in post_list:
                    vice_leader_pid = pid

            # Now fetch actual nicknames using bulk player API
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

            # Log leadership info for debug
            logger.debug(f"=== GUILD LEADERSHIP FOUND ===")
            logger.debug(f"Guild Leader: {leader_name} | PID: {leader_pid}")
            logger.debug(f"Vice Leader: {vice_leader_name} | PID: {vice_leader_pid}")

            # Calculate real online players count
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

            embed.add_field(
                name="👑 Guild Leader",
                value=f"`{leader_name}`",
                inline=True
            )

            embed.add_field(
                name="⚔️ Vice Leader",
                value=f"`{vice_leader_name}`",
                inline=True
            )

            embed.add_field(
                name="🟢 Online Now",
                value=f"`{online} / {members.get('member_num', 0)}`",
                inline=True
            )

            # Guild announcement
            announcement = result.get('gonggao_info', {}).get('msg')
            if announcement and announcement.strip():
                embed.add_field(
                    name="📢 Guild Announcement",
                    value=f"`{announcement}`",
                    inline=False
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Guild search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Search Failed",
                description=f"An error occurred while searching: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(WWMCog(bot))