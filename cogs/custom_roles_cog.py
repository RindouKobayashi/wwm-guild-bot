import re
import os
import sqlite3
import random
import discord
import settings
from discord.ext import commands
from discord import app_commands
from settings import logger

# Database path for color presets (separate from activity.db)
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "presets.db")


def get_presets_db_path():
    """Get the database path for presets."""
    return DB_PATH


def init_presets_table():
    """Initialize the color presets table if it doesn't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(get_presets_db_path()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS color_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                preset_name TEXT NOT NULL,
                hex_code TEXT NOT NULL,
                UNIQUE(user_id, guild_id, preset_name)
            )
        ''')
        conn.commit()


def get_user_presets(user_id: int, guild_id: int) -> list:
    """Get all presets for a user in a guild."""
    with sqlite3.connect(get_presets_db_path()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT preset_name, hex_code FROM color_presets WHERE user_id = ? AND guild_id = ? ORDER BY preset_name ASC",
            (user_id, guild_id)
        )
        return cursor.fetchall()


def save_preset(user_id: int, guild_id: int, preset_name: str, hex_code: str) -> bool:
    """Save a color preset for a user. Returns True if successful."""
    # Validate hex code
    if not re.match(r'^#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$', hex_code):
        return False
    
    # Ensure color has # prefix
    if not hex_code.startswith('#'):
        hex_code = '#' + hex_code
    
    with sqlite3.connect(get_presets_db_path()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO color_presets (user_id, guild_id, preset_name, hex_code) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, preset_name.lower(), hex_code.upper())
        )
        conn.commit()
    return True


def delete_preset(user_id: int, guild_id: int, preset_name: str) -> bool:
    """Delete a color preset. Returns True if deleted."""
    with sqlite3.connect(get_presets_db_path()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM color_presets WHERE user_id = ? AND guild_id = ? AND preset_name = ?",
            (user_id, guild_id, preset_name.lower())
        )
        conn.commit()
        return cursor.rowcount > 0


async def preset_autocomplete(interaction: discord.Interaction, current: str) -> list:
    """Autocomplete for preset names."""
    presets = get_user_presets(interaction.user.id, interaction.guild.id)
    return [
        app_commands.Choice(name=f"{name} - {hex_code}", value=name)
        for name, hex_code in presets
        if current.lower() in name.lower()
    ][:25]


class CustomRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_presets_table()

    @app_commands.command(name="change_color", description="Change the color of your role")
    @app_commands.describe(
        mode="Whether to set a random color, hex code, or preset",
        hex_code="Hex color code (e.g., #FF5733). Required for hex mode",
        preset="Your saved preset name. Use /preset save to create one first."
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Random", value="random"),
        app_commands.Choice(name="Hex Code", value="hex"),
        app_commands.Choice(name="Preset", value="preset"),
    ])
    @app_commands.autocomplete(preset=preset_autocomplete)
    async def change_color_self(self, interaction: discord.Interaction, mode: str, hex_code: str = None, preset: str = None):
        logger.info(f"User {interaction.user} requested to change role color with {mode}: {hex_code} (preset: {preset})")

        # Check if user has a special role that allows them to change their role color
        special_role_ids = list(settings.SPECIAL_ROLES.values())
        user_special_role = next((role for role in interaction.user.roles if role.id in special_role_ids), None)

        # Also allow activity leader role to change color
        if not user_special_role:
            activity_leader_role = interaction.guild.get_role(settings.ACTIVITY_LEADER_ROLE_ID)
            if activity_leader_role in interaction.user.roles:
                # Activity leader can change their activity leader role color
                user_special_role = activity_leader_role

        if not user_special_role:
            await interaction.response.send_message("You don't have permission to change your role color.", delete_after=10)
            return

        # Process the color based on mode
        if mode == "random":
            color_int = random.randint(0, 0xFFFFFF)
            color_value = discord.Color(value=color_int)
            color_hex = f"#{color_int:06X}"
        elif mode == "preset":
            # Get preset value
            if not preset:
                await interaction.response.send_message("Please provide a preset name. Use `/preset list` to see your saved presets.", ephemeral=True)
                return
            
            # Look up the preset
            presets = get_user_presets(interaction.user.id, interaction.guild.id)
            preset_dict = {name.lower(): hex_code for name, hex_code in presets}
            
            if preset.lower() not in preset_dict:
                await interaction.response.send_message(f"Preset `{preset}` not found. Use `/preset list` to see your saved presets or `/preset save` to create one.", ephemeral=True)
                return
            
            hex_code = preset_dict[preset.lower()]
            color_value = discord.Color.from_str(hex_code)
            color_hex = hex_code
        else:  # hex
            if not hex_code:
                await interaction.response.send_message("Please provide a hex color code (e.g., #FF5733).")
                return

            # Validate hex color format
            if not re.match(r'^#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$', hex_code):
                await interaction.response.send_message("Invalid color format. Please provide a valid hex color (e.g., #FF5733 or FF5733).", delete_after=10)
                return

            # Ensure color has # prefix
            if not hex_code.startswith('#'):
                hex_code = '#' + hex_code
            color_value = discord.Color.from_str(hex_code)
            color_hex = hex_code

        await interaction.response.defer()
        await user_special_role.edit(color=color_value)
        await interaction.followup.send(f"`{user_special_role.name}` color changed to `{color_hex}`.")

    @app_commands.command(name="preset", description="Manage your color presets")
    @app_commands.describe(
        action="What to do with the preset",
        name="Name for your preset (e.g., 'black')",
        hex_code="Hex color code for the preset (e.g., #010B13)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Save", value="save"),
        app_commands.Choice(name="Delete", value="delete"),
        app_commands.Choice(name="List", value="list"),
    ])
    async def preset_self(self, interaction: discord.Interaction, action: str, name: str = None, hex_code: str = None):
        logger.info(f"User {interaction.user} requested preset action: {action} (name: {name}, hex: {hex_code})")
        
        if action == "save":
            if not name or not hex_code:
                await interaction.response.send_message("Please provide both a name and hex code for the preset. Example: `/preset save name:black hex_code:#010B13`", ephemeral=True)
                return
            
            if len(name) > 50:
                await interaction.response.send_message("Preset name is too long. Maximum 50 characters allowed.", ephemeral=True)
                return
            
            if save_preset(interaction.user.id, interaction.guild.id, name, hex_code):
                # Normalize hex code for display
                if not hex_code.startswith('#'):
                    hex_code = '#' + hex_code
                hex_code = hex_code.upper()
                await interaction.response.send_message(f"Preset `{name}` saved with color `{hex_code}`!")
            else:
                await interaction.response.send_message("Invalid hex color format. Please provide a valid hex color (e.g., #FF5733 or FF5733).", ephemeral=True)
        
        elif action == "delete":
            if not name:
                await interaction.response.send_message("Please provide the name of the preset to delete.", ephemeral=True)
                return
            
            if delete_preset(interaction.user.id, interaction.guild.id, name):
                await interaction.response.send_message(f"Preset `{name}` deleted!")
            else:
                await interaction.response.send_message(f"Preset `{name}` not found.", ephemeral=True)
        
        elif action == "list":
            presets = get_user_presets(interaction.user.id, interaction.guild.id)
            if not presets:
                await interaction.response.send_message("You don't have any saved presets. Use `/preset save` to create one!", ephemeral=True)
                return
            
            preset_list = "\n".join([f"**{name}**: {hex_code}" for name, hex_code in presets])
            await interaction.response.send_message(f"Your color presets:\n{preset_list}", ephemeral=True)

    @app_commands.command(name="change_role_name", description="Change the name of your role")
    @app_commands.describe(new_name="The new name for your role (max 100 characters)")
    async def change_role_name(self, interaction: discord.Interaction, new_name: str):
        logger.info(f"User {interaction.user} requested to change role name to '{new_name}'")

        # Validate role name length (Discord limit is 100 characters)
        if len(new_name) > 100:
            await interaction.response.send_message("Role name is too long. Maximum 100 characters allowed.", ephemeral=True)
            return

        if not new_name.strip():
            await interaction.response.send_message("Role name cannot be empty or only whitespace.", ephemeral=True)
            return

        # Check if user has a special role that allows them to change their role name
        special_role_ids = list(settings.SPECIAL_ROLES.values())
        user_special_role = next((role for role in interaction.user.roles if role.id in special_role_ids), None)

        if not user_special_role:
            await interaction.response.send_message("You don't have permission to change your role name.", delete_after=10)
            return

        old_name = user_special_role.name
        await user_special_role.edit(name=new_name)
        await interaction.response.send_message(f"Role name changed from `{old_name}` to `{new_name}`.")

    @app_commands.command(name="help", description="Get help information about custom role commands")
    async def help(self, interaction: discord.Interaction):
        help_text = (
            "Here are the available custom role commands:\n\n"
            "**/change_color**: Change the color of your special role.\n"
            "  - Random: Set a random color\n"
            "  - Hex Code: Provide your own hex color (e.g., #FF5733)\n"
            "  - Preset: Use a saved preset (auto-complete available)\n\n"
            "**/preset save**: Save a color preset for quick access.\n"
            "  Example: `/preset save name:black hex_code:#010B13`\n\n"
            "**/preset delete**: Delete a saved preset.\n\n"
            "**/preset list**: List all your saved presets.\n\n"
            "**/change_role_name**: Change the name of your special role.\n\n"
            "**/help**: Show this help message."
        )
        await interaction.response.send_message(help_text, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRolesCog(bot))