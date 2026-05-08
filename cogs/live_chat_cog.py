#!/usr/bin/env python3
"""
Live Guild Chat Cog
Automatically polls WWM guild chat every 10 seconds and posts new messages to Discord
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Optional, Set
import discord
from discord.ext import commands, tasks
from settings import logger
from utility.wwm import get_club_chat


class LiveChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_seen_msg_ids: Set[str] = set()
        self.is_running = False
        
        # Configuration
        self.CONFIG_FILE = "data/live_chat_config.json"
        self.CLUB_ID = "aRvTyiPA8WMSXrRj"      # Your guild ID
        self.HOSTNUM = 10103                    # Your server hostnum
        self.CHANNEL_ID = None                  # Set via /chatenable command
        self.POLL_INTERVAL = 10                 # Seconds between checks
        
        # Ensure data directory exists
        os.makedirs("data", exist_ok=True)
        
        # Load saved configuration
        self.load_config()
        
        # Only start poller if already enabled in config
        if self.is_running and self.CHANNEL_ID:
            # Reset running flag to force proper initialization on first poll
            self.is_running = False
            self.chat_poller.start()
            logger.info("✅ Live chat auto-started from saved configuration")

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
                logger.info(f"✅ Live chat monitoring started. Tracked {len(self.last_seen_msg_ids)} existing messages.")
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
                logger.info(f"🔔 Found {len(new_messages)} new chat messages")
                
                # Sort messages by timestamp (oldest first)
                new_messages.sort(key=lambda x: x.get('ts', 0))
                
                # Post to Discord
                channel = self.bot.get_channel(self.CHANNEL_ID)
                if channel:
                    for msg in new_messages:
                        embed = self.format_message_embed(msg)
                        await channel.send(embed=embed)
                
                # Keep only last 200 message IDs to prevent memory leak
                if len(self.last_seen_msg_ids) > 500:
                    self.last_seen_msg_ids = set(list(self.last_seen_msg_ids)[-300:])
                
        except Exception as e:
            logger.error(f"Error in chat poller: {str(e)}", exc_info=True)

    def format_message_embed(self, msg: dict) -> discord.Embed:
        """Format chat message into Discord embed"""
        ts = int(msg.get('ts', 0))
        nickname = msg.get('nickname', 'Unknown')
        level = msg.get('level', 0)
        # Handle messages with empty msg field (e.g. shared activity cards)
        message = msg.get('msg', '').strip()
        if not message:
            ext = msg.get('ext', {})
            share_text = ext.get('share_text_info') or ext.get('extra_data', {}).get('share_text_info')
            if share_text:
                message = "[Shared] " + ", ".join(share_text)
            elif ext.get('msg_type') == 'club_gonggao':
                message = msg.get('msg', '')  # Keep original for announcements
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
            name=f"{nickname} (Lv.{level})"
        )
        
        return embed

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
    logger.info("✅ LiveChatCog loaded successfully")
