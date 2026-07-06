import discord
from cogs.basic_cog import BasicCog
from cogs.market_cog import is_admin_or_staff
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
from utility.wwm import _wwm_api_post, close_session, get_player_info
import json

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    

    @app_commands.describe(type="currently only 1 or 2", id="player id to look up", sub_type="sub type for filtering")
    @app_commands.command(name="list_game_history", description="List game history for a specific player")
    async def list_game_history(self, interaction: discord.Interaction, type: int, id: str, sub_type: int = None):
        """List game history for a specific player"""
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        await interaction.response.defer()
        # look up player using id, then call the API to get game history
        player_info = await get_player_info(id, fields=["base"])
        if not player_info:
            await interaction.followup.send(f"Could not find player with id {id}")
            return
        
        logger.debug(f"Found player info: {json.dumps(player_info, indent=4)}")
        player_info = player_info["result"]
        avatar = player_info["id"]
        logger.debug(avatar)
        last_char = avatar[-1]
        entity_last_char = chr(ord(last_char) + 2)
        entity_id = avatar[:-1] + entity_last_char
        payload = {
            "type": type, 
            "entity_id": entity_id,
            "avatar": avatar,
            "start": 0,
            "uid": "1",
        }
        if sub_type:
            payload["sub_type"] = sub_type

        result = await _wwm_api_post(settings.WWM_LIST_GAME_HISTORY_URL, payload)
        if result:
            # send the result as a json file
            await interaction.followup.send(file=discord.File(io.BytesIO(json.dumps(result, indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()), "game_history.json"))
        else:
            await interaction.followup.send(f"Could not find game history for player {id}")
        await interaction.edit_original_response(content=f"Game history for player {id} has been sent as a file.", view=None)
        
            
        

        

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))