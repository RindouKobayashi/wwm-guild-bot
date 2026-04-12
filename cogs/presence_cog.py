import discord
import random
import sqlite3
import os
from datetime import datetime
from discord.ext import commands, tasks
from settings import logger

# Database path for activity data
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "activity.db")

class PresenceCog(commands.Cog):
    """Handles the bot's auto-rotating presence."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_count = 0
        self.current_status = None
        self.change_status.start()

    def cog_unload(self):
        self.change_status.cancel()

    def get_statuses(self):
        """Get a list of possible statuses, including activity database stats."""
        statuses = []
        
        # 1. Guild count status
        guild_count = len(self.bot.guilds)
        statuses.append((discord.ActivityType.watching, f"{guild_count} servers"))
        
        # 2. Activity database stats
        if os.path.exists(DB_PATH):
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    
                    # Get today's top scorer across all guilds
                    from cogs.activity_cog import get_today_session_id
                    session_id = get_today_session_id()
                    cursor.execute("""
                        SELECT user_id, SUM(points) as total 
                        FROM activity_session 
                        WHERE session_id = ?
                        GROUP BY user_id 
                        ORDER BY total DESC 
                        LIMIT 1
                    """, (session_id,))
                    result = cursor.fetchone()
                    if result and result[1] > 0:
                        user_id, points = result
                        # Try to get the user from cache
                        user = self.bot.get_user(user_id)
                        if user:
                            statuses.append((discord.ActivityType.competing, f"Top scorer: {user.name}"))
                        else:
                            statuses.append((discord.ActivityType.competing, f"Top scorer: {points} pts"))
                    
                    # Get total points tracked (all-time)
                    cursor.execute("SELECT SUM(points) FROM activity_alltime")
                    total = cursor.fetchone()[0] or 0
                    if total > 0:
                        statuses.append((discord.ActivityType.watching, f"{total} messages tracked"))
                    
                    # Get number of active users today
                    cursor.execute("""
                        SELECT COUNT(DISTINCT user_id) FROM activity_session 
                        WHERE session_id = ? AND points > 0
                    """, (session_id,))
                    active_users = cursor.fetchone()[0] or 0
                    if active_users > 0:
                        statuses.append((discord.ActivityType.listening, f"{active_users} active users"))
                        
            except Exception as e:
                logger.debug(f"Could not get activity stats: {e}")
        
        # 3. Member count across all guilds
        total_members = sum(g.member_count for g in self.bot.guilds)
        statuses.append((discord.ActivityType.listening, f"{total_members} members"))
        
        return statuses

    @tasks.loop(seconds=12)
    async def change_status(self):
        """Changes the bot's presence every 12 seconds."""
        try:
            self.guild_count = len(self.bot.guilds)
            available_statuses = self.get_statuses()
            if not available_statuses:
                return
            
            self.current_status = random.choice(available_statuses)
            activity_type, status_text = self.current_status
            activity = discord.Activity(type=activity_type, name=status_text)
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            logger.error(f"Error changing presence: {e}")

    @change_status.before_loop
    async def before_change_status(self):
        """Waits until the bot is ready before starting the presence loop."""
        await self.bot.wait_until_ready()

    

async def setup(bot: commands.Bot):
    """Adds the PresenceCog to the bot."""
    await bot.add_cog(PresenceCog(bot))
