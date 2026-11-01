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
    from control_panel.telemetry import enabled, heartbeat
    if enabled():
        bot.panel_heartbeat = asyncio.create_task(heartbeat(bot))
    # Import cog modules that have persistent views so their module-level
    # register() calls execute BEFORE register_all_views is called.
    from cogs.view_registry import register_all_views
    import cogs.guild_verification_cog  # noqa: F401 — registers VerificationStartView, VerificationAdminView
    import cogs.leaderboard_cog         # noqa: F401 — registers LeaderboardView
    import cogs.wwm_cog                 # noqa: F401 — registers GuildStatusBoard
    import cogs.giveaway_cog            # noqa: F401 — registers PersistentGiveawayView, EndedGiveawayView
    import cogs.market_cog              # noqa: F401 — registers MarketReportView
    register_all_views(bot)
    logger.info("Registered persistent views in setup_hook")

@bot.event
async def on_ready():
    logger.info(f"User: {bot.user} (ID: {bot.user.id})")
    bot.owner_id = settings.BOT_OWNER_ID

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

    from utility.activity_commands import sync_with_activity
    await sync_with_activity(bot) # Preserve the Portal-managed Activity Launch command.

async def main():
    import signal
    import os
    loop = asyncio.get_running_loop()
    from control_panel.shutdown import install_shutdown
    install_shutdown(loop, lambda: asyncio.create_task(bot.close()))
    def shutdown_signal(*_):
        loop.call_soon_threadsafe(lambda: asyncio.create_task(bot.close()))
    if os.name == 'nt' and hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, shutdown_signal)
    elif os.name != 'nt':
        loop.add_signal_handler(signal.SIGTERM, shutdown_signal)
    try:
        await bot.start(settings.DISCORD_API_TOKEN)
    except KeyboardInterrupt:
        logger.info("Keyboard Interrupt received. Shutting down the bot.")
    finally:
        task = getattr(bot, 'panel_heartbeat', None)
        if task:
            task.cancel()
        logger.info("Closing bot...")
        await bot.close()
        from utility.wwm import close_session
        await close_session()
        logger.info("Bot closed successfully.")

@bot.listen('on_disconnect')
async def panel_disconnect():
    from control_panel.telemetry import emit
    emit('log', {'level': 'WARNING', 'module': 'discord', 'message': 'Discord gateway disconnected'})

@bot.listen('on_command_error')
async def panel_command_error(context, error):
    from control_panel.telemetry import emit
    emit('log', {'level': 'ERROR', 'module': 'discord', 'message': 'Command failed', 'exception': type(error).__name__})

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
    from control_panel.locks import FileLock
    bot_lock = FileLock(settings.BASE_DIR / 'data' / 'bot-process.lock')
    try:
        run()
    finally:
        bot_lock.close()
