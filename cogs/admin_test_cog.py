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
from settings import logger, BASE_DIR, BOT_OWNER_ID, WWM_REDIS_PLAYER_URL, WWM_UID # Added WWM_REDIS_PLAYER_URL, WWM_UID
from utility.wwm import _wwm_api_post, close_session, get_player_info, find_people_by_nickname, ALL_KNOWN_FIELDS
import json

def _prepare_for_json_sort(obj):
    """Recursively convert dict keys to strings to enable sorting with sort_keys=True"""
    if isinstance(obj, dict):
        return {str(k): _prepare_for_json_sort(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_prepare_for_json_sort(item) for item in obj]
    return obj

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._known_fields = ALL_KNOWN_FIELDS  # For use in autocomplete

    

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
            await interaction.followup.send(file=discord.File(io.BytesIO(json.dumps(_prepare_for_json_sort(result), indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()), "game_history.json"))
        else:
            await interaction.followup.send(f"Could not find game history for player {id}")
        await interaction.edit_original_response(content=f"Game history for player {id} has been sent as a file.", view=None)
        
            
        
    # -------------------------------------------------------------------------
    # New: /get_player_data - Fetch player data with field selection
    # -------------------------------------------------------------------------

    async def _resolve_player(self, identifier: str):
        """
        Resolve a player identifier (number ID or nickname) to PID and hostnum.
        Smart routing: if exactly 10 digits → number ID API, else → nickname API.
        returns (player_result, pid, hostnum) or (None, None, None) if not found.
        """
        # Smart routing: if identifier is exactly 10 digits, treat as number ID
        if identifier.isdigit() and len(identifier) == 10:
            # Exactly 10 digits, treat as number ID
            player_result = await get_player_info(identifier, fields=["base"], force_search=True)
            if player_result and player_result.get('result') and player_result['result'].get('id'):
                pid = player_result['result']['id']
                hostnum = player_result['result'].get('hostnum', 10595)  # Default hostnum if not present
                logger.debug(f"Resolved identifier '{identifier}' to PID {pid} via number ID")
                return player_result['result'], pid, hostnum
                

        # Otherwise, treat as nickname and use the find_people_by_nickname API
        player_result = await find_people_by_nickname(identifier, force_search=True)
        if player_result and player_result.get('result'):
            result = player_result['result']
            pid = result.get('id')
            hostnum = result.get('hostnum', 10595)  # Default hostnum if not present
            logger.debug(f"Resolved identifier '{identifier}' to PID {pid} via nickname search")
            return result, pid, hostnum

        logger.warning(f"Could not resolve identifier '{identifier}'")
        return None, None, None

    async def _parse_field_list(self, fields_str: str) -> list:
        """
        Parse the comma-separated field string into a cleaned list.
        Supports: "base", "base, club, attr", "all", "", etc.
        Also allows completely custom field names for API testing.
        """
        if not fields_str or fields_str.strip() == "":
            return ["base"]

        cleaned = fields_str.strip()
        
        # Split by comma, strip whitespace, remove empty strings
        field_list = [f.strip() for f in cleaned.split(",") if f.strip()]
        
        if not field_list:
            return ["base"]
        
        # Remove duplicates while preserving order
        seen = set()
        unique_fields = []
        for f in field_list:
            if f.lower() not in seen:
                seen.add(f.lower())
                unique_fields.append(f)
        
        return unique_fields

    # --- Autocomplete for mode ---
    async def mode_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = [
            app_commands.Choice(name="all_fields", value="all_fields"),
            app_commands.Choice(name="custom", value="custom"),
        ]
        if not current:
            return choices
        return [c for c in choices if current.lower() in c.value.lower()]

    # --- Autocomplete for fields (custom mode) ---
    async def fields_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """
        Autocomplete for the fields parameter.
        - Shows known fields as suggestions
        - If user has already typed some fields (comma-separated), preserves them
        - Allows custom field names not in the list
        - Limited to 25 suggestions (Discord limit)
        """
        # Parse current input to find what segment they're typing now
        parts = current.rsplit(",", 1)
        prefix = parts[0] + "," if len(parts) > 1 else ""
        current_segment = parts[-1].strip().lower() if parts else ""

        suggestions = []
        
        if current_segment:
            # Filter known fields by current segment
            for field in self._known_fields:
                if current_segment in field.lower():
                    full_value = f"{prefix}{field}"
                    suggestions.append(app_commands.Choice(
                        name=f"{prefix}{field}",
                        value=full_value
                    ))
                    if len(suggestions) >= 25:
                        break
        else:
            # Show all known fields when nothing typed yet
            # Also include a "full data" meta-option
            suggestions.append(app_commands.Choice(
                name="← Include ALL known fields (equivalent to all_fields mode)",
                value="all"
            ))
            for field in self._known_fields[:24]:  # 24 + the "all" option = 25
                suggestions.append(app_commands.Choice(
                    name=field,
                    value=field
                ))

        return suggestions[:25]

    @app_commands.describe(
        identifier="Player name or number ID to look up",
        mode="'all_fields' for everything, 'custom' to pick specific fields",
        fields="Comma-separated field list (only used when mode='custom'). Try new fields here!"
    )
    @app_commands.autocomplete(mode=mode_autocomplete)
    @app_commands.autocomplete(fields=fields_autocomplete)
    @app_commands.command(name="get_player_data", description="Fetch player data with selectable fields")
    async def get_player_data(
        self,
        interaction: discord.Interaction,
        identifier: str,
        mode: str,
        fields: str = None
    ):
        """Fetch player data by name or ID, with field selection"""
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        # Step 1: Resolve player identifier to PID
        player_result, pid, hostnum = await self._resolve_player(identifier)
        if not player_result or not pid:
            await interaction.followup.send(f"❌ Could not find a player matching '{identifier}'. Try a number ID or in-game name.")
            return

        player_name = player_result.get("name", player_result.get("nickname", identifier))
        logger.info(f"get_player_data: Resolved '{identifier}' → {player_name} (PID: {pid})")

        # Step 2: Determine which fields to fetch
        hostnum = player_result.get("hostnum", 10595)
        
        if mode == "all_fields":
            field_list = ALL_KNOWN_FIELDS
            mode_label = "all fields"
        else:
            # custom mode
            if fields:
                field_list = await self._parse_field_list(fields)
            else:
                field_list = ["base"]
            mode_label = f"custom: {', '.join(field_list)}"

        # Check if "all" was passed inside the custom field string
        if "all" in [f.lower() for f in field_list]:
            field_list = ALL_KNOWN_FIELDS
            mode_label = "all fields"

        logger.debug(f"get_player_data: Fetching fields [{mode_label}] for PID {pid}")

        # Step 3: Fetch the data from Redis endpoint
        payload = {
            "fields": field_list,
            "hostnum2pids": {
                hostnum: [pid]
            },
            "uid": WWM_UID,
            "token": "1"  # Required by some endpoints
        }

        raw_response = await _wwm_api_post(WWM_REDIS_PLAYER_URL, payload, timeout=30)

        # Step 4: Process and send the result
        if not raw_response or not isinstance(raw_response, dict):
            await interaction.followup.send(f"❌ API returned no data or an unexpected format for player '{player_name}'.")
            return

        # Extract the actual player data from the response
        player_data = {}
        if 'result' in raw_response and raw_response['result']:
            # Response is keyed by PID
            result_dict = raw_response['result']
            if isinstance(result_dict, dict):
                # Try to get data by PID, or take the first entry
                if pid in result_dict:
                    player_data = result_dict[pid]
                elif result_dict:
                    first_key = next(iter(result_dict))
                    player_data = result_dict[first_key]
        
        if not player_data:
            # If we couldn't extract the data, send the raw response for debugging
            player_data = raw_response

        # Build the output - wrap in a nice structure
        output = {
            "query": {
                "identifier": identifier,
                "resolved_name": player_name,
                "resolved_pid": pid,
                "hostnum": hostnum,
                "mode": mode_label,
                "fields_requested": field_list,
            },
            "data": player_data
        }

        # Send as a downloadable JSON file
        json_bytes = json.dumps(_prepare_for_json_sort(output), indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()
        filename = f"player_data_{pid}.json"
        file = discord.File(io.BytesIO(json_bytes), filename=filename)

        # Prepare a summary message with some basic info about what was fetched
        summary_parts = [
            f"✅ **{player_name}** (`{pid}`)",
            f"📦 **Mode:** {mode_label}",
            f"🔢 **Fields count:** {len(field_list)}",
        ]
        
        # If base info is available, show a quick summary
        if isinstance(player_data, dict):
            base = player_data.get("base", {})
            if base:
                nick = base.get("nickname", base.get("name", "N/A"))
                level = base.get("level", "N/A")
                online = "🟢 Online" if base.get("is_online") else "🔴 Offline"
                summary_parts.append(f"👤 **{nick}** | Lv.{level} | {online}")

        summary = " | ".join(summary_parts)

        await interaction.followup.send(
            content=summary,
            file=file
        )

    @app_commands.describe(identifier="Player identifier to look up")
    @app_commands.command(name="get_player_combat_plan", description="Fetch a player's combat plan")
    async def get_player_combat_plan(self, interaction: discord.Interaction, identifier: str):
        """Fetch a player's combat plan by name or ID"""
        if not await is_admin_or_staff(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        # Step 1: Resolve player identifier to PID
        player_result, pid, hostnum = await self._resolve_player(identifier)
        if not player_result or not pid:
            await interaction.followup.send(f"❌ Could not find a player matching '{identifier}'. Try a number ID or in-game name.")
            return

        player_name = player_result.get("name", player_result.get("nickname", identifier))
        logger.info(f"get_player_combat_plan: Resolved '{identifier}' → {player_name} (PID: {pid})")

        # Step 2: Fetch the combat plan from the API
        payload = {
            "uid": WWM_UID,
            "pid": pid,
            "hostnum": hostnum
        }

        combat_plan_response = await _wwm_api_post(settings.WWM_GET_PLAYER_COMBAT_PLAN_URL, payload, timeout=30)

        if not combat_plan_response or not isinstance(combat_plan_response, dict):
            await interaction.followup.send(f"❌ API returned no data or an unexpected format for combat plan of player '{player_name}'.")
            return

        # Send as a downloadable JSON file
        json_bytes = json.dumps(_prepare_for_json_sort(combat_plan_response), indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()
        filename = f"combat_plan_{pid}.json"
        file = discord.File(io.BytesIO(json_bytes), filename=filename)

        summary = f"✅ **{player_name}** (`{pid}`) | 📦 Combat Plan fetched successfully."

        await interaction.followup.send(
            content=summary,
            file=file
        )
        


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))