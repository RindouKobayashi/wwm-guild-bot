import discord
import datetime
import json
import aiosqlite
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow, Section, Thumbnail, MediaGallery, Button, Select
import logging
from typing import Optional, Dict, List, Tuple
from deepdiff import DeepDiff

import settings
from utility.wwm import get_sect_election_ranking
from utility.api_constants import SCHOOL_NAMES, SCHOOL_EMOTES
from settings import logger, BASE_DIR

# Database for sect election snapshots
SECT_DB_PATH = BASE_DIR / "data" / "sect_monitor.db"

WELL_OF_HEAVEN_ID = 1

# Top N for snapshot vs top M for diff display
SNAPSHOT_LIMIT = 50
DIFF_DISPLAY_LIMIT = 15

BLURPLE = 0x5865F2
PURPLE = 0x9B59B6
GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xE67E22

GMT8_TZ = datetime.timezone(datetime.timedelta(hours=8))


def admin_or_staff():
    """Check if the user is an administrator OR has any of the staff roles defined in settings."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        try:
            from settings import STAFF_ROLES
            staff_role_ids = set(STAFF_ROLES.values())
        except (ImportError, AttributeError):
            raise app_commands.MissingPermissions(["administrator"])
        member_role_ids = {r.id for r in interaction.user.roles}
        if staff_role_ids & member_role_ids:
            return True
        raise app_commands.MissingPermissions(["administrator"])
    return app_commands.check(predicate)


class SectFeedView(LayoutView):
    """Components V2 LayoutView showing sect election feed with diff highlights."""
    
    def __init__(self, school_name: str, school_emoji: str, diff_data: dict, timestamp: int, previous_timestamp: int = 0, today_diff: dict = None, today_ts: int = 0):
        super().__init__(timeout=None)
        self.school_name = school_name
        self.school_emoji = school_emoji
        self.diff_data = diff_data
        self.timestamp = timestamp
        self.previous_timestamp = previous_timestamp
        self.today_diff = today_diff
        self.today_ts = today_ts
        
        self._build()
    
    def _build(self):
        """Build the feed layout."""
        inner_items = []
        
        # Header with time range
        if self.previous_timestamp > 0:
            header_text = (
                f"# {self.school_emoji} {self.school_name} — Election Feed\n"
                f"*<t:{self.previous_timestamp}:F> → <t:{self.timestamp}:F>*"
            )
        else:
            header_text = (
                f"# {self.school_emoji} {self.school_name} — Election Feed\n"
                f"*<t:{self.timestamp}:F>*"
            )
        inner_items.append(TextDisplay(header_text))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        top10 = self.diff_data.get('top10', [])
        new_entries = self.diff_data.get('new_entries', [])
        dropped = self.diff_data.get('dropped', [])
        unchanged = self.diff_data.get('unchanged', [])
        
        # Section: New entries (promoted into top 10)
        if new_entries:
            lines = []
            for entry in new_entries[:5]:  # Show max 5 new entries
                rank = entry['rank']
                nickname = entry['nickname']
                votes = entry['votes']
                change = entry.get('vote_change', 0)
                change_str = f"+{change:,}" if change >= 0 else f"{change:,}"
                lines.append(f"**#{rank}** 🆕 **{nickname}** — {votes:,} votes ({change_str})")
            inner_items.append(TextDisplay(f"### 🆕 New in Top 10\n\n" + "\n".join(lines)))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        # Section: Dropped from top 10
        if dropped:
            lines = []
            for entry in dropped[:5]:
                rank = entry.get('previous_rank', '?')
                nickname = entry['nickname']
                votes = entry['votes']
                lines.append(f"**~#{rank}** ❌ **{nickname}** — {votes:,} votes (fell out)")
            inner_items.append(TextDisplay(f"### ❌ Dropped from Top 10\n\n" + "\n".join(lines)))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        # Section: Top 15 current standings with vote changes (includes today diff in brackets)
        if top10:
            lines = []
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            for entry in top10:
                rank = entry['rank']
                nickname = entry['nickname']
                votes = entry['votes']
                change = entry.get('vote_change', 0)
                
                # Medal for top 10, plain number for 11-15
                if rank <= 10:
                    medal = medals[rank - 1]
                else:
                    medal = f"{rank}."
                
                arrow = ""
                if change > 0:
                    arrow = f" ▲ +{change:,}"
                elif change < 0:
                    arrow = f" ▼ {change:,}"
                
                # Get today's change for this player
                today_change = 0
                if self.today_diff and self.today_diff.get('top10'):
                    for te in self.today_diff['top10']:
                        if te.get('pid') == entry.get('pid'):
                            today_change = te.get('vote_change', 0)
                            break
                
                today_bracket = ""
                if today_change > 0:
                    today_bracket = f" [▲ +{today_change:,} since <t:{self.today_ts}:t>]"
                elif today_change < 0:
                    today_bracket = f" [▼ {today_change:,} since <t:{self.today_ts}:t>]"
                
                lines.append(f"{medal} **{nickname}** — {votes:,} votes{arrow}{today_bracket}")
            
            inner_items.append(TextDisplay(f"### 📊 Top 10 Standings\n\n" + "\n".join(lines[:10])))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
            if len(lines) > 10:
                inner_items.append(TextDisplay("\n".join(lines[10:15])))
                inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        # Footer: total candidates tracked
        total = self.diff_data.get('total_candidates', 0)
        inner_items.append(TextDisplay(
            f"Tracking top **{SNAPSHOT_LIMIT}** of **{total:,}** total candidates"
        ))
        
        container = Container(*inner_items, accent_color=PURPLE)
        self.add_item(container)


class SectCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = SECT_DB_PATH
        # Per-sect state: school_id -> {snapshot, ts, channel_id, thread_id}
        self.sect_states: Dict[int, Dict] = {}
        self._current_sect_id = WELL_OF_HEAVEN_ID
        
    async def cog_load(self):
        await self._init_database()
        await self._load_all_states()
        self.sect_hourly_task.start()
        logger.debug("Sect election hourly task started for all configured sects")
        
    async def cog_unload(self):
        if self.sect_hourly_task.is_running():
            self.sect_hourly_task.cancel()
    
    async def _init_database(self):
        (BASE_DIR / "data").mkdir(exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sect_election_snapshots (
                    ts INTEGER PRIMARY KEY,
                    school_id INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sect_election_daily (
                    date TEXT,
                    school_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (date, school_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sect_feed_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.commit()
    
    async def _load_all_states(self):
        """Load per-sect last snapshot, thread ID and channel from database."""
        async with aiosqlite.connect(self.db_path) as db:
            # Load latest snapshot per school_id
            cursor = await db.execute("""
                SELECT school_id, MAX(ts), snapshot_json 
                FROM sect_election_snapshots 
                GROUP BY school_id
            """)
            async for school_id, ts, snapshot_json in cursor:
                channel_key = None
                for key, cid in settings.SECT_CHANNELS.items():
                    # Map channel key -> school_id using reverse lookup from SCHOOL_NAMES
                    pass
                # Simple mapping from SCHOOL_NAMES by id
                self.sect_states[school_id] = {
                    'last_snapshot_ts': ts,
                    'last_snapshot': json.loads(snapshot_json) if snapshot_json else None,
                    'thread_id': None
                }
                logger.debug(f"Loaded last snapshot for school_id={school_id} from timestamp {ts}")
            
            # Load thread IDs per school_id
            cursor = await db.execute("SELECT key, value FROM sect_feed_state")
            async for key, value in cursor:
                if key.startswith('feed_thread_id_'):
                    try:
                        sid = int(key.replace('feed_thread_id_', ''))
                        self.sect_states.setdefault(sid, {})['thread_id'] = int(value)
                    except ValueError:
                        pass
    
    async def _save_snapshot(self, ts: int, data: list, school_id: int = WELL_OF_HEAVEN_ID):
        """Save a new snapshot to the database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO sect_election_snapshots (ts, school_id, snapshot_json) VALUES (?, ?, ?)",
                (ts, school_id, json.dumps(data, ensure_ascii=False))
            )
            await db.commit()
    
    async def _get_today_10am_snapshot(self, school_id: int = WELL_OF_HEAVEN_ID) -> Tuple[Optional[list], Optional[int]]:
        """Get or create today's 10am GMT+8 baseline snapshot for a given school.
        
        If today's snapshot doesn't exist yet (before 10am), fall back to yesterday's
        so the 'since 10am' bracket persists across midnight.
        """
        now = datetime.datetime.now(GMT8_TZ)
        today_10am = now.replace(hour=10, minute=0, second=0, microsecond=0)
        date_str = today_10am.strftime("%Y-%m-%d")
        
        async with aiosqlite.connect(self.db_path) as db:
            # Try today's snapshot first
            cursor = await db.execute(
                "SELECT ts, snapshot_json FROM sect_election_daily WHERE date = ? AND school_id = ?",
                (date_str, school_id)
            )
            row = await cursor.fetchone()
            
            if row:
                ts, snapshot_json = row
                logger.debug(f"Loaded today's 10am snapshot from {date_str}")
                return json.loads(snapshot_json), ts
            
            # If before 10am, try yesterday's snapshot to keep the bracket alive across midnight
            if now < today_10am:
                yesterday = now - datetime.timedelta(days=1)
                yesterday_10am = yesterday.replace(hour=10, minute=0, second=0, microsecond=0)
                yesterday_str = yesterday_10am.strftime("%Y-%m-%d")
                
                cursor = await db.execute(
                    "SELECT ts, snapshot_json FROM sect_election_daily WHERE date = ? AND school_id = ?",
                    (yesterday_str, school_id)
                )
                row = await cursor.fetchone()
                
                if row:
                    ts, snapshot_json = row
                    logger.debug(f"Loaded yesterday's 10am snapshot from {yesterday_str} (after midnight fallback)")
                    return json.loads(snapshot_json), ts
            
            # If we're past 10am today and no snapshot exists, create one
            if now >= today_10am:
                current_data = await self._fetch_election_data(limit=SNAPSHOT_LIMIT, school_id=school_id)
                if current_data:
                    candidates, _ = current_data
                    ts = int(today_10am.timestamp())
                    await db.execute(
                        "INSERT OR REPLACE INTO sect_election_daily (date, school_id, ts, snapshot_json) VALUES (?, ?, ?, ?)",
                        (date_str, school_id, ts, json.dumps(candidates, ensure_ascii=False))
                    )
                    await db.commit()
                    logger.debug(f"Created today's 10am snapshot at {date_str} for school_id={school_id}")
                    return candidates, ts
            
            return None, None
    
    async def _fetch_election_data(self, limit: int = SNAPSHOT_LIMIT, school_id: int = WELL_OF_HEAVEN_ID) -> Optional[Tuple[list, int]]:
        """Fetch current election rankings. Returns (candidates_list, total_candidates_count)."""
        try:
            response = await get_sect_election_ranking(school_id, limit=limit)
            if not response or response.get('code') != 0:
                logger.error("Failed to fetch sect election data")
                return None
            
            result = response.get('result', {})
            rank_list = result.get('rank_list', [])
            total = result.get('rank_total_len', len(rank_list))
            
            # Extract top N with essential fields
            candidates = []
            for idx, entry in enumerate(rank_list[:limit], 1):
                player_info = entry.get('player_info', {})
                base = player_info.get('base', {})
                candidates.append({
                    'rank': idx,
                    'pid': entry.get('pid') or player_info.get('id'),
                    'nickname': base.get('nickname', 'Unknown'),
                    'level': base.get('level', 0),
                    'number_id': str(base.get('number_id', '')),
                    'votes': entry.get('score', 0),
                })
            
            return candidates, total
        except Exception as e:
            logger.error(f"Failed to fetch election data: {e}")
            return None
    
    def _compute_diff(self, old_snapshot: list, new_snapshot: list, total_candidates: int = 0) -> dict:
        """Compute diff between two snapshots, focusing on top 10."""
        old_top10_by_pid = {c['pid']: c for c in old_snapshot[:DIFF_DISPLAY_LIMIT] if c.get('pid')}
        new_top10 = new_snapshot[:DIFF_DISPLAY_LIMIT]
        
        new_entries = []
        dropped = []
        unchanged = []
        
        # Analyze new top 10
        for entry in new_top10:
            pid = entry.get('pid')
            if not pid:
                continue
            
            if pid in old_top10_by_pid:
                # Was in previous top 10
                old_entry = old_top10_by_pid[pid]
                vote_change = entry['votes'] - old_entry['votes']
                rank_change = old_entry['rank'] - entry['rank']  # Positive = moved up
                
                # Always include vote_change so feed shows gains/losses
                unchanged.append({**entry, 'vote_change': vote_change, 'rank_change': rank_change})
            else:
                # New entry in top 10
                old_rank = None
                for i, old_c in enumerate(old_snapshot[:SNAPSHOT_LIMIT], 1):
                    if old_c.get('pid') == pid:
                        old_rank = i
                        break
                
                new_entries.append({
                    **entry,
                    'previous_rank': old_rank,
                    'vote_change': entry['votes']  # All votes are "new" for this position
                })
        
        # Find dropped entries (in old top 10 but not in new top 10)
        new_top10_pids = {c['pid'] for c in new_top10 if c.get('pid')}
        for pid, old_entry in old_top10_by_pid.items():
            if pid not in new_top10_pids:
                dropped.append({
                    **old_entry,
                    'current_votes': 0  # Will be updated below if still in top 50
                })
        
        # Update dropped entries with current votes if they're still in top 50
        new_snapshot_by_pid = {c['pid']: c for c in new_snapshot if c.get('pid')}
        for dropped_entry in dropped:
            pid = dropped_entry.get('pid')
            if pid in new_snapshot_by_pid:
                dropped_entry['current_votes'] = new_snapshot_by_pid[pid]['votes']
        
        # Merge vote_change from unchanged/new_entries into top10 for display
        top10_with_changes = []
        unchanged_by_pid = {e['pid']: e for e in unchanged}
        new_entries_by_pid = {e['pid']: e for e in new_entries}
        for entry in new_top10:
            pid = entry.get('pid')
            if pid in unchanged_by_pid:
                top10_with_changes.append({**entry, 'vote_change': unchanged_by_pid[pid].get('vote_change', 0), 'rank_change': unchanged_by_pid[pid].get('rank_change', 0)})
            elif pid in new_entries_by_pid:
                top10_with_changes.append({**entry, 'vote_change': new_entries_by_pid[pid].get('vote_change', entry['votes']), 'rank_change': 0})
            else:
                top10_with_changes.append({**entry, 'vote_change': 0, 'rank_change': 0})
        
        return {
            'top10': top10_with_changes,
            'new_entries': new_entries,
            'dropped': dropped,
            'unchanged': unchanged,
            'total_candidates': total_candidates if total_candidates > 0 else len(new_snapshot)
        }
    
    @tasks.loop(minutes=10)
    async def sect_hourly_task(self):
        """Run every 10 minutes (GMT+8)."""
        now = datetime.datetime.now(GMT8_TZ)
        
        # Loop through all configured sects
        for key, channel_id in settings.SECT_CHANNELS.items():
            school_id = self._resolve_school_id_from_key(key)
            if school_id is None:
                continue
            self._current_sect_id = school_id
            state = self.sect_states.setdefault(school_id, {})
            last_ts = state.get('last_snapshot_ts')
            
            # First run: no previous snapshot → always run
            if not last_ts:
                logger.debug(f"Sect election first run for {key} (school_id={school_id})")
                await self._run_feed_update(school_id=school_id)
                continue
            
            # Only post if we haven't posted in this 10-minute window yet
            current_window_ts = int(now.timestamp() // 600 * 600)  # 600 seconds = 10 minutes
            if last_ts // 600 == current_window_ts // 600:
                continue
            
            logger.debug(f"Sect election update for {key} (school_id={school_id}) at {now.strftime('%H:%M')}")
            await self._run_feed_update(school_id=school_id)
        
        self._current_sect_id = WELL_OF_HEAVEN_ID
    
    def _resolve_school_id_from_key(self, key: str) -> Optional[int]:
        """Resolve school_id from SECT_CHANNELS key by matching SCHOOL_NAMES IDs from api_constants."""
        if not hasattr(self, '_key_to_school_id'):
            # These MUST match SCHOOL_NAMES in utility/api_constants.py
            self._key_to_school_id = {
                'well_of_heaven': 1,
                'silver_needle': 4,
                'raging_tides': 3,
                'midnight_blades': 6,
                'nine_mortal_ways': 11,
                'velvet_shade': 12,
                'masked_troupe': 2,
            }
        return self._key_to_school_id.get(key)
    
    async def _run_feed_update(self, school_id: int = WELL_OF_HEAVEN_ID):
        """Execute the feed update workflow for a specific school."""
        channel_key = None
        channel_id = None
        for k, cid in settings.SECT_CHANNELS.items():
            if self._resolve_school_id_from_key(k) == school_id:
                channel_key = k
                channel_id = cid
                break
        
        if not channel_id:
            logger.error(f"No channel configured for school_id={school_id}")
            return
        
        channel = self.bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            logger.error(f"Sect channel {self.sect_channel_id} not found")
            return
        
        # Fetch current data
        fetch_result = await self._fetch_election_data(limit=SNAPSHOT_LIMIT, school_id=school_id)
        if not fetch_result:
            logger.error("Failed to fetch election data for feed update")
            return
        current_data, total_candidates = fetch_result
        
        now_ts = int(datetime.datetime.now(GMT8_TZ).timestamp())
        
        state = self.sect_states.setdefault(school_id, {})
        last_snapshot = state.get('last_snapshot')
        last_snapshot_ts = state.get('last_snapshot_ts')
        
        # First snapshot
        if not last_snapshot:
            logger.debug(f"First snapshot for school_id={school_id} — saving baseline")
            await self._save_snapshot(now_ts, current_data, school_id=school_id)
            state['last_snapshot'] = current_data
            state['last_snapshot_ts'] = now_ts
            
            # Post initial feed
            diff_data = {
                'top10': current_data[:DIFF_DISPLAY_LIMIT],
                'new_entries': [],
                'dropped': [],
                'unchanged': [],
                'total_candidates': total_candidates
            }
            await self._post_feed(channel, diff_data, now_ts, school_id=school_id, is_initial=True)
            return
        
        # Compute diff from previous snapshot
        diff_data = self._compute_diff(last_snapshot, current_data, total_candidates=total_candidates)
        
        # Compute today's diff from 10am baseline
        today_snapshot, today_ts = await self._get_today_10am_snapshot(school_id=school_id)
        if today_snapshot:
            today_diff = self._compute_diff(today_snapshot, current_data, total_candidates=total_candidates)
            diff_data['today_diff'] = today_diff
            diff_data['today_ts'] = today_ts
        else:
            diff_data['today_diff'] = None
            diff_data['today_ts'] = 0
        
        # Always post feed
        await self._post_feed(channel, diff_data, now_ts, school_id=school_id)
        
        # Save new snapshot
        await self._save_snapshot(now_ts, current_data, school_id=school_id)
        state['last_snapshot'] = current_data
        state['last_snapshot_ts'] = now_ts
    
    async def _save_thread_id(self, thread_id: int, school_id: int = WELL_OF_HEAVEN_ID):
        """Save the feed thread ID for a school."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO sect_feed_state (key, value) VALUES (?, ?)",
                (f"feed_thread_id_{school_id}", str(thread_id))
            )
            await db.commit()
    
    async def _post_feed(self, channel: discord.TextChannel, diff_data: dict, timestamp: int, school_id: int = WELL_OF_HEAVEN_ID, is_initial: bool = False):
        """Post feed to channel in a persistent thread per school."""
        try:
            state = self.sect_states.setdefault(school_id, {})
            thread_id = state.get('thread_id')
            thread = None
            if thread_id:
                try:
                    thread = channel.get_thread(thread_id)
                    if thread and thread.archived:
                        thread = None
                except Exception:
                    thread = None
            
            if not thread:
                thread = await channel.create_thread(
                    name=f"Sect Election Feed — {SCHOOL_NAMES.get(school_id, 'Unknown')}",
                    type=discord.ChannelType.public_thread
                )
                state['thread_id'] = thread.id
                await self._save_thread_id(thread.id, school_id=school_id)
                logger.debug(f"Created persistent feed thread for school_id={school_id}: {thread.id}")
            
            previous_ts = state.get('last_snapshot_ts') or 0
            today_diff = diff_data.get('today_diff')
            today_ts = diff_data.get('today_ts', 0)
            view = SectFeedView(
                school_name=SCHOOL_NAMES.get(school_id, SCHOOL_NAMES.get(WELL_OF_HEAVEN_ID, 'Unknown')),
                school_emoji=SCHOOL_EMOTES.get(school_id, SCHOOL_EMOTES.get(WELL_OF_HEAVEN_ID, '')),
                diff_data=diff_data,
                timestamp=timestamp,
                previous_timestamp=previous_ts,
                today_diff=today_diff,
                today_ts=today_ts
            )
            
            await thread.send(view=view)
            logger.debug(f"Posted sect feed for school_id={school_id} to thread {thread.id}")
            
        except Exception as e:
            logger.error(f"Failed to post feed for school_id={school_id}: {e}", exc_info=True)
    
    @sect_hourly_task.before_loop
    async def before_sect_hourly(self):
        await self.bot.wait_until_ready()
    
    # ── Commands ──────────────────────────────────────────────────────
    
    @app_commands.command(name="sect-feed", description="Post a sect election feed update")
    @admin_or_staff()
    @app_commands.describe(sect="Which sect to check")
    @app_commands.choices(sect=[
        app_commands.Choice(name="All", value="all"),
        *[app_commands.Choice(name=name, value=str(sid)) for sid, name in SCHOOL_NAMES.items() if sid != 100]
    ])
    async def sect_feed(self, interaction: discord.Interaction, sect: Optional[str] = None):
        """Manually trigger a sect election feed update."""
        if sect == "all":
            await interaction.response.send_message("🔄 Fetching sect election data for all sects...", ephemeral=True)
            for key in settings.SECT_CHANNELS:
                sid = self._resolve_school_id_from_key(key)
                if sid is None:
                    continue
                await self._run_feed_update(school_id=sid)
            await interaction.edit_original_response(content="✅ All sect feeds updated")
            return
        
        school_id = None
        if sect:
            try:
                school_id = int(sect)
            except ValueError:
                await interaction.response.send_message("❌ Unknown sect. Pick one from the dropdown.", ephemeral=True)
                return
        
        if school_id is None:
            school_id = self._current_sect_id if self._current_sect_id else WELL_OF_HEAVEN_ID
        
        # Only fetch if there is a channel configured for this sect
        channel_id = None
        for k, cid in settings.SECT_CHANNELS.items():
            if self._resolve_school_id_from_key(k) == school_id:
                channel_id = cid
                break
        
        if not channel_id:
            label = SCHOOL_NAMES.get(school_id, 'Unknown')
            await interaction.response.send_message(f"⚠️ {label} has no configured channel.", ephemeral=True)
            return
        
        label = SCHOOL_NAMES.get(school_id, SCHOOL_NAMES.get(WELL_OF_HEAVEN_ID, 'Unknown'))
        await interaction.response.send_message(f"🔄 Fetching {label} sect election data...", ephemeral=True)
        self._current_sect_id = school_id
        await self._run_feed_update(school_id=school_id)
        await interaction.edit_original_response(content=f"✅ {label} feed update complete")
    
    @app_commands.command(name="sect-status", description="Check sect monitoring status")
    @app_commands.describe(sect="Which sect to check")
    @app_commands.choices(sect=[
        app_commands.Choice(name=name, value=str(sid))
        for sid, name in SCHOOL_NAMES.items() if sid != 100
    ])
    async def sect_status(self, interaction: discord.Interaction, sect: Optional[str] = None):
        """Show the current status of sect monitoring."""
        school_id = None
        if sect:
            try:
                school_id = int(sect)
            except ValueError:
                await interaction.response.send_message("❌ Unknown sect. Pick one from the dropdown.", ephemeral=True)
                return
        
        if school_id is None:
            school_id = self._current_sect_id if self._current_sect_id else WELL_OF_HEAVEN_ID
        
        state = self.sect_states.get(school_id, {})
        label = SCHOOL_NAMES.get(school_id, SCHOOL_NAMES.get(WELL_OF_HEAVEN_ID, 'Unknown'))
        emoji = SCHOOL_EMOTES.get(school_id, SCHOOL_EMOTES.get(WELL_OF_HEAVEN_ID, ''))
        channel_id = None
        channel_key = None
        for k, cid in settings.SECT_CHANNELS.items():
            if self._resolve_school_id_from_key(k) == school_id:
                channel_id = cid
                channel_key = k
                break
        
        embed = discord.Embed(
            title=f"{emoji} {label} Sect Monitor",
            color=PURPLE
        )
        
        status = "✅ Running" if self.sect_hourly_task.is_running() else "❌ Stopped"
        embed.add_field(name="Task Status", value=status, inline=True)
        embed.add_field(name="Channel", value=f"<#{channel_id}>" if channel_id else "Not set", inline=True)
        embed.add_field(name="Thread", value=f"<#{state.get('thread_id')}>" if state.get('thread_id') else "Not created", inline=True)
        
        last_ts = state.get('last_snapshot_ts')
        if last_ts:
            last_time = datetime.datetime.fromtimestamp(last_ts, tz=GMT8_TZ)
            embed.add_field(
                name="Last Snapshot",
                value=f"<t:{last_ts}:R> ({last_time.strftime('%Y-%m-%d %H:%M GMT+8')})",
                inline=False
            )
        
        last_snapshot = state.get('last_snapshot')
        if last_snapshot:
            top3 = last_snapshot[:3]
            top3_str = "\n".join(
                f"{'🥇' if i==0 else '🥈' if i==1 else '🥉'} {c['nickname']} — {c['votes']:,} votes"
                for i, c in enumerate(top3)
            )
            embed.add_field(name="Current Top 3", value=top3_str, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SectCog(bot))