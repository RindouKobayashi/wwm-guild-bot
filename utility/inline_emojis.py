"""Render WWM inline markers using this bot application's reviewed receipts."""
import json
from pathlib import Path
import re
from utility.emotes import key

BOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r'\[#([1-9][0-9]{0,6})\]')


def validate_pack(directory, data=None):
    from utility.emoji_upload import icons
    from utility.game_assets import digest
    from utility.martial_data import validate_approval
    data = data or json.loads((directory / 'catalogue.json').read_text(encoding='utf-8'))
    validate_approval(directory)
    rows = icons(directory / 'upload-pack'); seen = set()
    for row in rows:
        identifier = key(row.get('source_id'))
        source = data.get('inline_entries', {}).get(identifier)
        if (not source or not source.get('image') or identifier in seen
                or row['key'] != 'inline_' + identifier
                or row.get('source_sha256') != source.get('image_sha256')):
            raise ValueError('Inline emoji is not bound to a verified inline asset')
        seen.add(identifier)
    expected = {k for k,v in data.get('inline_entries', {}).items() if v.get('image')}
    if seen != expected: raise ValueError('Inline upload pack coverage mismatch')
    return rows


class InlineEmojis:
    def __init__(self, assets=None, root=BOT, registry=None):
        from utility.game_assets import current
        assets = assets if assets is not None else current()
        self.entries = assets.section('emotes', {}).get('inline_entries', {})
        self.rows = {}
        if assets.directory:
            path = assets.directory / 'emotes/upload-pack/upload-manifest.json'
            try:
                self.rows = {str(r['source_id']): r for r in json.loads(path.read_text(encoding='utf-8'))['icons']}
            except (OSError, ValueError, KeyError, TypeError): pass
        if registry is not None:
            self.registry = registry
        else:
            from utility.martial import read_registry
            self.registry = read_registry(root / 'assets/discord/inline-emojis.json', root / 'data/inline-emojis.json')

    def resolve(self, identifier, application_id):
        identifier = key(identifier)
        row = self.rows.get(identifier)
        if identifier not in self.entries or not row or not application_id: return None
        receipt = self.registry.get('applications', {}).get(str(application_id), {}).get(row['key'], {})
        if not isinstance(receipt, dict): return None
        name = receipt.get('name', '')
        identity = str(receipt.get('id', ''))
        if receipt.get('sha256') != row['sha256'] or name != row['name'] or not re.fullmatch(r'[A-Za-z0-9_]{2,32}', name) or not re.fullmatch(r'[0-9]{1,25}', identity):
            return None
        return '<' + ('a' if row.get('animated') else '') + ':' + name + ':' + identity + '>'

    def render(self, text, emotion, application_id):
        """Outgoing/legacy position format only; not the received chat protocol."""
        if not isinstance(text, str) or not isinstance(emotion, dict): return text
        def identifier_for(match):
            position = match.group(1)
            if position in emotion and int(position) in emotion and key(emotion[position]) != key(emotion[int(position)]):
                return None
            value = emotion.get(position, emotion.get(int(position)))
            return key(value)
        return self._render(text, application_id, identifier_for)

    def render_received(self, text, application_id):
        """Received [#N] names inline emote N directly, independently of ext."""
        if not isinstance(text, str): return text
        return self._render(text, application_id,
            lambda match: match.group(1) if match.group(1) in self.entries else None)

    def has_translatable_text(self, text):
        if not isinstance(text, str): return False
        remainder = MARKER.sub(lambda match: '' if match.group(1) in self.entries else match.group(0), text)
        return any(character.isalpha() for character in remainder)

    def _render(self, text, application_id, identifier_for):
        growth_budget = max(0, 3800 - len(text))
        def replace(match):
            nonlocal growth_budget
            identifier = identifier_for(match)
            if not identifier: return match.group(0)
            emoji = self.resolve(identifier, application_id)
            if emoji:
                replacement = emoji
            else:
                row = self.entries.get(identifier, {})
                label = row.get('name')
                if not isinstance(label, str) or not label: label = 'Emote ' + identifier
                # Labels are catalogue text, never executable Markdown or mentions.
                import discord
                replacement = '[' + discord.utils.escape_markdown(discord.utils.escape_mentions(label))[:80] + ']'
            growth = max(0, len(replacement) - len(match.group(0)))
            if growth > growth_budget: return match.group(0)
            growth_budget -= growth
            return replacement
        # Bound expansion for malformed/oversized messages without dropping text.
        return MARKER.sub(replace, text, count=64)
