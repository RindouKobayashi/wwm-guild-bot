"""Portable standalone-chat emotes. Game extraction never runs in the bot."""
from dataclasses import dataclass
from pathlib import Path
import re

MAX_IMAGE_BYTES = 4 * 1024 * 1024
TOPIC_TYPE = '2'


def key(value):
    if type(value) is int:
        return str(value) if 0 < value < 2**31 else None
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value.strip()):
        return key(int(value.strip()))
    return None


def validate(data):
    from utility.game_assets import inside
    if data.get('format') != 1 or not isinstance(data.get('entries'), dict) or not data['entries']:
        raise ValueError('Invalid emote catalogue')
    for namespace in ('entries', 'inline_entries'):
        for identifier, row in data.get(namespace, {}).items():
            if key(identifier) != identifier or row.get('id') != int(identifier):
                raise ValueError('Invalid emote identity')
            expected = 'chat_emotion_icon_lib' if namespace == 'entries' else 'chat_emotion_frame_data'
            if row.get('source_table') != expected:
                raise ValueError('Mixed emote namespaces')
            if row.get('status') not in ('static', 'animated', 'static_preview', 'unresolved'):
                raise ValueError('Invalid emote rendering status')
            for field in ('image', 'preview'):
                relative = row.get(field)
                if relative:
                    inside(Path('.'), relative)
                    prefix = 'topic' if namespace == 'entries' else 'inline'
                    if relative not in (f'art/{prefix}_{identifier}.png', f'art/{prefix}_{identifier}.gif'):
                        raise ValueError('Invalid emote artwork path')
                    if not re.fullmatch(r'[a-f0-9]{64}', row.get(field + '_sha256', '')):
                        raise ValueError('Missing emote image checksum')
            if row['status'] != 'unresolved' and not row.get('image'):
                raise ValueError('Resolved emote has no artwork')


def inspect_image(path):
    """Offline/startup validation; never called from resolve or chat send."""
    from PIL import Image
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError('Emote image is missing or oversized')
    try:
        image = Image.open(path)
    except Image.DecompressionBombError as exc:
        raise ValueError('Emote dimensions exceed decode budget') from exc
    with image as im:
        if im.format not in ('PNG', 'WEBP', 'GIF') or not (0 < im.width <= 2048 and 0 < im.height <= 2048):
            raise ValueError('Unsupported emote image')
        if path.suffix.lower() != {'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif'}[im.format]:
            raise ValueError('Image extension does not match its contents')
        count = getattr(im, 'n_frames', 1)
        if not 1 <= count <= 600 or count * im.width * im.height > 100_000_000:
            raise ValueError('Emote animation exceeds decode budget')
        for frame in range(count):
            im.seek(frame)
            im.load()
        return count > 1


@dataclass(frozen=True)
class Emote:
    id: str
    path: Path
    preview: Path | None
    source: str
    name: str | None
    status: str

    def for_limit(self, limit):
        for path in (self.path, self.preview):
            try:
                if path and path.is_file() and path.stat().st_size <= min(limit, MAX_IMAGE_BYTES):
                    return path
            except OSError:
                continue
        return None


class EmoteResolver:
    def __init__(self, assets=None, manual_dir=None):
        from utility.game_assets import current, inside
        assets = assets if assets is not None else current()
        self.entries = {}
        self.manual_dir = Path(manual_dir or Path(__file__).resolve().parents[1] / 'data/emotion').resolve()
        if assets.directory:
            for identifier, row in assets.section('emotes', {}).get('entries', {}).items():
                if not row.get('image'):
                    continue
                try:
                    path = inside(assets.directory / 'emotes', row['image'])
                    inspect_image(path)
                    preview = inside(assets.directory / 'emotes', row['preview']) if row.get('preview') else None
                    if preview: inspect_image(preview)
                    self.entries[identifier] = Emote(identifier, path, preview, 'Game assets', row.get('name'), row['status'])
                except (OSError, ValueError):
                    continue
        self.manual = {}
        for path in self.manual_dir.glob('*'):
            self.register_manual(path.stem)

    def register_manual(self, identifier):
        """Called at startup and after approval, outside the message renderer."""
        identifier = key(identifier)
        if not identifier: return
        for suffix in ('.png', '.webp', '.gif'):
            path = self.manual_dir / (identifier + suffix)
            try:
                if not path.resolve().is_relative_to(self.manual_dir): continue
                animated = inspect_image(path)
                self.manual[identifier] = Emote(identifier, path, None, 'Manual fallback', None, 'animated' if animated else 'static')
                return
            except (OSError, ValueError):
                continue

    def resolve(self, identifier, emotion_type=None):
        identifier = key(identifier)
        # "1" is custom/user-uploaded content, never an icon-library ID.
        # Older API messages omit the discriminator; preserve that relay contract.
        if not identifier or emotion_type not in (None, 2, TOPIC_TYPE): return None
        for entry in (self.entries.get(identifier), self.manual.get(identifier)):
            if entry and entry.path.is_file(): return entry
        return None


def fallback_text(identifier, description=None):
    import discord
    text = description.strip() if isinstance(description, str) else ''
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text[:1000])) if text else 'Unknown emote (' + (key(identifier) or 'missing ID') + ')'
