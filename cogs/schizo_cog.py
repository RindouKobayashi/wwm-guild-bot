import discord
import settings
from discord import app_commands
from discord.ext import commands
from settings import logger
import random

class SchizoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.chance_to_react = 0.1  # 10% chance to react to a message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id in [414234388776353828]:
            if message.author == self.bot.user:
                return
            roll = random.random()
            logger.debug(f"Rolled a {roll:.4f} for message ID {message.id} in channel ID {message.channel.id}")
            if roll < self.chance_to_react:
                emoji = random.choice(["😀", "😂", "😎", "🤔", "🙃", "😜"])
                
                await message.add_reaction(emoji)
                logger.debug(f"Added reaction {emoji} to message ID {message.id} in channel ID {message.channel.id}")
                await message.remove_reaction(emoji, self.bot.user)
                logger.debug(f"Removed reaction {emoji} from message ID {message.id} in channel ID {message.channel.id}")

async def setup(bot: commands.Bot):
    await bot.add_cog(SchizoCog(bot))