import discord
import settings
import asyncio
import os # Added
import io # Added
import aiosqlite
import re
import datetime
from discord.ext import commands, tasks
from discord import app_commands, File # Added File
from settings import logger, BASE_DIR, BOT_OWNER_ID # Added BASE_DIR, BOT_OWNER_ID

class BasicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = BASE_DIR / "data" / "reminders.db"

    async def cog_load(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, trigger_at INTEGER NOT NULL, message TEXT NOT NULL, status TEXT DEFAULT 'pending')")
            await db.commit()
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    def _parse_time_spec(self, spec: str) -> int:
        pattern = r'(\d+)\s*(y|mo|w|d|h|m|s)'
        units = {'y': 365 * 24 * 3600, 'mo': 30 * 24 * 3600, 'w': 7 * 24 * 3600, 'd': 24 * 3600, 'h': 3600, 'm': 60, 's': 1}
        total = 0
        for num, unit in re.findall(pattern, spec, re.IGNORECASE):
            total += int(num) * units[unit.lower()]
        return total

    @app_commands.command(name="my_reminders", description="List your pending reminders")
    async def my_reminders(self, interaction: discord.Interaction):
        """List the caller's pending reminders."""
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminders WHERE user_id = ? AND status = 'pending' AND trigger_at > ? ORDER BY trigger_at ASC", (interaction.user.id, now))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
            return
        lines = []
        for row in rows:
            lines.append(f"• <t:{row['trigger_at']}:R>: {row['message']}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="remindme", description="Set a personal reminder. Examples: /remindme 3h Take a break, /remindme 90m Meeting")
    @app_commands.describe(time="Time specification such as '3h', '90m', '1h 30m', '2 days'", message="What should I remind you about?")
    async def remindme(self, interaction: discord.Interaction, time: str, message: str):
        """Set a reminder for yourself."""
        logger.info(f"Command /remindme invoked by {interaction.user} with time='{time}' msg='{message}'")
        seconds = self._parse_time_spec(time)
        if seconds <= 0:
            await interaction.response.send_message("Could not parse time. Example formats: `3h`, `90m`, `1h 30m`, `2 days`.", ephemeral=True)
            return
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        trigger_at = now + seconds
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO reminders (user_id, channel_id, trigger_at, message) VALUES (?, ?, ?, ?)", (interaction.user.id, interaction.channel.id, trigger_at, message))
            await db.commit()
        await interaction.response.send_message(f"Reminder set for <t:{trigger_at}:R>: {message}", ephemeral=True)
        logger.info(f"Reminder queued for user {interaction.user.id} at {trigger_at}")

    @tasks.loop(seconds=10)
    async def reminder_loop(self):
        try:
            now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM reminders WHERE status = 'pending' AND trigger_at <= ?", (now,))
                rows = await cursor.fetchall()
            for row in rows:
                user = self.bot.get_user(row['user_id'])
                if not user:
                    continue
                try:
                    await user.send(f"Reminder: {row['message']}")
                except Exception:
                    channel = self.bot.get_channel(row['channel_id'])
                    if channel:
                        try:
                            await channel.send(f"{user.mention} reminder: {row['message']}")
                        except Exception:
                            pass
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("UPDATE reminders SET status='done' WHERE id=?", (row['id'],))
                    await db.commit()
        except Exception as e:
            logger.error(f"reminder_loop error: {e}")

    @app_commands.command(name="sync", description="Sync the commands with the server")
    async def sync(self, interaction: discord.Interaction):
        """Sync the commands with the server"""
        logger.info(f"Command /sync has been invoked by {interaction.user}")
        # Defer the response to allow for longer processing time
        await interaction.response.defer()
        # Check if the user is the owner of the bot
        if interaction.user.id != settings.BOT_OWNER_ID:
            await interaction.followup.send("You are not allowed to run this command.", ephemeral=True)
            return
        await interaction.followup.send("Syncing commands with the server...", ephemeral=True)
        await self.bot.tree.sync()
        await interaction.edit_original_response(content="Commands have been synced with the server.")

    @app_commands.command(name="logs", description="View the last N lines of the log file.")
    @app_commands.describe(lines="Number of lines to show (default: 20)")
    async def logs(self, interaction: discord.Interaction, lines: int = 20):
        """Shows the last N lines of the log file."""
        logger.info(f"Command /logs invoked by {interaction.user} for {lines} lines.")
        logger.debug(f"Checking authorization for user {interaction.user.id}")
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
            return

        log_file_path = BASE_DIR / "logs" / "infos.log"
        
        if not os.path.exists(log_file_path):
            await interaction.response.send_message("Log file not found.", ephemeral=True)
            return

        try:
            with open(log_file_path, 'r', encoding='utf-8') as f:
                # Read all lines and take the last N
                all_lines = f.readlines()
                last_lines = all_lines[-lines:]
            
            if not last_lines:
                await interaction.response.send_message("Log file is empty or fewer lines than requested exist.", ephemeral=True)
                return

            log_content = "".join(last_lines)
            
            if len(log_content) <= 1980: # Leave some room for code block markers
                 await interaction.response.send_message(f"```log\n{log_content}\n```", ephemeral=True)
            else:
                # Send as a file if too long
                with io.BytesIO(log_content.encode('utf-8')) as log_file_obj:
                    await interaction.response.send_message("Log content is too long, sending as a file.", file=File(log_file_obj, "logs.log"), ephemeral=True)

        except Exception as e:
            logger.error(f"Error reading log file: {e}")
            await interaction.response.send_message(f"An error occurred while reading the log file: {e}", ephemeral=True)

    @app_commands.command(name="delete_message_after", description="Delete messages after a certain message ID.")
    @app_commands.describe(message_id="The ID of the message after which to delete")
    async def delete_message_after(self, interaction: discord.Interaction, message_id: str):
        """Delete messages after a certain message ID."""
        logger.info(f"Command /delete_message_after invoked by {interaction.user} for message ID {message_id}.")
        logger.debug(f"Checking authorization for user {interaction.user.id}")
        await interaction.response.defer(ephemeral=True) # Defer the response to allow for longer processing time
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
        
        try:
            message = await interaction.channel.fetch_message(int(message_id))
            if message:
                # Make sure to not delete the interaction response message itself if it happens to be in the same channel
                deleted = await interaction.channel.purge(after=message, reason=f"Requested by {interaction.user}")
                await interaction.followup.send(f"Deleted {len(deleted)} messages after message ID {message_id}.", ephemeral=True)
            else:
                await interaction.followup.send(f"Message with ID {message_id} not found.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error deleting messages: {e}")
            await interaction.followup.send(f"An error occurred while deleting messages: {e}", ephemeral=True)

    @app_commands.command(name="status", description="Check the status of the bot.")
    async def status(self, interaction: discord.Interaction):
        """Check the status of the bot."""
        logger.info(f"Command /status invoked by {interaction.user}")
        logger.debug(f"Checking authorization for user {interaction.user.id}")
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
            return

        # Get number of discord guilds the bot is in
        guild_count = len(self.bot.guilds)
        await interaction.response.send_message(f"The bot is currently in {guild_count} guilds.", ephemeral=True)

        # Leave all servers except for whitelisted ones
        for guild in self.bot.guilds:
            if guild.id not in settings.WHITELISTED_DISCORD_SERVERS:
                await guild.leave()
                logger.info(f"Left guild {guild.id}")
                await interaction.channel.send(f"Left guild {guild.name} ({guild.id})")
            else:
                logger.info(f"Guild {guild.id} is whitelisted.")
                await interaction.channel.send(f"Guild {guild.name} ({guild.id}) is whitelisted")

async def setup(bot: commands.Bot):
    await bot.add_cog(BasicCog(bot))