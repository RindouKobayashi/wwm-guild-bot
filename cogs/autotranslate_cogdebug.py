import discord
import settings
import asyncio
import os # Added
import io # Added
import re
from discord.ext import commands
from discord import app_commands, File
from settings import logger, BASE_DIR, BOT_OWNER_ID
from googletrans import Translator

# Regex patterns for emotes and emojis
DISCORD_EMOTE_PATTERN = re.compile(r'<:(\w+):\d+>')
EMOJI_PATTERN = re.compile(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251]+')

class AutoTranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.translator = Translator()

    def extract_discord_emotes(self, text: str):
        """Extract and remove Discord custom emotes from text."""
        emotes = []
        def replace_emote(match):
            emote = match.group(0)
            emotes.append(emote)
            return ''
        cleaned_text = DISCORD_EMOTE_PATTERN.sub(replace_emote, text)
        return cleaned_text, emotes

    def extract_emojis(self, text: str):
        """Extract and remove Unicode emojis from text."""
        emojis = []
        positions = []
        def replace_emoji(match):
            emoji = match.group(0)
            start = match.start()
            end = match.end()
            emojis.append(emoji)
            positions.append((start, end, emoji))
            return ''
        cleaned_text = EMOJI_PATTERN.sub(replace_emoji, text)
        return cleaned_text, positions

    def reinsert_emojis(self, text: str, positions: list):
        """Reinsert emojis at their original positions."""
        for start, end, emoji in reversed(positions):
            text = text[:start] + emoji + text[start:]
        return text


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

# Extract and remove emotes/emojis
                cleaned_text, removed_emotes = self.extract_discord_emotes(message.content)
                cleaned_text, emoji_positions = self.extract_emojis(cleaned_text)

                # Translate the cleaned text
                translation = await self.translator.translate(cleaned_text, dest=target_language)
                translated_text = translation.text

                # Reinsert emojis
                translated_text = self.reinsert_emojis(translated_text, emoji_positions)

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