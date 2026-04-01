import discord
import settings
import asyncio
from discord.ext import commands
from settings import logger

# Discord Bot Permission
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(
    command_prefix=";",
    intents=intents,
)

@bot.event
async def on_ready():
    logger.info(f"User: {bot.user} (ID: {bot.user.id})")

async def main():
    try:
        await bot.start(settings.DISCORD_API_TOKEN)
    except KeyboardInterrupt:
        logger.info("Keyboard Interrupt received. Shutting down the bot.")
    finally:
        await bot.close()

