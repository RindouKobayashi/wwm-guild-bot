import discord
from discord.ext import commands
import settings
from settings import logger
from discord import app_commands
from googletrans import Translator, LANGUAGES

logger.info("Loading translate context menu...")

translator = Translator()

# Reuse the exact same mention handling from AutoTranslateCog for consistency
def strip_mentions(content: str) -> tuple[str, list, bool]:
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
    import re
    content = re.sub(r'<a?:\w+:\d+>', emote_replacer, content)
    content = re.sub(r'<@!?\d+>', user_mention_replacer, content)
    content = re.sub(r'<@&\d+>', role_mention_replacer, content)
    content = re.sub(r'<#\d+>', channel_mention_replacer, content)
    
    # Neutralize @everyone / @here
    content = re.sub(r'@(everyone|here)', '@\u200b\\1', content)
    
    return content, entities, has_mentions

def restore_entities(translated_text: str, entities: list) -> str:
    """Put original emotes and mentions back into translated text using placeholders"""
    for idx, (etype, value) in enumerate(entities):
        translated_text = translated_text.replace(f'__ENTITY_{idx}__', value)
    return translated_text


class TranslateSelect(discord.ui.Select):
    def __init__(self, message_content: str, detected_lang: str):
        self.message_content = message_content
        self.detected_lang = detected_lang
        
        # Build options based on detected language
        options = []
        
        # Full language list in priority order
        language_list = [
            ("English", "en", "🇺🇸", "Translate to English"),
            ("Chinese", "zh-cn", "🇨🇳", "Translate to Simplified Chinese"),
            ("Japanese", "ja", "🇯🇵", "Translate to Japanese"),
            ("Korean", "ko", "🇰🇷", "Translate to Korean"),
            ("Spanish", "es", "🇪🇸", "Translate to Spanish"),
            ("French", "fr", "🇫🇷", "Translate to French"),
            ("German", "de", "🇩🇪", "Translate to German"),
            ("Russian", "ru", "🇷🇺", "Translate to Russian"),
        ]
        
        # Add all languages EXCEPT the detected source language, keep original priority order
        for name, code, emoji, desc in language_list:
            if code != detected_lang:
                options.append(discord.SelectOption(label=name, value=code, description=desc, emoji=emoji))
            
        super().__init__(
            placeholder=f"Detected: {LANGUAGES.get(detected_lang, 'Unknown').title()} - Select target language",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        target_lang = self.values[0]
        
        try:
            # Process content safely before translation
            content_to_translate, extracted_entities, has_mentions = strip_mentions(self.message_content)
            
            # Perform translation
            translation = await translator.translate(content_to_translate, dest=target_lang)
            translated_text = translation.text
            
            # Restore all original entities
            translated_text = restore_entities(translated_text, extracted_entities)
            
            embed = discord.Embed(
                title=f"✅ Translation",
                description=translated_text,
                color=discord.Color.green()
            )
            embed.set_footer(text=f"Translated from {LANGUAGES.get(translation.src, 'Unknown').title()} → {LANGUAGES.get(target_lang).title()}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Context menu translation failed: {e}")
            await interaction.followup.send("❌ Failed to translate message. Please try again later.", ephemeral=True)


class TranslateView(discord.ui.View):
    def __init__(self, message_content: str, detected_lang: str):
        super().__init__(timeout=120)
        self.add_item(TranslateSelect(message_content, detected_lang))


def setup_contextmenu(bot: commands.Bot):
    @bot.tree.context_menu(name="Translate")
    async def translate_context_menu(interaction: discord.Interaction, message: discord.Message):
        """Translate any message to your chosen language."""
        logger.info(f"Context menu 'Translate' invoked by {interaction.user} on message ID {message.id}")
        
        if not message.content or len(message.content.strip()) == 0:
            await interaction.response.send_message("❌ This message has no text content to translate.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Auto detect language
            detection = await translator.detect(message.content)
            detected_lang = detection.lang
            
            # Show selection menu
            view = TranslateView(message.content, detected_lang)
            
            embed = discord.Embed(
                title="🌍 Translate Message",
                description=f"Detected language: **{LANGUAGES.get(detected_lang, 'Unknown').title()}**\n\nPlease select which language you would like to translate this message to:",
                color=discord.Color.blue()
            )
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Language detection failed: {e}")
            # Fallback to default options if detection fails
            view = TranslateView(message.content, "unknown")
            embed = discord.Embed(
                title="🌍 Translate Message",
                description="Could not auto-detect language. Please select target language:",
                color=discord.Color.blue()
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


logger.info("Translate context menu loaded successfully.")