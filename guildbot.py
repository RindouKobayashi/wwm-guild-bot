import discord
import settings
import asyncio
from discord.ext import commands
from settings import logger
from context_menus import translate_context_menu

# Discord Bot Permission
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(
    command_prefix=";",
    intents=intents,
)

@bot.event
async def setup_hook():
    # ✅ CORRECT PLACE TO REGISTER PERSISTENT VIEWS - BEFORE BOT STARTS
    # This is the official documented way from discord.py examples
    from cogs.guild_verification_cog import VerificationStartView, VerificationAdminView
    bot.add_view(VerificationStartView())
    bot.add_view(VerificationAdminView())
    logger.info("✅ Registered persistent views in setup_hook")

@bot.event
async def on_ready():
    logger.info(f"User: {bot.user} (ID: {bot.user.id})")

    # Loading cogs
    for cog_file in settings.COGS_DIR.glob("*cog.py"):
        if cog_file.name != "__init__.py":
            ext_name = f"cogs.{cog_file.name[:-3]}"
            if ext_name not in bot.extensions: # Check if extension is already loaded
                try:
                    await bot.load_extension(ext_name)
                    logger.info(f"Loaded cog: {cog_file.name}")
                except Exception as e:
                    logger.error(f"Failed to load cog {cog_file.name}: {e}")
            else:
                logger.debug(f"Cog {cog_file.name} already loaded.") # Optional: Log if already loaded

    # Loading context menus
    translate_context_menu.setup_contextmenu(bot)

    await bot.tree.sync() # Sync commands after loading cogs

async def main():
    try:
        await bot.start(settings.DISCORD_API_TOKEN)
    except KeyboardInterrupt:
        logger.info("Keyboard Interrupt received. Shutting down the bot.")
    finally:
        logger.info("Closing bot...")
        await bot.close()
        logger.info("Bot closed successfully.")

def run():
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Keyboard Interrupt received. Shutting down the bot.")
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
    finally:
        if not loop.is_closed():
            loop.close()
        logger.info("The bot has been shut down successfully.")

if __name__ == "__main__":
    run()