import asyncio
import json
import os
from datetime import datetime
from typing import Optional, Set
import discord
from discord.ext import commands, tasks
from settings import logger
from utility.wwm import get_club_chat, get_custom_guild_info, get_bulk_players_info, get_film_plan, get_teams_info
from utility.api_constants import get_kongfu_ids_from_player, format_kongfu_display
from googletrans import Translator


class LiveChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_seen_msg_ids: Set[str] = set()
        self.is_running = False
        self.translator = Translator()
        self._translate_retry_delay = 2.0
        self._translate_max_retries = 2
        # Configuration
        self.CONFIG_FILE = "data/live_chat_config.json"
        self.CLUB_ID = "aRvTyiPA8WMSXrRj"      # Your guild ID
        self.HOSTNUM = 10103                    # Your server hostnum
        self.CHANNEL_ID = None                  # Set via /chatenable command
        self.POLL_INTERVAL = 10                 # Seconds between checks
        self.ranks = None                       # To store rank information
        # Team-up alert configuration
        self.TEAMUP_CHANNEL_ID = 1442853064053756028  # General channel for teamup pings
        self.TEAMUP_ROLE_ID = 1488830429686665246     # Team Up role
        self.TEAMUP_KEYWORD = "@teamup"               # Trigger keyword
        self.TEAMUP_EMBED_COLOR = 0xE74C3C            # Red

        # Ensure data directory exists
        os.makedirs("data", exist_ok=True)
        
        # Load saved configuration
        self.load_config()
        
        # Only start poller if already enabled in config
        if self.is_running and self.CHANNEL_ID:
            # Reset running flag to force proper initialization on first poll
            self.is_running = False
            self.chat_poller.start()
            logger.debug("✅ Live chat auto-started from saved configuration")

    def cog_unload(self):
        self.chat_poller.cancel()
        self.save_config()

    @tasks.loop(seconds=10)
    async def chat_poller(self):
        if not self.bot.is_ready():
            return
            
        try:
            # Fetch chat data in thread pool (avoid blocking event loop)
            chat_result = await asyncio.to_thread(get_club_chat, self.CLUB_ID, self.HOSTNUM)
            
            if not chat_result or 'result' not in chat_result or 'chat' not in chat_result['result']:
                logger.debug("No chat data received")
                return
                
            chat_messages = chat_result['result']['chat']['chat_history']
            
            # First run: just mark all messages as seen
            if not self.is_running:
                self.is_running = True
                self.last_seen_msg_ids = {msg['msg_id'] for msg in chat_messages if 'msg_id' in msg}
                logger.debug(f"✅ Live chat monitoring started. Tracked {len(self.last_seen_msg_ids)} existing messages.")
                return
            
            # Process new messages
            new_messages = []
            for msg in chat_messages:
                msg_id = msg.get('msg_id')
                if not msg_id or msg_id in self.last_seen_msg_ids:
                    continue
                    
                new_messages.append(msg)
                self.last_seen_msg_ids.add(msg_id)
            
            if new_messages:
                logger.debug(f"🔔 Found {len(new_messages)} new chat messages")

                # Call guild api so that we can get rank of sender and other info that might not be included in chat message data
                self.ranks = await asyncio.to_thread(get_custom_guild_info, self.CLUB_ID, self.HOSTNUM, {'members': ['custom_posts']})
                self.ranks = self.ranks.get('result', {}).get('members', {}).get('custom_posts', {}) if self.ranks else {}
                #logger.info(f"Ranks data: {self.ranks}")
                # Sort messages by timestamp (oldest first)
                new_messages.sort(key=lambda x: x.get('ts', 0))
                
                # Post to Discord
                channel = self.bot.get_channel(self.CHANNEL_ID)
                teamup_channel = self.bot.get_channel(self.TEAMUP_CHANNEL_ID)
                if channel:
                    for msg in new_messages:
                        # Check for exhibition video before formatting embed
                        ext = msg.get('ext', {})
                        msg_type = ext.get('msg_type', 'msg_normal')
                        msg_label = msg.get('msg', '').strip()
                        video_url = None
                        
                        if msg_type == 'msg_artwork_card' and msg_label == "[Exhibition]":
                            artwork_data = ext.get('extra_data', {}).get('artwork_data', {})
                            plan_id = artwork_data.get('plan_id', '')
                            if plan_id:
                                film_data = await asyncio.to_thread(get_film_plan, plan_id)
                                if film_data and 'result' in film_data:
                                    video_url = film_data['result'].get('video_url', '')
                                    # Store film data in ext for format_message_embed to use
                                    video_name = film_data['result'].get('name', '')
                                    video_msg = film_data['result'].get('msg', '')
                                    video_hot = film_data['result'].get('hot', '')
                                    ext['film_data'] = film_data['result']
                        
                        if not video_url:
                            embed = await self.format_message_embed(msg)
                        
                            message = await channel.send(embed=embed)
                        else:
                            ts = int(msg.get('ts', 0))
                            nickname = msg.get('nickname', 'Unknown')
                            level = msg.get('level', 0)
                            ext = msg.get('ext', {})
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

                            message = f"[Exhibition] [{video_name if video_name else 'Unknown'}]({video_url})"
                            if video_msg:
                                message += f"\n{video_msg}"
                            if video_hot:
                                message += f"| ❤️ {video_hot}"

                            name=f"{nickname} ({rank_name}) (Lv.{level})" if rank_name != "Unknown" else f"{nickname} (Lv.{level})"
                            message = await channel.send(f"### {name}\n{message}\n\n<t:{ts}:F> (<t:{ts}:R>)")
                        
                        # Check for @teamup keyword in message
                        raw_msg = msg.get('msg', '').strip().lower()
                        if self.TEAMUP_KEYWORD in raw_msg:
                            await self.send_teamup_alert(msg, teamup_channel)
                
                # Keep only last 200 message IDs to prevent memory leak
                if len(self.last_seen_msg_ids) > 500:
                    self.last_seen_msg_ids = set(list(self.last_seen_msg_ids)[-300:])
                
        except Exception as e:
            logger.error(f"Error in chat poller: {str(e)}", exc_info=True)

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
        
        embed = discord.Embed(
            description=f"{message}\n\n<t:{ts}:F> (<t:{ts}:R>)",
            color=channel_colors.get(channel_type, 0x3498DB)
        )
        
        embed.set_author(
            # If rank_name is "Unknown", it will just show nickname without rank
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

        # Build embed description
        description = f"**{nickname}**"
        if rank_name != "Unknown":
            description += f" ({rank_name})"
        description += f" (Lv.{level})"
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

    def get_avatar_url(self, head_id: int) -> str:
        """Get avatar icon URL for given head ID"""
        return f"https://h72static.easebar.com/head/{head_id}.png"

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
                logger.debug(f"Loaded live chat config: enabled={self.is_running}, channel={self.CHANNEL_ID}")
            except Exception as e:
                logger.error(f"Failed to load live chat config: {str(e)}")

    def save_config(self):
        """Save current configuration to file"""
        try:
            config = {
                'channel_id': self.CHANNEL_ID,
                'enabled': self.is_running,
                'last_msg_ids': list(self.last_seen_msg_ids)[-300:]
            }
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=4)
            logger.debug("Saved live chat configuration")
        except Exception as e:
            logger.error(f"Failed to save live chat config: {str(e)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveChatCog(bot))
