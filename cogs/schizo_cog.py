import discord
import settings
from discord import app_commands
from discord.ext import commands, tasks
from settings import logger
import random
import asyncio

class SchizoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.target_channels = [414234388776353828, 1442853064053756028]

        # Probabilities
        self.chance_to_react = 0.1  # 10% chance to react to a message
        self.chance_to_type = 0.05  # 5% chance to type a message
        self.chance_to_whisper = 0.01  # 1% chance to whisper a message

        self.cryptic_messages = [
            "Wake up.",
            "Look at me.",
            "I'm watching you.",
            "Look behind you.",
            "I wouldn't say that if I were you.",
            "?",
        ]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id not in self.target_channels:
            return
        if message.author == self.bot.user:
            return
        if message.author.id == self.bot.owner_id: # blacklist owner
            logger.debug(f"skipped owner {self.bot.owner_id}")
            return
        
        roll = {
            "react": random.random(),
            "type": random.random(),
            "whisper": random.random(),
        }
        
        # 1. React
        if roll["react"] < self.chance_to_react:
            emoji = random.choice(["😀", "😂", "😎", "🤔", "🙃", "😜", "👀", "👁️", "👤", "❓"])
            try:
                await message.add_reaction(emoji)
                await message.remove_reaction(emoji, self.bot.user)
                logger.debug(f"Phantom reacted to {message.content} with {emoji}")
            except discord.HTTPException:
                pass # Ignore if message is deleted or not found

        # 2. Phantom Typing
        if roll["type"] < self.chance_to_type:
            logger.debug(f"Phantom typing triggered on {message.content}")
            async with message.channel.typing():
                # Wait for 3 to 10 seconds
                await asyncio.sleep(random.randint(3, 10))

        # 3. Ghost Whisper
        if roll["whisper"] < self.chance_to_whisper:
            logger.debug(f"Phantom whisper triggered on {message.content}")
            phrase = random.choice(self.cryptic_messages)
            try:
                await asyncio.sleep(random.randint(3, 10))
                ghost_msg = await message.reply(phrase, mention_author=False)
                await asyncio.sleep(1)
                await ghost_msg.delete()
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.channel.id not in self.target_channels:
            return
        if after.author.bot or after.author.id == self.bot.owner_id:
            return


        roll = random.random()
        logger.info(f"For edited message, chance roll: {roll}")
        
        if roll < 0.1:
            try:
                msg = await after.reply("I saw what you originally sent.", mention_author=False)
                await asyncio.sleep(1)
                await msg.delete()
                logger.debug(f"Called out {after.author.name} for editing a message")
            except discord.HTTPException:
                pass

    



async def setup(bot: commands.Bot):
    await bot.add_cog(SchizoCog(bot))