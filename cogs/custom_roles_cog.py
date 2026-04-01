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

async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRolesCog(bot))