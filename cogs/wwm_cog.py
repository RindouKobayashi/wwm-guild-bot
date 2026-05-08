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
        
        # Send immediate loading feedback - CAPTURE MESSAGE REFERENCE
        loading_msg = await interaction.followup.send("🔄 Getting player list...", ephemeral=True, wait=True)
        
        # Fetch live online players list
        try:
            # Use cached member list from guild monitor state (no extra API call)
            if not self.cog.last_guild_state:
                await loading_msg.edit(content="❌ Guild data not initialized, please try again shortly")
                return
            
            result = self.cog.last_guild_state.get('result', {})
            members = result.get('members', {})
            member_list = members.get('members', {})
            
            # Get REAL live online status - MINIMAL API CALL ONLY
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
            
            # Build response - EDIT ONLY OUR PRIVATE MESSAGE
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
        # Send initial message immediately - no defer
        await interaction.response.send_message("🔍 Searching for player...")

        # Check if user has bound their account
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT 1 FROM verified_members WHERE user_id = ?", (interaction.user.id,))
        is_verified = c.fetchone() is not None
        conn.close()

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
            await interaction.edit_original_response(content="✅ Found player\n📦 Loading player profile...")
            
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
                player_hostnum = 10403
                
                if isinstance(raw_data, dict):
                    data = raw_data.get('result', raw_data)
                    player_pid = data.get('id')
                    
                    # Extract correct hostnum from player data (root level)
                    if 'hostnum' in data:
                        player_hostnum = data.get('hostnum', 10403)
                        logger.debug(f"✅ Found player's actual hostnum: {player_hostnum}")
                
                if player_pid:
                    fashion_data = get_fashion_plan(player_pid, hostnum=player_hostnum)
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

                # Player signature / bio
                name_card = data.get('name_card', {})
                player_signature = name_card.get('sign', None)
                if player_signature and player_signature.strip():
                    embed.add_field(
                        name="✍️ Player Signature",
                        value=f"`{player_signature}`",
                        inline=False
                    )

                if is_verified:
                    # Full stats only for verified bound users - All from attr object
                    attr = data.get('attr', {})
                    
                    embed.add_field(
                        name="⚔️ Martial Mastery",
                        value=f"`{round(attr.get('XIUWEI_KUNGFU', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="📚 Scholar Mastery",
                        value=f"`{round(attr.get('XIUWEI_TRADE3', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="💚 Healer Mastery",
                        value=f"`{round(attr.get('XIUWEI_TRADE4', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="🗺️ Exploration Mastery",
                        value=f"`{round(attr.get('XIUWEI_EXPLORE', 0), 1)}`",
                        inline=True
                    )

                    embed.add_field(
                        name="🥊 Power",
                        value=f"`{round(attr.get('STR', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="🛡️ Body",
                        value=f"`{round(attr.get('CON', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="⚡ Momentum",
                        value=f"`{round(attr.get('BAS', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="💨 Agility",
                        value=f"`{round(attr.get('CRI', 0), 1)}`",
                        inline=True
                    )
                    
                    embed.add_field(
                        name="🔰 Defense",
                        value=f"`{round(attr.get('AGI', 0), 1)}`",
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
                else:
                    # Binding prompt footer for unbound users
                    embed.set_footer(text="🔗 Bind your account to view full stats, combat power and details. Go to #1501139237594992780 to link your game account.")

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
                        # Fetch actual guild name using correct hostnum from club data
                        await interaction.edit_original_response(content="✅ Found player\n📦 Loading player profile...\n🏰 Checking guild info...\n📋 Loading guild data...")
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

            # Send final result
            await interaction.edit_original_response(content=None, embed=embed)

        except Exception as e:
            logger.error(f"API Request failed: {str(e)}")
            embed = discord.Embed(
                title="❌ API Error",
                description=f"Failed to connect to WWM API: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.edit_original_response(content=None, embed=embed)

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
            db.execute("""
                CREATE TABLE IF NOT EXISTS guild_player_counts (
                    ts INTEGER PRIMARY KEY,
                    total_members INTEGER NOT NULL,
                    online_count INTEGER NOT NULL,
                    guild_week_fame INTEGER DEFAULT 0
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
            status_message, embeds, online_count, member_count = self._build_status_board(guild_data)
            
            # Record player count data point
            try:
                now_ts = int(discord.utils.utcnow().timestamp())
                with sqlite3.connect(self.db_path) as db:
                    db.execute(
                        "INSERT OR IGNORE INTO guild_player_counts (ts, total_members, online_count, guild_week_fame) VALUES (?, ?, ?, ?)",
                        (now_ts, member_count, online_count, guild_data.get('result', {}).get('base', {}).get('week_fame', 0))
                    )
                    db.commit()
                    
                # Cleanup old data (keep 30 days)
                cleanup_ts = now_ts - 30 * 86400
                db.execute("DELETE FROM guild_player_counts WHERE ts < ?", (cleanup_ts,))
                db.commit()
            except Exception as e:
                logger.warning(f"Failed to record player count: {e}")
            
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
        
        now = discord.utils.utcnow().timestamp()
        
        online = 0
        online_player_names = []
        players_data = None
        
        # Get all member pids ONCE
        all_pids = list(member_list.keys())
        
        # ✅ SINGLE API CALL ONLY - fetch ALL required data in ONE request
        from utility.wwm import get_bulk_players_info
        try:
            # ✅ SINGLE API CALL ONLY - fetch ALL required data in ONE request
            bulk_data = get_bulk_players_info(all_pids, fields=["base", "club"])
            
            if bulk_data and bulk_data.get('code') == 0:
                players_data = bulk_data.get('result', {})
                
                # Calculate online players from this same data
                for pid, player_data in players_data.items():
                    player_base = player_data.get('base', {})
                    if player_base.get('is_online', 0) == 1:
                        online += 1
                        online_player_names.append(player_base.get('nickname', 'Unknown'))
        except Exception as e:
            logger.warning(f"Failed to get bulk player data, falling back to estimate: {e}")
            # Fallback to old estimation method
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


        # 🔥 TOP 10 WEEKLY ACTIVITY POINTS
        # Extract and sort members by weekly liveness
        weekly_leaderboard = []
        
        # ✅ USE ALREADY FETCHED DATA - NO SECOND API CALL!
        if players_data is not None:
            for pid, member in member_list.items():
                nickname = member.get('nickname', 'Unknown')
                weekly_points = 0
                
                if pid in players_data:
                    player_data = players_data[pid]
                    club_data = player_data.get('club', {})
                    base_data = player_data.get('base', {})
                    weekly_points = club_data.get('liveness', 0)
                    # Get real nickname from base data if available
                    if 'nickname' in base_data:
                        nickname = base_data.get('nickname', nickname)
                
                weekly_leaderboard.append( (-weekly_points, nickname, weekly_points) )
        else:
            # Fallback if API fails
            for pid, member in member_list.items():
                nickname = member.get('nickname', 'Unknown')
                club_data = member.get('club', {})
                weekly_points = club_data.get('liveness', 0)
                weekly_leaderboard.append( (-weekly_points, nickname, weekly_points) )

        # Sort and take top 10
        weekly_leaderboard.sort()

        # Build leaderboard in code block with backticked values
        lines.append("\n## 🔥 WEEKLY ACTIVITY POINTS - TOP 10")
        lines.append("```")
        
        for rank, (neg_points, name, points) in enumerate(weekly_leaderboard[:10], 1):
            # Add medal emojis for top 3
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
        
        return "\n".join(lines), embeds, online, member_count
    
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
                        status_message, embeds, _, _ = self._build_status_board(guild_data)
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
            status_message, embeds, _, _ = self._build_status_board(guild_data)
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
        # Send initial message immediately
        await interaction.response.send_message("🔍 Searching for player...")

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

            await interaction.edit_original_response(content="✅ Found player\n🏰 Looking up guild info...")
            
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

            await interaction.edit_original_response(content="✅ Found player\n🏰 Looking up guild info...\n📋 Loading guild data...")
            
            # Now fetch full guild information
            guild_data = get_full_guild_info(target_guild_id, hostnum=target_hostnum)
            
            if not guild_data or 'result' not in guild_data:
                embed = discord.Embed(
                    title="❌ Guild not found",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(content=None, embed=embed)
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

            await interaction.edit_original_response(content=None, embed=embed)

        except Exception as e:
            logger.error(f"Guild search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Search Failed",
                description=f"An error occurred while searching: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)


    @guild_group.command(name="player-count", description="Display graph of online player count over time")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        range="Time range for the graph"
    )
    @app_commands.choices(range=[
        app_commands.Choice(name="Today (5am GMT+8 to now)", value="today"),
        app_commands.Choice(name="This Week (current schedule week)", value="week"),
        app_commands.Choice(name="Last 7 Days", value="7days"),
    ])
    async def player_count_graph(self, interaction: discord.Interaction, range: str = "today"):
        """Show a graph of online player counts over time"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import MaxNLocator
            import datetime as dt
            
            now_utc = dt.datetime.now(dt.timezone.utc)
            
            # Calculate time range based on the 5am GMT+8 schedule day logic
            GMT8_OFFSET = 8 * 3600
            now_ts = int(now_utc.timestamp())
            
            if range == "today":
                # Start of today's schedule day (5am GMT+8)
                gmt8_now = now_ts + GMT8_OFFSET
                gmt8_dt = dt.datetime.fromtimestamp(gmt8_now, tz=dt.timezone.utc)
                schedule_start = gmt8_dt.replace(hour=5, minute=0, second=0, microsecond=0)
                # If current time is before 5am, start from previous day
                if gmt8_dt.hour < 5:
                    schedule_start -= dt.timedelta(days=1)
                start_ts = int(schedule_start.timestamp() - GMT8_OFFSET)
                
            elif range == "week":
                # Start of the current schedule week (Monday 5am GMT+8)
                gmt8_now = now_ts + GMT8_OFFSET
                gmt8_dt = dt.datetime.fromtimestamp(gmt8_now, tz=dt.timezone.utc)
                adjusted = gmt8_dt - dt.timedelta(hours=5)
                # Monday of that week
                monday = adjusted - dt.timedelta(days=adjusted.weekday())
                schedule_start = monday.replace(hour=5, minute=0, second=0, microsecond=0)
                start_ts = int(schedule_start.timestamp() - GMT8_OFFSET)
                
            else:  # 7days
                start_ts = now_ts - 7 * 86400
            
            # Query data from database
            with sqlite3.connect(self.db_path) as db:
                db.row_factory = sqlite3.Row
                cursor = db.execute(
                    "SELECT ts, online_count, total_members FROM guild_player_counts WHERE ts >= ? ORDER BY ts ASC",
                    (start_ts,)
                )
                rows = cursor.fetchall()
            
            if not rows:
                await interaction.followup.send("❌ No data available for the selected time range.", ephemeral=True)
                return
            
            # Prepare data
            timestamps = [row['ts'] for row in rows]
            online_counts = [row['online_count'] for row in rows]
            
            # Convert timestamps to datetime objects
            dates = [dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc) for ts in timestamps]
            
            # Create plot with dark theme
            plt.style.use('dark_background')
            fig, ax = plt.subplots(figsize=(12, 6))
            
            # Plot the data
            ax.fill_between(dates, online_counts, alpha=0.3, color='#2ECC71')
            ax.plot(dates, online_counts, color='#2ECC71', linewidth=2, marker='', linestyle='-')
            
            # Also add a rolling average line for smoother view
            if len(online_counts) >= 10:
                # Simple moving average (10 data points ~ 10 minutes)
                window = min(10, len(online_counts) // 3)
                if window > 1:
                    import numpy as np
                    weights = np.ones(window) / window
                    smoothed = np.convolve(online_counts, weights, mode='valid')
                    # Adjust dates to match smoothed array
                    smooth_dates = dates[window-1:]
                    ax.plot(smooth_dates, smoothed, color='#FFD700', linewidth=1.5, linestyle='--', alpha=0.7, label='Trend')
            
            # Style the chart
            ax.set_facecolor('#1a1a2e')
            fig.patch.set_facecolor('#1a1a2e')
            ax.grid(True, alpha=0.2, color='white')
            
            ax.set_xlabel('Time (GMT+8)', color='white', fontsize=12)
            ax.set_ylabel('Online Players', color='white', fontsize=12)
            
            # Title with range info
            range_labels = {"today": "Today", "week": "This Week", "7days": "Last 7 Days"}
            ax.set_title(f'Online Player Count - {range_labels.get(range, "Custom")}', 
                        color='white', fontsize=14, fontweight='bold')
            
            # Format x-axis
            if range == "today":
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
            elif range == "week":
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%a %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
            else:
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M', tz=dt.timezone(dt.timedelta(hours=8))))
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=12))
            
            plt.xticks(rotation=45, color='white')
            plt.yticks(color='white')
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            
            # Legend
            ax.legend(['Online Players', 'Trend'], loc='upper right', 
                     facecolor='#1a1a2e', edgecolor='white', labelcolor='white')
            
            # Adjust layout
            plt.tight_layout()
            
            # Save to bytes
            import io
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            plt.close()
            
            # Send the graph
            file = discord.File(buf, filename='player_count.png')
            
            # Calculate peak info
            peak_count = max(online_counts)
            peak_ts = timestamps[online_counts.index(peak_count)]
            peak_dt = dt.datetime.fromtimestamp(peak_ts, tz=dt.timezone.utc) + dt.timedelta(hours=8)
            avg_count = round(sum(online_counts) / len(online_counts), 1)
            
            embed = discord.Embed(
                title="📊 Player Count Statistics",
                color=discord.Color.green()
            )
            embed.add_field(name="📈 Peak Online", value=f"`{peak_count} players`", inline=True)
            embed.add_field(name="📉 Average Online", value=f"`{avg_count} players`", inline=True)
            embed.add_field(name="📊 Data Points", value=f"`{len(rows)}`", inline=True)
            embed.set_image(url="attachment://player_count.png")
            embed.set_footer(text=f"Time range: {range_labels.get(range, 'Custom')} | Data recorded every 1 minute")
            
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
            
        except ImportError as e:
            await interaction.followup.send(f"❌ Missing dependency: `{e}`. Please ensure matplotlib and numpy are installed.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to generate player count graph: {str(e)}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to generate graph: `{str(e)}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WWMCog(bot))
