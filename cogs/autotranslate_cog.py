import discord
import settings
import asyncio
import os # Added
import io # Added
from discord.ext import commands
from discord import app_commands, File
from settings import logger, BASE_DIR, BOT_OWNER_ID
from googletrans import Translator

class AutoTranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.translator = Translator()


    @commands.Cog.listener() 
    async def on_message(self, message: discord.Message):
        # Ignore messages from bots
        if message.author.bot:
            return

        # Check if the message is in a channel that requires auto-translation
        if message.channel.id in settings.AUTO_TRANSLATE_CHANNELS.values():
            logger.info(f"Auto-translate triggered for message ID {message.id} in channel {message.channel.id} by user {message.author}")

            try:
                # Determine target language and channel
                if message.channel.id == settings.AUTO_TRANSLATE_CHANNELS["english"]:
                    target_language = "zh-cn"  # Simplified Chinese
                    target_channel_id = settings.AUTO_TRANSLATE_CHANNELS["chinese"]
                elif message.channel.id == settings.AUTO_TRANSLATE_CHANNELS["chinese"]:
                    target_language = "en"  # English
                    target_channel_id = settings.AUTO_TRANSLATE_CHANNELS["english"]
                else:
                    return

                # Translate the message
                translation = await self.translator.translate(message.content, dest=target_language)
                translated_text = translation.text

                # Get the target channel
                target_channel = self.bot.get_channel(target_channel_id)
                if not target_channel:
                    logger.error(f"Target channel {target_channel_id} not found")
                    return

                # Create webhook to mimic user
                webhook = await target_channel.create_webhook(name=message.author.display_name)
                await webhook.send(
                    content=translated_text,
                    username=message.author.display_name,
                    avatar_url=message.author.avatar.url if message.author.avatar else None
                )
                await webhook.delete()

            except Exception as e:
                logger.error(f"Translation failed: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))