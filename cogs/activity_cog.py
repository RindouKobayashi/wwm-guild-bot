import discord
import aiosqlite
import time
import os
import settings
from datetime import datetime, timezone, timedelta
from discord.ext import commands, tasks
from discord import app_commands
from settings import logger

# Database path
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "activity.db")

# GMT+8 timezone
GMT8 = timezone(timedelta(hours=8))

# Reset time: 5am GMT+8
RESET_HOUR = 5

def get_today_session_id() -> str:
    """Get today's session ID based on GMT+8 with 5am as day boundary."""
    now = datetime.now(GMT8)
    # Shift the "day" so RESET_HOUR (5am) is the start of the day
    # At 4:59am on April 2nd, this gives April 1st's date
    # At 5:00am on April 2nd, this gives April 2nd's date
    shifted = now - timedelta(hours=RESET_HOUR)
    return f"{shifted.year}-{shifted.month:02d}-{shifted.day:02d}"

def get_next_reset_time() -> datetime:
    """Get the next 5am GMT+8 reset time, accounting for 5am day boundary."""
    now = datetime.now(GMT8)
    # Calculate the "shifted" date to determine the current session day
    shifted = now - timedelta(hours=RESET_HOUR)
    # The next reset is at 5am on the next shifted day
    next_shifted_day = shifted.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return next_shifted_day


class ActivityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.activity_check.start()
        self.reset_check.start()

    async def cog_load(self):
        await self._init_db()
    
    def cog_unload(self):
        """Cancel background tasks when cog is unloaded."""
        self.activity_check.cancel()
        self.reset_check.cancel()
    
    async def _init_db(self):
        """Initialize the database and create tables if they don't exist."""
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as conn:
            # All-time points table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS activity_alltime (
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    points INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, guild_id)
                )
            ''')
            # Session points table (resets daily)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS activity_session (
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    points INTEGER DEFAULT 0,
                    last_point_time REAL DEFAULT 0,
                    PRIMARY KEY (user_id, guild_id, session_id)
                )
            ''')
            await conn.commit()
        logger.info("Activity database initialized with all-time and session tables")
    
    async def _update_points(self, user_id: int, guild_id: int):
        """Update user points if cooldown has passed. Updates both session and all-time."""
        now = time.time()
        cooldown = 15  # 1 point per 15 seconds
        session_id = get_today_session_id()
        
        async with aiosqlite.connect(DB_PATH) as conn:
            # Update session points
            cursor = await conn.execute(
                "SELECT points, last_point_time FROM activity_session WHERE user_id = ? AND guild_id = ? AND session_id = ?",
                (user_id, guild_id, session_id)
            )
            result = await cursor.fetchone()
            
            session_points = 0
            if result:
                points, last_time = result
                if now - last_time >= cooldown:
                    new_points = points + 1
                    await conn.execute(
                        "UPDATE activity_session SET points = ?, last_point_time = ? WHERE user_id = ? AND guild_id = ? AND session_id = ?",
                        (new_points, now, user_id, guild_id, session_id)
                    )
                    session_points = new_points
                else:
                    return None  # Cooldown not passed
            else:
                await conn.execute(
                    "INSERT INTO activity_session (user_id, guild_id, session_id, points, last_point_time) VALUES (?, ?, ?, 1, ?)",
                    (user_id, guild_id, session_id, now)
                )
                session_points = 1
            
            # Update all-time points
            cursor = await conn.execute(
                "SELECT points FROM activity_alltime WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id)
            )
            alltime_result = await cursor.fetchone()
            
            if alltime_result:
                await conn.execute(
                    "UPDATE activity_alltime SET points = points + 1 WHERE user_id = ? AND guild_id = ?",
                    (user_id, guild_id)
                )
            else:
                await conn.execute(
                    "INSERT INTO activity_alltime (user_id, guild_id, points) VALUES (?, ?, 1)",
                    (user_id, guild_id)
                )
            
            await conn.commit()
            return session_points
    
    async def _check_has_special_role(self, guild: discord.Guild, user_id: int) -> bool:
        """Check if a user has any special roles defined in settings."""
        member = guild.get_member(user_id)
        if not member:
            return False
        special_role_ids = set(settings.SPECIAL_ROLES.values())
        return any(role.id in special_role_ids for role in member.roles)
    
    async def _get_session_candidates(self, guild_id: int) -> list:
        """Get all users ordered by session points, for filtering special role users."""
        session_id = get_today_session_id()
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT user_id, points FROM activity_session WHERE guild_id = ? AND session_id = ? ORDER BY points DESC, last_point_time ASC",
                (guild_id, session_id)
            )
            return await cursor.fetchall()
    
    async def _get_eligible_leader(self, guild: discord.Guild) -> tuple:
        """Get the activity leader who is eligible for the role (no special roles).
        
        Returns:
            (user_id, session_points) of the eligible leader, or None if no eligible user.
            Users with special roles are skipped - role only goes to users without special roles.
        """
        candidates = await self._get_session_candidates(guild.id)
        for user_id, points in candidates:
            has_special = await self._check_has_special_role(guild, user_id)
            if not has_special:
                return (user_id, points)
        return None
    
    async def _get_leader_before_update(self, guild: discord.Guild, updating_user_id: int, new_points: int) -> str:
        """Get who should hold the leader role after a user's points update.
        
        Returns:
            User ID of the leader, or None if no eligible user.
            - Users with special roles cannot hold the activity leader role
            - Current eligible leader keeps the role until someone STRICTLY exceeds their points
        """
        candidates = await self._get_session_candidates(guild.id)
        
        # Find the current eligible leader (without special role)
        current_eligible_leader_id = None
        current_eligible_points = 0
        
        for uid, pts in candidates:
            has_special = await self._check_has_special_role(guild, uid)
            if not has_special:
                current_eligible_leader_id = uid
                current_eligible_points = pts
                break
        
        # Check if updating user is eligible
        updating_user_has_special = await self._check_has_special_role(guild, updating_user_id)
        
        if updating_user_has_special:
            # Updating user has special role, cannot be leader
            return current_eligible_leader_id
        
        # Updating user is eligible - check if they now strictly exceed current leader
        if current_eligible_leader_id is None or new_points > current_eligible_points:
            return updating_user_id
        
        # Current leader keeps the role (even on tie)
        return current_eligible_leader_id
    
    async def _get_user_session_points(self, user_id: int, guild_id: int) -> int:
        """Get a user's session points for today."""
        session_id = get_today_session_id()
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT points FROM activity_session WHERE user_id = ? AND guild_id = ? AND session_id = ?",
                (user_id, guild_id, session_id)
            )
            result = await cursor.fetchone()
            return result[0] if result else 0
    
    async def _get_user_alltime_points(self, user_id: int, guild_id: int) -> int:
        """Get a user's all-time points."""
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT points FROM activity_alltime WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id)
            )
            result = await cursor.fetchone()
            return result[0] if result else 0
    
    def _reset_session_points(self, guild_id: int):
        """Reset all session points for a new day. Old session data is kept for history."""
        # We don't delete old sessions - they stay for historical reference
        # New day will naturally have no entries, treating everyone as 0 points
        logger.info(f"Session points reset for guild {guild_id} - new day started")
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for messages and award points."""
        # Ignore bots, DMs, and blacklisted channels
        if message.author.bot:
            return
        if not message.guild:
            return
        if message.channel.id in settings.ACTIVITY_BLACKLIST_CHANNELS:
            return
        
        # Update points
        new_points = await self._update_points(message.author.id, message.guild.id)
        
        if new_points is not None:
            alltime_pts = await self._get_user_alltime_points(message.author.id, message.guild.id)
            logger.debug(f"User {message.author} earned a point! Session: {new_points}, All-time: {alltime_pts}, Session ID: {get_today_session_id()}")
            
            # Check if they're now the leader (must strictly exceed current leader)
            leader_id = await self._get_leader_before_update(message.guild, message.author.id, new_points)
            if leader_id == message.author.id:
                await self._assign_leader_role(message.guild, message.author)
    
    async def _assign_leader_role(self, guild: discord.Guild, new_leader: discord.Member):
        """Assign the leader role to the new leader and remove it from previous leader."""
        leader_role_id = settings.ACTIVITY_LEADER_ROLE_ID
        leader_role = guild.get_role(leader_role_id)
        
        if not leader_role:
            logger.warning(f"Leader role not found with ID {leader_role_id}")
            return
        
        # Remove role from all members who have it (in case of multiple)
        for member in leader_role.members:
            if member.id != new_leader.id:
                try:
                    await member.remove_roles(leader_role, reason="Activity leader updated")
                    logger.debug(f"Removed leader role from {member}")
                except discord.Forbidden:
                    logger.error(f"Cannot remove role from {member} - insufficient permissions")
        
        # Add role to new leader
        if new_leader:
            try:
                await new_leader.add_roles(leader_role, reason="New activity leader")
                logger.debug(f"Gave leader role to {new_leader}")
            except discord.Forbidden:
                logger.error(f"Cannot give role to {new_leader} - insufficient permissions")
    
    @tasks.loop(hours=1)
    async def activity_check(self):
        """Periodically check and update the activity leader."""
        logger.debug("Running periodic activity leader check...")
        for guild in self.bot.guilds:
            leader = await self._get_eligible_leader(guild)
            if leader:
                member = guild.get_member(leader[0])
                if member:
                    await self._assign_leader_role(guild, member)
                    logger.debug(f"Updated leader for {guild.name}: {member.name} with {leader[1]} session points")
                else:
                    logger.warning(f"Eligible leader (user ID: {leader[0]}) not found in guild {guild.name}")
            else:
                logger.debug(f"No eligible leader found for {guild.name}")
    
    @tasks.loop(minutes=5)
    async def reset_check(self):
        """Check if it's time to reset session points (5am GMT+8)."""
        now = datetime.now(GMT8)
        reset_time = now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
        self._last_reset = getattr(self, '_last_reset', None)
        
        # Check if we've crossed the reset threshold
        if now >= reset_time:
            today_session = get_today_session_id()
            if self._last_reset != today_session:
                logger.info(f"Daily reset triggered at {now} (GMT+8)")
                self._last_reset = today_session
                for guild in self.bot.guilds:
                    self._reset_session_points(guild.id)
                # Check for new leader after reset
                await self.activity_check()
    
    @activity_check.before_loop
    async def before_activity_check(self):
        await self.bot.wait_until_ready()
    
    @reset_check.before_loop
    async def before_reset_check(self):
        await self.bot.wait_until_ready()
        # Initialize last_reset to today so we don't reset on startup
        self._last_reset = get_today_session_id()
    
    @app_commands.command(name="activity_leader", description="View the current most active member today")
    async def activity_leader(self, interaction: discord.Interaction):
        """Show the current activity leader."""
        leader = await self._get_eligible_leader(interaction.guild)
        
        if not leader:
            await interaction.response.send_message("No activity data recorded today!")
            return
        
        member = interaction.guild.get_member(leader[0])
        if member:
            alltime_points = await self._get_user_alltime_points(member.id, interaction.guild.id)
            alltime_rank = await self._get_alltime_rank(member.id, interaction.guild.id)
            embed = discord.Embed(
                title="🏆 Today's Most Active Member",
                description=f"**{member.display_name}** is leading today with **{leader[1]} points!**\n\n*All-time rank: #{alltime_rank} ({alltime_points} total points)*",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"The leader (User ID: {leader[0]}) is no longer in the server with **{leader[1]} points**")
    
    @app_commands.command(name="activity_stats", description="View activity stats for yourself or another user")
    @app_commands.describe(user="The user to check (default: yourself)")
    async def activity_stats(self, interaction: discord.Interaction, user: discord.Member = None):
        """Show activity stats for a user."""
        target = user or interaction.user
        session_points = await self._get_user_session_points(target.id, interaction.guild.id)
        alltime_points = await self._get_user_alltime_points(target.id, interaction.guild.id)
        
        session_rank = await self._get_session_rank(target.id, interaction.guild.id)
        alltime_rank = await self._get_alltime_rank(target.id, interaction.guild.id)
        
        wasted_today = self._format_wasted_time(session_points)
        wasted_alltime = self._format_wasted_time(alltime_points)
        
        embed = discord.Embed(
            title="📊 Activity Stats",
            color=target.color
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="🌟 Today's Points", value=str(session_points), inline=True)
        embed.add_field(name="🏅 Today's Rank", value=f"#{session_rank}", inline=True)
        embed.add_field(name="⏱️ Wasted Today", value=wasted_today, inline=True)
        embed.add_field(name="⭐ All-Time Points", value=str(alltime_points), inline=True)
        embed.add_field(name="🏆 All-Time Rank", value=f"#{alltime_rank}", inline=True)
        embed.add_field(name="💀 Lifetime Wasted", value=wasted_alltime, inline=True)
        embed.set_footer(text="Session resets daily at 5:00 AM (GMT+8) | 1 point = 15 seconds active chatting")
        
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="activity_leaderboard", description="View the activity leaderboard")
    @app_commands.describe(view="Which leaderboard to view")
    @app_commands.choices(view=[
        app_commands.Choice(name="Today's Activity", value="today"),
        app_commands.Choice(name="All-Time Activity", value="alltime"),
    ])
    async def activity_leaderboard(self, interaction: discord.Interaction, view: str):
        """Show the activity leaderboard."""
        session_id = get_today_session_id()
        
        async with aiosqlite.connect(DB_PATH) as conn:
            if view == "today":
                cursor = await conn.execute(
                    "SELECT user_id, points FROM activity_session WHERE guild_id = ? AND session_id = ? ORDER BY points DESC, last_point_time ASC LIMIT 15",
                    (interaction.guild.id, session_id)
                )
                title = "🏆 Today's Activity Leaderboard"
            else:
                cursor = await conn.execute(
                    "SELECT user_id, points FROM activity_alltime WHERE guild_id = ? ORDER BY points DESC LIMIT 15",
                    (interaction.guild.id,)
                )
                title = "⭐ All-Time Activity Leaderboard"
            
            entries = await cursor.fetchall()
        
        if not entries:
            await interaction.response.send_message("No activity data recorded yet!")
            return
        
        # Build leaderboard
        description = ""
        for i, (user_id, points) in enumerate(entries, 1):
            member = interaction.guild.get_member(user_id)
            has_special = await self._check_has_special_role(interaction.guild, user_id)
            
            if member:
                name = member.display_name
                special_marker = " 🔒" if has_special else ""
                description += f"**{i}.** {name}{special_marker} — *{points} points*\n"
            else:
                description += f"**{i}.** Unknown User (ID: {user_id}) — *{points} points*\n"
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold() if view == "alltime" else discord.Color.green()
        )
        embed.set_footer(text="🔒 = User has special role (skipped for daily leader role)")
        
        await interaction.response.send_message(embed=embed)
    
    async def _get_session_rank(self, user_id: int, guild_id: int) -> int:
        """Get a user's session rank."""
        session_id = get_today_session_id()
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) + 1 FROM activity_session WHERE guild_id = ? AND session_id = ? AND points > (SELECT points FROM activity_session WHERE user_id = ? AND guild_id = ? AND session_id = ?)",
                (guild_id, session_id, user_id, guild_id, session_id)
            )
            result = await cursor.fetchone()
            session_pts = await self._get_user_session_points(user_id, guild_id)
            return result[0] if result[0] > 0 else 1 if session_pts > 0 else "-"
    
    async def _get_alltime_rank(self, user_id: int, guild_id: int) -> int:
        """Get a user's all-time rank."""
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) + 1 FROM activity_alltime WHERE guild_id = ? AND points > (SELECT COALESCE(points, 0) FROM activity_alltime WHERE user_id = ? AND guild_id = ?)",
                (guild_id, user_id, guild_id)
            )
            result = await cursor.fetchone()
            alltime_pts = await self._get_user_alltime_points(user_id, guild_id)
            return result[0] if result[0] > 0 else 1 if alltime_pts > 0 else "-"
    
    def _format_wasted_time(self, points: int) -> str:
        """Convert activity points to human readable wasted time.
        Each point = 15 seconds of active chatting time.
        """
        total_seconds = points * 15
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0:
            parts.append(f"{seconds}s")
            
        return ' '.join(parts) if parts else "0s"


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityCog(bot))