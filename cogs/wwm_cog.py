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


class WWMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_guild_state = None
        self.monitor_channel = None
        self.monitor_enabled = False
        self.check_interval_minutes = 2
        self.monitor_message = None
        
        # Load saved config
        self.db_path = BASE_DIR / "data" / "guild_monitor.db"
        self._init_database()
        
        logger.info("WWM Cog loaded")

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
                                logger.info(f"Successfully added cover image: {cover_img}")
                        elif fashion_data.get('code') == 2:
                            logger.info("Fashion plan API requires valid user session token (code 2)")
                        else:
                            logger.info(f"Fashion plan returned code: {fashion_data.get('code')}")
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
                            
                            # Check if member of our guild
                            if player_club_id == CLUB_ID:
                                member_status = "✅ **Guild Member**"
                                embed.color = discord.Color.green()
                            else:
                                member_status = "❌ Not Guild Member"
                            
                            embed.add_field(
                                name="👥 Member Status",
                                value=member_status,
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
        logger.info("WWM Cog loaded successfully")
    
    async def cog_unload(self):
        if self.guild_monitor_task.is_running():
            self.guild_monitor_task.cancel()
        logger.info("WWM Cog unloaded")
    
    @tasks.loop(minutes=2)
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
            
            await self.monitor_message.edit(content=status_message, embeds=embeds)
            logger.debug("Guild status message updated successfully")
            
            # Check for changes and send alerts
            if self.last_guild_state is not None:
                diff = DeepDiff(self.last_guild_state, guild_data, ignore_order=True, exclude_paths=["root['timestamp']"])
                
                if diff:
                    logger.info(f"Guild changes detected: {list(diff.keys())}")
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
        
        # Calculate online members (last 2 hours)
        now = discord.utils.utcnow().timestamp()
        online = 0
        for pid, member in member_list.items():
            last_online = member.get('last_online_ts', 0)
            if now - last_online < 7200: # 2 hours
                online += 1
        
        lines = []
        lines.append("## 🏰 **GUILD LIVE STATUS**")
        lines.append("```ansi")
        lines.append("╔═════════════════════════════════════════╗")
        lines.append(f"║ 📛 Name: {base.get('name', 'Unknown'):<40}")
        lines.append(f"║ ⭐ Level: {base.get('level', 0):<40}")
        lines.append(f"║ 👥 Members: {member_count}/100{' ':<32}")
        lines.append(f"║ 🎓 Apprentices: {members.get('apprentice_num', 0):<34}")
        lines.append(f"║ 💰 Guild Funds: {base.get('fund', 0):,}{' ':<25}")
        lines.append(f"║ 📈 Total Fame: {base.get('fame', 0):,}{' ':<28}")
        lines.append(f"║ 🔥 Weekly Activity: {base.get('week_fame', 0):,}{' ':<23}")
        lines.append(f"║ ⚔️ GvG Points: {play.get('pk_match_info', {}).get('battle_score', 0):<32}")
        lines.append(f"║ 🟢 Online Now: {online}/{member_count}{' ':<30}")
        lines.append("╚═════════════════════════════════════════╝")
        lines.append("```")
        
        # Ranks Table (Text Only - No Embeds)
        custom_posts = members.get('custom_posts', {})
        rank_names = {
            '5': 'Command',
            '7': 'Half Time Performer',
            5: 'Command',
            7: 'Half Time Performer'
        }
        rank_order = [
            '䨻䨻䨻䨻䨻',
            '䨻䨻䨻䨻',
            '䨻䨻䨻',
            '䨻䨻',
            'Command',
            'Half Time Performer',
            'Construction',
            'Absent'
        ]
        
        rank_list = []
        for post_id, post_data in custom_posts.items():
            post_id_str = str(post_id)
            # Skip leader (1) and vice leader (2) ranks
            if post_id_str in ('1', '2') or int(post_id) in (1, 2):
                continue
            name = rank_names.get(post_id, rank_names.get(post_id_str, post_data.get('name', post_id)))
            count = len(post_data.get('pids', []))
            rank_list.append( (name, count) )
        
        # Sort by custom order
        rank_list.sort(key=lambda x: rank_order.index(x[0]) if x[0] in rank_order else 999)
        
        lines.append("### 👑 **RANKS**")
        lines.append("```ansi")
        
        for name, count in rank_list:
            lines.append(f"{name}: {count}")
            
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
        lines.append(f"🔄 Next Update: <t:{int(now) + 120}:R>")
        
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
                        self.monitor_message = await self.monitor_channel.send(content=status_message, embeds=embeds)
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
            self.monitor_message = await channel.send(content=status_message, embeds=embeds)
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


async def setup(bot: commands.Bot):
    await bot.add_cog(WWMCog(bot))