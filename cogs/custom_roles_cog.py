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

    @app_commands.command(name="set_role_icon", description="Set an icon for your role (requires server boost level 2)")
    @app_commands.describe(
        source="Choose to upload an image or use an emoji",
        image="Upload an image for your role icon (PNG/JPEG, max 128KB)",
        emoji="Select a server emoji to use as your role icon"
    )
    @app_commands.choices(source=[
        app_commands.Choice(name="Image", value="image"),
        app_commands.Choice(name="Emoji", value="emoji"),
    ])
    async def set_role_icon(self, interaction: discord.Interaction, source: str, image: discord.Attachment = None, emoji: str = None):
        logger.info(f"User {interaction.user} requested to set role icon with source: {source}")

        # Check if the server has boost level 2
        if interaction.guild.premium_tier < 2:
            await interaction.response.send_message(
                "This server needs **Boost Level 2** to have custom role icons. Please boost the server first!",
                ephemeral=True
            )
            return

        # Check if user has a special role that allows them to change their role icon
        special_role_ids = list(settings.SPECIAL_ROLES.values())
        user_special_role = next((role for role in interaction.user.roles if role.id in special_role_ids), None)

        # Also allow activity leader role to change icon
        if not user_special_role:
            activity_leader_role = interaction.guild.get_role(settings.ACTIVITY_LEADER_ROLE_ID)
            if activity_leader_role in interaction.user.roles:
                user_special_role = activity_leader_role

        if not user_special_role:
            await interaction.response.send_message("You don't have permission to set your role icon.", delete_after=10)
            return

        # Check if the role is managed by a bot/integration (can't edit icons for managed roles)
        if user_special_role.managed:
            await interaction.response.send_message("Cannot change the icon for a managed role.", ephemeral=True)
            return

        await interaction.response.defer()

        if source == "image":
            if not image:
                await interaction.followup.send("Please upload an image for your role icon.", ephemeral=True)
                return

            # Validate image format
            valid_extensions = ["png", "jpeg", "jpg", "webp", "gif"]
            file_ext = image.filename.split(".")[-1].lower()
            if file_ext not in valid_extensions:
                await interaction.followup.send("Invalid image format. Please use PNG, JPEG, WebP, or GIF.", ephemeral=True)
                return

            # Check file size (Discord limit is 256KB for role icons)
            if image.size > 256 * 1024:
                await interaction.followup.send("Image is too large. Maximum size is 256KB.", ephemeral=True)
                return

            # Download and convert the image
            try:
                image_data = await image.read()
                
                # For GIF, extract only the first frame
                if file_ext == "gif":
                    try:
                        from PIL import Image
                        from io import BytesIO
                        
                        # Open the GIF and extract the first frame
                        with Image.open(BytesIO(image_data)) as gif:
                            # Convert to RGBA if necessary for compatibility
                            first_frame = gif.convert("RGBA")
                            
                            # Save the first frame as PNG
                            output = BytesIO()
                            first_frame.save(output, format="PNG")
                            image_data = output.getvalue()
                            file_ext = "png"
                    except ImportError:
                        # PIL not available, try using image size as hint that it's valid
                        logger.warning("PIL not available, attempting to use GIF as-is")
                    except Exception as e:
                        logger.error(f"Failed to extract first frame from GIF: {e}")
                        await interaction.followup.send("Failed to process GIF image. Please try a different format.", ephemeral=True)
                        return
                
                await interaction.followup.send("Processing image...", ephemeral=True)
                
                # Set the role icon using the image data
                # Discord will automatically resize and convert the image
                await user_special_role.edit(display_icon=image_data)
                await interaction.followup.send(f"Role icon set for `{user_special_role.name}`!", ephemeral=True)
                
            except discord.HTTPException as e:
                logger.error(f"Failed to set role icon: {e}")
                await interaction.followup.send("Failed to set role icon. The image may be invalid or too large.", ephemeral=True)
            except Exception as e:
                logger.error(f"Error setting role icon: {e}")
                await interaction.followup.send("An error occurred while setting the role icon.", ephemeral=True)

        elif source == "emoji":
            if not emoji:
                await interaction.followup.send("Please provide an emoji to use as your role icon.", ephemeral=True)
                return

            # Parse the emoji (can be a custom emoji or unicode emoji)
            # For custom emoji: <:name:id>
            # For unicode emoji: just the character
            
            try:
                # Try to find the emoji in the guild
                found_emoji = None
                
                # Check if it's a custom emoji
                if emoji.startswith('<') and ':' in emoji:
                    # Extract emoji ID from <:name:id> or <a:name:id> format
                    match = re.search(r'<a?:\w+:(\d+)>', emoji)
                    if match:
                        emoji_id = int(match.group(1))
                        found_emoji = discord.utils.get(interaction.guild.emojis, id=emoji_id)
                else:
                    # It's a unicode emoji - Discord doesn't support unicode emoji for role icons
                    await interaction.followup.send(
                        "Custom server emojis are supported, but unicode emojis are not. Please use a server emoji like `:emoji_name:`.",
                        ephemeral=True
                    )
                    return

                if not found_emoji:
                    await interaction.followup.send("Could not find that emoji in this server. Please use a custom emoji from this server.", ephemeral=True)
                    return

                # Get the emoji image
                emoji_image = await found_emoji.read()
                await user_special_role.edit(display_icon=emoji_image)
                await interaction.followup.send(f"Role icon set to {found_emoji} for `{user_special_role.name}`!", ephemeral=True)

            except discord.HTTPException as e:
                logger.error(f"Failed to set role icon from emoji: {e}")
                await interaction.followup.send("Failed to set role icon. The emoji may be invalid.", ephemeral=True)
            except Exception as e:
                logger.error(f"Error setting role icon from emoji: {e}")
                await interaction.followup.send("An error occurred while setting the role icon.", ephemeral=True)

    @app_commands.command(name="change_role_name", description="Change the name of your role")
    @app_commands.describe(new_name="The new name for your role (max 100 characters)")
    async def change_role_name(self, interaction: discord.Interaction, new_name: str):
        logger.info(f"User{interaction.user} requested to change role name to '{new_name}'")

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
            "**/set_role_icon**: Set an icon for your role.\n"
            "  - Image: Upload a PNG/JPEG image (max 128KB)\n"
            "  - Emoji: Use a server emoji\n"
            "  - Requires server Boost Level 2\n\n"
            "**/change_role_name**: Change the name of your special role.\n\n"
            "**/help**: Show this help message."
        )
        await interaction.response.send_message(help_text, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRolesCog(bot))