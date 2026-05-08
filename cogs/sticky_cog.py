import discord
import json
import os
import asyncio
from discord.ext import commands
from settings import logger, BASE_DIR

STICKY_CHANNEL_ID = 1463479585567150194
BINDING_CHANNEL_ID = 1469961307154288703
IDLE_TIMEOUT = 180  # 3 minutes in seconds
CONFIG_FILE = BASE_DIR / "data" / "sticky_config.json"


class StickyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sticky_message_id = None
        self.idle_task = None
        self.last_message_id = None
        
        # Load saved sticky message ID
        self.load_config()
        
    def load_config(self):
        """Load saved sticky message ID from file"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.sticky_message_id = config.get('sticky_message_id')
                    self.last_message_id = config.get('last_message_id')
                logger.debug(f"Loaded sticky config: message_id={self.sticky_message_id}")
            except Exception as e:
                logger.error(f"Failed to load sticky config: {e}")
    
    def save_config(self):
        """Save sticky message ID to file"""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump({
                    'sticky_message_id': self.sticky_message_id,
                    'last_message_id': self.last_message_id
                }, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save sticky config: {e}")
    
    async def delete_sticky_message(self):
        """Delete the old sticky message if it exists"""
        if not self.sticky_message_id:
            return
        
        try:
            channel = self.bot.get_channel(STICKY_CHANNEL_ID)
            if channel:
                try:
                    old_msg = await channel.fetch_message(self.sticky_message_id)
                    await old_msg.delete()
                    logger.debug("Deleted old sticky message")
                except discord.NotFound:
                    pass  # Already deleted
                except discord.Forbidden:
                    pass  # No permission
        except Exception as e:
            logger.warning(f"Failed to delete old sticky message: {e}")
        
        self.sticky_message_id = None
        self.save_config()
    
    async def send_sticky_message(self):
        """Delete old sticky and send a new one"""
        await self.delete_sticky_message()
        
        try:
            channel = self.bot.get_channel(STICKY_CHANNEL_ID)
            if not channel:
                logger.warning(f"Sticky channel {STICKY_CHANNEL_ID} not found")
                return
            
            embed = discord.Embed(
                title="📖 Guild Bot Commands",
                description="Welcome! Use these commands to get the most out of the bot.",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="🔗 Step 1: Bind Your Account",
                value=f"Before using most features, link your game account first in <#{BINDING_CHANNEL_ID}>.",
                inline=False
            )
            
            embed.add_field(
                name="👤 /player search <number_id>",
                value="Look up any player's profile, stats, and guild information.\n"
                      "Works for any player even without binding.\n"
                      "Bound users get access to full stats.",
                inline=False
            )
            
            embed.add_field(
                name="🏰 /guild search <player_id>",
                value="Search for a guild by entering any of its member's 10-digit Number ID.\n"
                      "**Requires** a bound account.",
                inline=False
            )
            
            embed.add_field(
                name="💡 Tips",
                value="• All commands start with `/`\n"
                      "• Player Number IDs are 10 digits long\n"
                      "• Bind your account to unlock full features",
                inline=False
            )
            
            embed.set_footer(text="This message appears after 3 minutes of inactivity")
            
            message = await channel.send(embed=embed)
            self.sticky_message_id = message.id
            self.save_config()
            logger.info(f"Sticky message sent (ID: {message.id})")
            
        except Exception as e:
            logger.error(f"Failed to send sticky message: {e}", exc_info=True)
    
    async def reset_idle_timer(self):
        """Cancel existing idle task and start a new one"""
        if self.idle_task:
            self.idle_task.cancel()
        
        self.idle_task = asyncio.create_task(self.idle_timeout())
    
    async def idle_timeout(self):
        """Wait for idle timeout then send sticky"""
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            await self.send_sticky_message()
        except asyncio.CancelledError:
            pass  # Timer was reset, that's fine
    
    @commands.Cog.listener()
    async def on_ready(self):
        """On bot ready, delete any leftover sticky and reset timer"""
        # Delete old sticky if it exists from previous session
        await self.delete_sticky_message()
        self.sticky_message_id = None
        self.last_message_id = None
        self.save_config()
        
        # Start idle timer from bot start
        await self.reset_idle_timer()
        logger.info("✅ StickyCog ready")
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Watch for messages in the sticky channel"""
        if message.author.bot:
            return
        
        if message.channel.id != STICKY_CHANNEL_ID:
            return
        
        # Update last message tracking
        self.last_message_id = message.id
        
        # Reset the idle timer - sticky will appear after 3 minutes of silence
        await self.reset_idle_timer()


async def setup(bot: commands.Bot):
    await bot.add_cog(StickyCog(bot))
    logger.info("✅ StickyCog loaded")