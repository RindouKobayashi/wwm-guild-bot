import re
import random
import discord
import settings
from discord.ext import commands
from discord import app_commands
from settings import logger

class CustomRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="change_color", description="Change the color of your role")
    @app_commands.describe(
        mode="Whether to set a random color or a specific hex code",
        hex_code="Hex color code (e.g., #FF5733). Optional - not required for random mode"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Random", value="random"),
        app_commands.Choice(name="Hex Code", value="hex"),
    ])
    async def change_color(self, interaction: discord.Interaction, mode: str, hex_code: str = None):
        logger.info(f"User {interaction.user} requested to change role color with {mode}: {hex_code}")

        # Check if user has a special role that allows them to change their role color
        special_role_ids = list(settings.SPECIAL_ROLES.values())
        user_special_role = next((role for role in interaction.user.roles if role.id in special_role_ids), None)

        if not user_special_role:
            await interaction.response.send_message("You don't have permission to change your role color.", delete_after=10)
            return

        # Process the color based on mode
        if mode == "random":
            color_int = random.randint(0, 0xFFFFFF)
            color_value = discord.Color(value=color_int)
            color_hex = f"#{color_int:06X}"
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

async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRolesCog(bot))