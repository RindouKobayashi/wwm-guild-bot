import discord
import settings
import asyncio
import os # Added
import io # Added
from discord.ext import commands
from discord import app_commands, File # Added File
from settings import logger, BASE_DIR, BOT_OWNER_ID # Added BASE_DIR, BOT_OWNER_ID

class BasicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

async def setup(bot: commands.Bot):
    await bot.add_cog(BasicCog(bot))
