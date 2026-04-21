import discord
import settings
import asyncio
import os
import io
import re
import json
from discord.ext import commands
from discord import app_commands, File
from settings import logger, BASE_DIR, BOT_OWNER_ID
from googletrans import Translator

class AutoTranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.translator = Translator()
        self.webhooks = {}
        self.max_message_history = 200
        self.message_map_file = os.path.join(BASE_DIR, "data", "translate_message_map.json")
        
        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.message_map_file), exist_ok=True)
        
        # Load stored message mappings
        self.message_map = {}
        self._load_message_map()

    def _load_message_map(self):
        """Load message mappings from disk"""
        try:
            if os.path.exists(self.message_map_file):
                with open(self.message_map_file, 'r', encoding='utf-8') as f:
                    self.message_map = json.load(f)
                logger.info(f"Loaded {len(self.message_map)//2} message mappings from storage")
        except Exception as e:
            logger.warning(f"Could not load message map: {e}, starting fresh")
            self.message_map = {}

    def _save_message_map(self):
        """Save message mappings to disk"""
        try:
            with open(self.message_map_file, 'w', encoding='utf-8') as f:
                json.dump(self.message_map, f)
        except Exception as e:
            logger.error(f"Failed to save message map: {e}")

    def _store_message_pair(self, original_id: int, translated_id: int):
        """Store bidirectional message mapping with FIFO limit"""
        # Store both directions for lookup
        self.message_map[str(original_id)] = translated_id
        self.message_map[str(translated_id)] = original_id

        # Enforce max limit - remove oldest entries when over limit
        while len(self.message_map) > self.max_message_history * 2:
            # Remove first 2 entries (one pair)
            keys = list(self.message_map.keys())
            if len(keys) >= 2:
                del self.message_map[keys[0]]
                del self.message_map[keys[1]]
        
        self._save_message_map()

    def _get_mapped_message(self, message_id: int) -> int | None:
        """Get matching translated message id if exists"""
        return self.message_map.get(str(message_id))

    def strip_mentions(self, content: str) -> tuple[str, list, bool]:
        """
        Remove all Discord mentions from content to prevent duplicate pings in translations
        Returns tuple: (cleaned_content, list_of_extracted_entities, has_mentions)
        """
        entities = []
        has_mentions = False

        # Extract and store all custom emotes
        def emote_replacer(match):
            entities.append(('emote', match.group(0)))
            return f'__ENTITY_{len(entities)-1}__'
        
        # Extract and store all user mentions
        def user_mention_replacer(match):
            nonlocal has_mentions
            has_mentions = True
            entities.append(('mention', match.group(0)))
            return f'__ENTITY_{len(entities)-1}__'
        
        # Extract and store all role mentions
        def role_mention_replacer(match):
            nonlocal has_mentions
            has_mentions = True
            entities.append(('mention', match.group(0)))
            return f'__ENTITY_{len(entities)-1}__'
        
        # Extract and store all channel mentions
        def channel_mention_replacer(match):
            nonlocal has_mentions
            has_mentions = True
            entities.append(('mention', match.group(0)))
            return f'__ENTITY_{len(entities)-1}__'
        
        # Replace all entities with unique placeholders
        content = re.sub(r'<a?:\w+:\d+>', emote_replacer, content)
        content = re.sub(r'<@!?\d+>', user_mention_replacer, content)
        content = re.sub(r'<@&\d+>', role_mention_replacer, content)
        content = re.sub(r'<#\d+>', channel_mention_replacer, content)
        
        # Neutralize @everyone / @here
        content = re.sub(r'@(everyone|here)', '@\u200b\\1', content)
        
        return content, entities, has_mentions

    def restore_entities(self, translated_text: str, entities: list) -> str:
        """Put original emotes and mentions back into translated text using placeholders"""
        for idx, (etype, value) in enumerate(entities):
            translated_text = translated_text.replace(f'__ENTITY_{idx}__', value)
        return translated_text

    async def create_persistent_webhooks(self):
        """Create persistent webhooks for both translation channels"""
        try:
            for channel_name, channel_id in settings.AUTO_TRANSLATE_CHANNELS.items():
                target_channel = self.bot.get_channel(channel_id)

                if not target_channel:
                    logger.error(f"Target channel {channel_id} not found for webhook creation")
                    continue

                # Check if webhook already exists
                existing_webhook = None
                for webhook in await target_channel.webhooks():
                    if webhook.name == f"AutoTranslate-{channel_name.capitalize()}":
                        existing_webhook = webhook
                        break

                if existing_webhook:
                    self.webhooks[channel_name] = existing_webhook
                    logger.info(f"Reusing existing webhook for {channel_name}: {existing_webhook.id}")
                else:
                    # Create a new webhook if none exists
                    webhook = await target_channel.create_webhook(name=f"AutoTranslate-{channel_name.capitalize()}")
                    self.webhooks[channel_name] = webhook
                    logger.info(f"Created new webhook for {channel_name}: {webhook.id}")
        except Exception as e:
            logger.error(f"Failed to create persistent webhooks: {e}")


    @commands.Cog.listener() 
    async def on_message(self, message: discord.Message):
        # Ignore messages from bots
        if message.author.bot:
            return

        # Check if the message is in a channel that requires auto-translation
        if message.channel.id in settings.AUTO_TRANSLATE_CHANNELS.values():
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

                # Strip mentions from content before translation to prevent duplicate pings
                content_to_translate, extracted_entities, has_mentions = self.strip_mentions(message.content)
                
                # Check if there is actually any translatable text left (not just placeholders)
                # Strip ALL placeholders, whitespace and invisible characters
                cleaned_check = re.sub(r'__ENTITY_\d+__|[\s\u200b\u200c\u200d\ufeff]+', '', content_to_translate)
                
                logger.debug(f"AutoTranslate DEBUG: message='{message.content}', content_to_translate='{repr(content_to_translate)}', cleaned_check='{repr(cleaned_check)}', entities={len(extracted_entities)}")
                
                if not cleaned_check:
                    # No real text to translate - only emotes/mentions/entities
                    # Skip translation entirely, just use original content directly
                    logger.debug(f"AutoTranslate DEBUG: Skipping translation, using original content")
                    translated_text = message.content
                else:
                    # Translate the message (without mentions)
                    logger.debug(f"AutoTranslate DEBUG: Running translation")
                    translation = await self.translator.translate(content_to_translate, dest=target_language)
                    translated_text = translation.text
                    
                    # Restore original emotes and mentions back into translated text
                    translated_text = self.restore_entities(translated_text, extracted_entities)

                # Get the target channel
                target_channel = self.bot.get_channel(target_channel_id)
                if not target_channel:
                    logger.error(f"Target channel {target_channel_id} not found")
                    return

                # Use persistent webhook
                target_channel_name = "chinese" if message.channel.id == settings.AUTO_TRANSLATE_CHANNELS["english"] else "english"
                webhook = self.webhooks.get(target_channel_name)

                if not webhook:
                    logger.error(f"Webhook not found for target channel: {target_channel_name}")
                    return

                # Check if this message is replying to another message
                if message.reference and message.reference.message_id:
                    try:
                        original_replied_id = message.reference.message_id
                        target_replied_id = self._get_mapped_message(original_replied_id)
                        
                        # If we have a mapped translated version, use that one instead for correct language
                        if target_replied_id:
                            # Fetch the translated version that exists in our target channel
                            replied_message = await target_channel.fetch_message(target_replied_id)
                        else:
                            # Fallback: no mapping found, use original message (for older messages)
                            replied_message = await message.channel.fetch_message(original_replied_id)
                        
                        # Clean and truncate replied message content for quote
                        quoted_content = replied_message.content.replace('\n', ' ').strip()
                        if len(quoted_content) > 120:
                            quoted_content = quoted_content[:117] + "..."
                        
                        # Prepend clean reply quote header
                        reply_header = f"> **🔗 Replying to {replied_message.author.display_name}:**\n> {quoted_content}\n\n"
                        translated_text = reply_header + translated_text
                        
                    except Exception as e:
                        logger.debug(f"Could not fetch replied message: {e}")

                # Send translated message
                sent_message = await webhook.send(
                    content=translated_text,
                    username=message.author.display_name,
                    avatar_url=message.author.avatar.url if message.author.avatar else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                    wait=True
                )

                # Store message mapping for future reply lookups
                if sent_message:
                    self._store_message_pair(message.id, sent_message.id)

            except Exception as e:
                logger.error(f"Translation failed: {e}")


async def setup(bot: commands.Bot):
    cog = AutoTranslateCog(bot)
    await cog.create_persistent_webhooks()
    await bot.add_cog(cog)