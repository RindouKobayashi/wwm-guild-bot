import asyncio
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
from utility.wwm import get_sect_election_ranking, get_school_chief_history, get_bulk_players_info_multi_hostnum, get_bulk_players_info
from utility.api_constants import SCHOOL_NAMES, SCHOOL_EMOTES, SCHOOL_RANKING, VOTE_COUNTS
from settings import logger, BASE_DIR

# Database for sect election snapshots
SECT_DB_PATH = BASE_DIR / "data" / "sect_monitor.db"
MARKET_DB_PATH = BASE_DIR / "data" / "market.db"

WELL_OF_HEAVEN_ID = 1

# Top N for snapshot vs top M for diff display
SNAPSHOT_LIMIT = 50
DIFF_DISPLAY_LIMIT = 10

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
        
        # Section: New entries (entered top 10)
        if new_entries:
            lines = []
            for entry in new_entries[:5]:  # Show max 5 new entries
                rank = entry['rank']
                nickname = entry['nickname']
                votes = entry['votes']
                change = entry.get('vote_change', 0)
                change_str = f"+{change:,}" if change >= 0 else f"{change:,}"
                prev_rank = entry.get('previous_rank')
                prev_str = f" (was #{prev_rank} previously)" if prev_rank else ""
                lines.append(f"**#{rank}** 🆕 **{nickname}** — {votes:,} votes ({change_str}){prev_str}")
            inner_items.append(TextDisplay(f"### 🆕 New in Top 10\n\n" + "\n".join(lines)))
            inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        # Section: Dropped from top 10
        if dropped:
            lines = []
            for entry in dropped[:5]:
                rank = entry.get('previous_rank', '?')
                nickname = entry['nickname']
                votes = entry['votes']
                # Check if still in top 50
                current_votes = entry.get('current_votes', 0)
                current_rank = entry.get('current_rank')
                if current_rank:
                    lines.append(f"**~#{rank}** ❌ **{nickname}** — {votes:,} votes (fell out, currently #{current_rank} with {current_votes:,} votes)")
                else:
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
        # Backfill historical election data for all configured sects
        await self._backfill_all_election_history()
        self.sect_hourly_task.start()
        self.sect_weekly_check.start()
        logger.debug("Sect election hourly task started for all configured sects")
        logger.debug("Sect election weekly check task started")
        
    async def cog_unload(self):
        if self.sect_hourly_task.is_running():
            self.sect_hourly_task.cancel()
        if self.sect_weekly_check.is_running():
            self.sect_weekly_check.cancel()
    
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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sect_election_history (
                    school_id INTEGER NOT NULL,
                    session_number INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,
                    election_data_json TEXT NOT NULL,
                    PRIMARY KEY (school_id, session_number)
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
    
    async def _backfill_all_election_history(self):
        """Backfill historical election data for all sects defined in SCHOOL_NAMES.
        Runs once on startup to populate sect_election_history table.
        """
        for school_id in SCHOOL_NAMES:
            if school_id == 100:  # Skip "Sectless"
                continue
            await self._backfill_school_election_history(school_id)
        logger.info("Election history backfill complete for all sects")

    async def _check_new_elections(self):
        """Weekly check for new election sessions across all sects.
        Fetches skip=0 (most recent) for each sect and compares with the latest
        known session. If a new one is found, appends it as a new session.
        """
        for school_id in SCHOOL_NAMES:
            if school_id == 100:
                continue
            try:
                await self._check_school_new_election(school_id)
            except Exception as e:
                logger.error(f"Failed to check new election for school_id={school_id}: {e}")

    async def _check_school_new_election(self, school_id: int):
        """Check if there's a new election session for one school and append it."""
        # Fetch the most recent election (skip=0)
        response = await get_school_chief_history(school_id, skip=0, limit=1)
        if not response:
            logger.debug(f"No response checking new election for school_id={school_id}")
            return

        result_list = response.get('result', [])
        if not isinstance(result_list, list) or len(result_list) == 0:
            return

        entry = result_list[0]
        ts_raw = entry.get('ts')
        chief = entry.get('chief', {})
        if not ts_raw or not chief:
            return

        new_ts = int(ts_raw)

        async with aiosqlite.connect(self.db_path) as db:
            # Get the latest known session for this school
            cursor = await db.execute(
                "SELECT MAX(session_number), MAX(timestamp) FROM sect_election_history WHERE school_id = ?",
                (school_id,)
            )
            row = await cursor.fetchone()
            max_session = row[0] if row and row[0] else 0
            max_ts = row[1] if row and row[1] else 0

            # If the new timestamp is different from the latest known, it's a new election
            if new_ts > max_ts:
                new_session = max_session + 1
                logger.info(f"New election detected for school_id={school_id}: session {new_session} (ts={new_ts})")
                await db.execute(
                    "INSERT OR REPLACE INTO sect_election_history (school_id, session_number, timestamp, election_data_json) VALUES (?, ?, ?, ?)",
                    (school_id, new_session, new_ts, json.dumps(chief, ensure_ascii=False))
                )
                await db.commit()
                logger.info(f"Appended new session {new_session} for school_id={school_id}")
            else:
                logger.debug(f"No new election for school_id={school_id} (latest ts={max_ts})")

    async def _backfill_school_election_history(self, school_id: int):
        """Backfill historical election data for one school.
        
        Fetches election history starting from skip=0 (most recent) with a large limit.
        Each response batch can contain multiple election sessions.
        We iterate until we get an empty result, then reverse-number the sessions.
        """
        # Check if we already have data for this school
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM sect_election_history WHERE school_id = ?",
                (school_id,)
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            if count > 0:
                logger.debug(f"Election history already exists for school_id={school_id} ({count} sessions), skipping backfill")
                return

        logger.info(f"Backfilling election history for school_id={school_id}...")

        # Collect all results with their skip indices
        all_results = []  # list of (skip_index, timestamp, chief_dict)
        skip = 0
        batch_limit = 10  # fetch up to 50 at a time
        max_skips = 50  # safety limit

        while skip < max_skips:
            try:
                response = await get_school_chief_history(school_id, skip=skip, limit=batch_limit)
                if not response:
                    logger.warning(f"No response from school_chief_history API for school_id={school_id}, skip={skip}")
                    break

                # The API returns result as a list of election objects
                result_list = response.get('result', [])
                if not isinstance(result_list, list) or len(result_list) == 0:
                    logger.info(f"No more election history for school_id={school_id} at skip={skip}")
                    break

                for election_entry in result_list:
                    ts_raw = election_entry.get('ts')
                    chief = election_entry.get('chief', {})
                    if not ts_raw or not chief:
                        logger.info(f"Empty election entry at skip={skip}, no more history for school_id={school_id}")
                        skip += 1  # Still increment skip to avoid infinite loop on same skip
                        break  # Empty entry means we've hit the end

                    ts_int = int(ts_raw)  # Remove decimal, discord-friendly
                    all_results.append((skip, ts_int, chief))
                    skip += 1  # Each entry in result consumes one skip

                logger.info(f"Fetched {len(result_list)} election(s) for school_id={school_id}, total so far: {len(all_results)}")

            except Exception as e:
                logger.error(f"Failed to fetch election history for school_id={school_id} at skip={skip}: {e}")
                break

        if not all_results:
            logger.info(f"No election history found for school_id={school_id}")
            return

        # Now we have all_results. The list is in order of discovery (skip=0 first = most recent).
        # Total sessions = N. session_number = N - skip_index.
        # So skip=0 (first in list) → session N, skip=N-1 (last in list) → session 1
        total_sessions = len(all_results)

        async with aiosqlite.connect(self.db_path) as db:
            for skip_idx, ts_int, chief in all_results:
                session_number = total_sessions - skip_idx
                logger.debug(f"Storing school_id={school_id} session {session_number}/{total_sessions} (ts={ts_int})")
                await db.execute(
                    "INSERT OR REPLACE INTO sect_election_history (school_id, session_number, timestamp, election_data_json) VALUES (?, ?, ?, ?)",
                    (school_id, session_number, ts_int, json.dumps(chief, ensure_ascii=False))
                )
            await db.commit()

        logger.info(f"Stored {total_sessions} election sessions for school_id={school_id}")

    async def _resolve_player_info_for_history(self, chief_dict: dict) -> Tuple[List[dict], Dict[str, str], Dict[str, str]]:
        """Given a chief dict from election history, resolve PIDs to nicknames and number_ids.
        
        Groups PIDs by their hostnum and makes a single multi-hostnum API call
        so the server can resolve all players at once.

        Returns:
            sorted_rankings: List of {rank, pid, hostnum, votes} sorted by rank
            pid_to_nickname: Dict mapping pid -> nickname
            pid_to_number_id: Dict mapping pid -> number_id
        """
        # Group PIDs by hostnum from the chief dict
        hostnum2pids: Dict[int, List[str]] = {}
        for rank_str, entry in chief_dict.items():
            if isinstance(entry, list) and len(entry) >= 3:
                pid = entry[0]
                hostnum = entry[1]
                hostnum2pids.setdefault(hostnum, []).append(pid)

        # Bulk fetch player info across all hostnums in one call
        player_data = None
        if hostnum2pids:
            try:
                player_data = await get_bulk_players_info_multi_hostnum(hostnum2pids, fields=["base"])
            except Exception as e:
                logger.error(f"Failed to bulk fetch player info across hostnums: {e}")

        # Build lookup maps
        pid_to_nickname = {}
        pid_to_number_id = {}
        if player_data and 'result' in player_data:
            for pid, data in player_data['result'].items():
                base = data.get('base', {})
                pid_to_nickname[pid] = base.get('nickname', 'Unknown')
                pid_to_number_id[pid] = str(base.get('number_id', ''))
            logger.debug(f"Resolved {len(pid_to_nickname)} player nicknames for history across {len(hostnum2pids)} hostnums")

        # Sort rankings by rank number
        sorted_rankings = []
        for rank_str, entry in chief_dict.items():
            if isinstance(entry, list) and len(entry) >= 3:
                try:
                    rank = int(rank_str)
                except (ValueError, TypeError):
                    continue
                sorted_rankings.append({
                    'rank': rank,
                    'pid': entry[0],
                    'hostnum': entry[1],
                    'votes': entry[2],
                })
        sorted_rankings.sort(key=lambda x: x['rank'])

        return sorted_rankings, pid_to_nickname, pid_to_number_id

    async def _get_today_10am_snapshot(self, school_id: int = WELL_OF_HEAVEN_ID, current_data: Optional[list] = None) -> Tuple[Optional[list], Optional[int]]:
        """Get or create today's 10am GMT+8 baseline snapshot for a given school.
        
        If today's snapshot doesn't exist yet (before 10am), fall back to yesterday's
        so the 'since 10am' bracket persists across midnight.
        
        If current_data is provided and we need to create a new baseline, use it
        instead of making a separate API call to avoid timing discrepancies.
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
                # Use provided current_data if available, otherwise fetch fresh
                if current_data is not None:
                    candidates = current_data
                else:
                    fetch_result = await self._fetch_election_data(limit=SNAPSHOT_LIMIT, school_id=school_id)
                    if not fetch_result:
                        return None, None
                    candidates, _ = fetch_result
                
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
        old_topN_by_pid = {c['pid']: c for c in old_snapshot[:DIFF_DISPLAY_LIMIT] if c.get('pid')}
        new_topN = new_snapshot[:DIFF_DISPLAY_LIMIT]
        
        new_entries = []
        dropped = []
        unchanged = []
        
        # Analyze new top 10
        for entry in new_topN:
            pid = entry.get('pid')
            if not pid:
                continue
            
            if pid in old_topN_by_pid:
                # Was in previous top 10
                old_entry = old_topN_by_pid[pid]
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
                
                # Calculate actual vote change from old snapshot if player existed before
                old_votes = 0
                for old_c in old_snapshot[:SNAPSHOT_LIMIT]:
                    if old_c.get('pid') == pid:
                        old_votes = old_c.get('votes', 0)
                        break
                vote_change = entry['votes'] - old_votes if old_votes else entry['votes']
                new_entries.append({
                    **entry,
                    'previous_rank': old_rank,
                    'vote_change': vote_change
                })
        
        # Find dropped entries (in old top 10 but not in new top 10)
        new_topN_pids = {c['pid'] for c in new_topN if c.get('pid')}
        for pid, old_entry in old_topN_by_pid.items():
            if pid not in new_topN_pids:
                dropped.append({
                    **old_entry,
                    'previous_rank': old_entry.get('rank'),  # Their old rank before dropping
                    'current_votes': 0,  # Will be updated below if still in top 50
                    'current_rank': None  # Will be updated below if still in top 50
                })
        
        # Update dropped entries with current votes and rank if they're still in top 50
        new_snapshot_by_pid = {c['pid']: c for c in new_snapshot if c.get('pid')}
        for dropped_entry in dropped:
            pid = dropped_entry.get('pid')
            if pid in new_snapshot_by_pid:
                dropped_entry['current_votes'] = new_snapshot_by_pid[pid]['votes']
                dropped_entry['current_rank'] = new_snapshot_by_pid[pid]['rank']
        
        # Merge vote_change from unchanged/new_entries into topN for display (ranks 1-10)
        topN_with_changes = []
        unchanged_by_pid = {e['pid']: e for e in unchanged}
        new_entries_by_pid = {e['pid']: e for e in new_entries}
        for entry in new_topN:
            pid = entry.get('pid')
            if pid in unchanged_by_pid:
                topN_with_changes.append({**entry, 'vote_change': unchanged_by_pid[pid].get('vote_change', 0), 'rank_change': unchanged_by_pid[pid].get('rank_change', 0)})
            elif pid in new_entries_by_pid:
                topN_with_changes.append({**entry, 'vote_change': new_entries_by_pid[pid].get('vote_change', entry['votes']), 'rank_change': 0})
            else:
                topN_with_changes.append({**entry, 'vote_change': 0, 'rank_change': 0})
        
        # Also include ranks 11-15 in the output with their today vote_change from old snapshot
        old_all_pid = {c['pid']: c for c in old_snapshot[:SNAPSHOT_LIMIT] if c.get('pid')}
        for entry in new_snapshot[DIFF_DISPLAY_LIMIT:15]:
            pid = entry.get('pid')
            vote_change = 0
            if pid and pid in old_all_pid:
                vote_change = entry['votes'] - old_all_pid[pid]['votes']
            topN_with_changes.append({**entry, 'vote_change': vote_change, 'rank_change': 0})
        
        return {
            'top10': topN_with_changes,
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
                'top10': current_data[:15],  # Include ranks 1-15 for display
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
        # Pass current_data so if a new baseline needs to be created, it uses the same data
        today_snapshot, today_ts = await self._get_today_10am_snapshot(school_id=school_id, current_data=current_data)
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

    @tasks.loop(time=datetime.time(hour=10, minute=0, tzinfo=GMT8_TZ), count=1)
    async def sect_weekly_check(self):
        """Check for new election sessions every Monday at 10:00 GMT+8.
        Runs once per week using the loop's built-in scheduling.
        """
        now = datetime.datetime.now(GMT8_TZ)
        # Only run on Monday
        if now.weekday() == 0:
            logger.info("Running weekly new election check...")
            await self._check_new_elections()
        else:
            logger.debug("Skipping weekly check (not Monday)")

    @sect_weekly_check.before_loop
    async def before_sect_weekly_check(self):
        await self.bot.wait_until_ready()
        # Wait until next Monday 10:00 GMT+8 before first run
        now = datetime.datetime.now(GMT8_TZ)
        next_monday = now + datetime.timedelta(days=(7 - now.weekday()) % 7 or 7)
        next_monday = next_monday.replace(hour=10, minute=0, second=0, microsecond=0)
        wait_seconds = (next_monday - now).total_seconds()
        logger.info(f"Sect weekly check will run in {wait_seconds / 3600:.1f} hours (next Monday 10:00 GMT+8)")
        await asyncio.sleep(wait_seconds)

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


    # ── History UI ────────────────────────────────────────────────────

    class SessionJumpModal(discord.ui.Modal, title="Jump to Session"):
        """Modal to jump to a specific session number."""
        session_input = discord.ui.TextInput(
            label="Session Number",
            placeholder="Enter a session number (e.g. 5)",
            min_length=1,
            max_length=4,
        )

        async def on_submit(self, modal_interaction: discord.Interaction):
            try:
                session_num = int(self.session_input.value)
            except ValueError:
                await modal_interaction.response.send_message("❌ Please enter a valid number.", ephemeral=True)
                return

            # Forward to the parent view's session display
            view = self.view
            if view and hasattr(view, '_show_session'):
                await view._show_session(modal_interaction, session_num)


    class SessionSelect(discord.ui.Select):
        """Dropdown to pick a session from the current page."""
        def __init__(self, parent_view, options: List[discord.SelectOption]):
            self._parent_view = parent_view
            super().__init__(
                placeholder="📋 Select a session to view...",
                options=options,
                min_values=1,
                max_values=1,
                row=1,
            )

        async def callback(self, interaction: discord.Interaction):
            session_num = int(self.values[0])
            await self._parent_view._show_session(interaction, session_num)


    class SectHistoryView(discord.ui.View):
        """Paginated view for sect election history index + session details."""

        def __init__(self, cog: 'SectCog', school_id: int, author_id: int):
            super().__init__(timeout=120)
            self.cog = cog
            self.school_id = school_id
            self.author_id = author_id
            self.label = SCHOOL_NAMES.get(school_id, 'Unknown')
            self.emoji = SCHOOL_EMOTES.get(school_id, '')

            # Load all sessions from DB
            self.sessions: List[Tuple[int, int]] = []  # (session_number, timestamp)
            self.current_page = 0
            self.page_size = 10
            self.total_sessions = 0
            self._loaded = False
            self._message: Optional[discord.Message] = None

            # Remove all default items first
            self.clear_items()
            # Placeholder items will be added once _load is done
            self._back_to_index = self._make_back_button()

        async def _load(self):
            """Load session list from database."""
            if self._loaded:
                return
            self._loaded = True
            async with aiosqlite.connect(self.cog.db_path) as db:
                cursor = await db.execute(
                    "SELECT session_number, timestamp FROM sect_election_history WHERE school_id = ? ORDER BY session_number ASC",
                    (self.school_id,)
                )
                rows = await cursor.fetchall()
            self.sessions = [(int(r[0]), int(r[1])) for r in rows]
            self.total_sessions = len(self.sessions)

        def _make_back_button(self) -> discord.ui.Button:
            btn = discord.ui.Button(
                label="⬅ Back to Index",
                style=discord.ButtonStyle.secondary,
                custom_id="back_to_index",
                row=0,
            )
            async def back_cb(interaction: discord.Interaction):
                await self._show_index(interaction)
            btn.callback = back_cb
            return btn

        async def _show_index(self, interaction: Optional[discord.Interaction] = None):
            """Show the paginated session index."""
            await self._load()
            self.clear_items()

            if self.total_sessions == 0:
                embed = discord.Embed(
                    title=f"{self.emoji} {self.label} — Election History",
                    description="📭 No election history found.",
                    color=PURPLE
                )
                if interaction:
                    await interaction.response.edit_message(embed=embed, view=self)
                return

            total_pages = max(1, (self.total_sessions + self.page_size - 1) // self.page_size)

            # Clamp page
            if self.current_page >= total_pages:
                self.current_page = total_pages - 1
            if self.current_page < 0:
                self.current_page = 0

            start = self.current_page * self.page_size
            end = min(start + self.page_size, self.total_sessions)
            page_sessions = self.sessions[start:end]

            embed = discord.Embed(
                title=f"{self.emoji} {self.label} — Election History Index",
                description=f"**{self.total_sessions}** past election sessions • Page **{self.current_page + 1}/{total_pages}**",
                color=PURPLE
            )

            for sess_num, ts in page_sessions:
                embed.add_field(
                    name=f"Session {sess_num}",
                    value=f"<t:{ts}:D> <t:{ts}:R>",
                    inline=True
                )

            embed.set_footer(text="Use the dropdown or modal to view a session")

            # Build nav buttons
            prev_btn = discord.ui.Button(
                label="◀ Previous",
                style=discord.ButtonStyle.primary,
                disabled=(self.current_page == 0),
                row=0,
            )
            async def prev_cb(interaction: discord.Interaction):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
                    return
                self.current_page -= 1
                await self._show_index(interaction)
            prev_btn.callback = prev_cb

            next_btn = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.primary,
                disabled=(self.current_page >= total_pages - 1),
                row=0,
            )
            async def next_cb(interaction: discord.Interaction):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
                    return
                self.current_page += 1
                await self._show_index(interaction)
            next_btn.callback = next_cb

            # Jump modal button
            jump_btn = discord.ui.Button(
                label="🔢 Jump to Session",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            async def jump_cb(interaction: discord.Interaction):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
                    return
                modal = self.cog.SessionJumpModal()
                modal.view = self
                await interaction.response.send_modal(modal)
            jump_btn.callback = jump_cb

            # Select menu for sessions on current page
            select_options = []
            for sess_num, ts in page_sessions:
                from datetime import datetime
                dt = datetime.fromtimestamp(ts)
                date_str = dt.strftime("%Y-%m-%d")
                select_options.append(
                    discord.SelectOption(
                        label=f"Session {sess_num}",
                        description=date_str,
                        value=str(sess_num),
                    )
                )
            session_select = self.cog.SessionSelect(self, select_options)

            self.add_item(prev_btn)
            self.add_item(jump_btn)
            self.add_item(next_btn)
            self.add_item(session_select)

            if interaction:
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                # Initial send
                pass

        async def _show_session(self, interaction: discord.Interaction, session_number: int):
            """Show details for a specific session number by editing the original message."""
            await self._load()

            # Fetch from DB
            async with aiosqlite.connect(self.cog.db_path) as db:
                cursor = await db.execute(
                    "SELECT timestamp, election_data_json FROM sect_election_history WHERE school_id = ? AND session_number = ?",
                    (self.school_id, session_number)
                )
                row = await cursor.fetchone()

            if not row:
                await interaction.response.send_message(
                    f"❌ Session {session_number} not found for {self.label}.",
                    ephemeral=True
                )
                return

            ts_int, election_json = row
            chief_dict = json.loads(election_json)

            # Show a loading indicator by editing the message
            loading_embed = discord.Embed(
                title=f"{self.emoji} {self.label} — Session #{session_number}",
                description="🔄 Fetching player data...",
                color=BLURPLE
            )
            self.clear_items()
            await interaction.response.edit_message(embed=loading_embed, view=self)

            # Resolve player info via bulk API call
            sorted_rankings, pid_to_nickname, pid_to_number_id = await self.cog._resolve_player_info_for_history(chief_dict)

            if not sorted_rankings:
                await interaction.edit_original_response(
                    content=f"⚠️ No ranking data found in session {session_number}.",
                    embed=None,
                    view=self
                )
                return

            # Build embed
            embed = discord.Embed(
                title=f"{self.emoji} {self.label} — Election Session #{session_number}",
                description=f"<t:{ts_int}:F>",
                color=BLURPLE
            )

            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for entry in sorted_rankings[:20]:
                rank = entry['rank']
                pid = entry['pid']
                votes = entry['votes']

                nickname = pid_to_nickname.get(pid, 'Unknown')
                number_id = pid_to_number_id.get(pid, '?')

                if rank <= 3:
                    prefix = medals[rank - 1]
                else:
                    prefix = f"**#{rank}**"

                lines.append(f"{prefix} **{nickname}** (`{number_id}`) — {votes:,} votes")

            embed.description += f"\n\n" + "\n".join(lines)

            if len(sorted_rankings) > 20:
                embed.set_footer(text=f"Showing top 20 of {len(sorted_rankings)} candidates")

            embed.add_field(
                name="📚 Total Sessions",
                value=f"There are **{self.total_sessions}** total election sessions on record.",
                inline=False
            )

            # Build view with back button
            self.clear_items()
            self.add_item(self._back_to_index)

            await interaction.edit_original_response(embed=embed, view=self)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
                return False
            return True

    # ── History Commands ──────────────────────────────────────────────

    @app_commands.command(name="sect-history", description="Browse past election results for a sect")
    @app_commands.describe(sect="Which sect's history to browse")
    @app_commands.choices(sect=[
        app_commands.Choice(name=name, value=str(sid))
        for sid, name in SCHOOL_NAMES.items() if sid != 100
    ])
    async def sect_history(self, interaction: discord.Interaction, sect: str):
        """Open an interactive browser for past election results."""
        try:
            school_id = int(sect)
        except ValueError:
            await interaction.response.send_message("❌ Invalid sect.", ephemeral=True)
            return

        # Check if there's any history
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM sect_election_history WHERE school_id = ?",
                (school_id,)
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0

        if count == 0:
            label = SCHOOL_NAMES.get(school_id, 'Unknown')
            await interaction.response.send_message(
                f"📭 No election history found for {label}. The bot may still be backfilling data on first startup.",
                ephemeral=True
            )
            return

        view = self.SectHistoryView(self, school_id, interaction.user.id)
        await view._load()

        # Build initial index embed
        total_pages = max(1, (view.total_sessions + view.page_size - 1) // view.page_size)
        start = 0
        end = min(view.page_size, view.total_sessions)
        page_sessions = view.sessions[start:end]

        embed = discord.Embed(
            title=f"{view.emoji} {view.label} — Election History Index",
            description=f"**{view.total_sessions}** past election sessions • Page **1/{total_pages}**",
            color=PURPLE
        )

        for sess_num, ts in page_sessions:
            embed.add_field(
                name=f"Session {sess_num}",
                value=f"<t:{ts}:D> <t:{ts}:R>",
                inline=True
            )

        embed.set_footer(text="Use the dropdown or buttons below to explore")

        # Build initial UI
        view.clear_items()

        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.primary,
            disabled=(view.total_sessions <= view.page_size),
            row=0,
        )
        async def next_cb(btn_interaction: discord.Interaction):
            if btn_interaction.user.id != interaction.user.id:
                await btn_interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
                return
            view.current_page = 1
            await view._show_index(btn_interaction)
        next_btn.callback = next_cb

        jump_btn = discord.ui.Button(
            label="🔢 Jump to Session",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        async def jump_cb(btn_interaction: discord.Interaction):
            if btn_interaction.user.id != interaction.user.id:
                await btn_interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
                return
            modal = self.SessionJumpModal()
            modal.view = view
            await btn_interaction.response.send_modal(modal)
        jump_btn.callback = jump_cb

        select_options = []
        for sess_num, ts in page_sessions:
            from datetime import datetime
            dt = datetime.fromtimestamp(ts)
            select_options.append(
                discord.SelectOption(
                    label=f"Session {sess_num}",
                    description=dt.strftime("%Y-%m-%d"),
                    value=str(sess_num),
                )
            )
        session_select = self.SessionSelect(view, select_options)

        if view.total_sessions > view.page_size:
            view.add_item(next_btn)
        view.add_item(jump_btn)
        view.add_item(session_select)

        await interaction.response.send_message(embed=embed, view=view)

        # Store the message for later edits
        view._message = await interaction.original_response()

    # ── Sect Votes Command ──────────────────────────────────────────

    @app_commands.command(name="sect-votes", description="Check remaining votes for players in the market watchlist by sect")
    @app_commands.describe(sect="Which sect to check")
    @app_commands.choices(sect=[
        app_commands.Choice(name=name, value=str(sid))
        for sid, name in SCHOOL_NAMES.items() if sid not in (100, 2, 6)
    ])
    async def sect_votes(self, interaction: discord.Interaction, sect: str):
        """Check remaining votes for all watchlist players of a given sect."""
        try:
            school_id = int(sect)
        except ValueError:
            await interaction.response.send_message("❌ Invalid sect.", ephemeral=True)
            return

        school_name = SCHOOL_NAMES.get(school_id, 'Unknown')
        school_emoji = SCHOOL_EMOTES.get(school_id, '')

        await interaction.response.defer()

        # 1. Fetch all PIDs from market watchlist
        pids = []
        watchlist_entries = []  # (pid, nickname, number_id)
        try:
            async with aiosqlite.connect(MARKET_DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT pid, nickname, number_id FROM market_watchlist ORDER BY added_at DESC"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    pids.append(row[0])
                    watchlist_entries.append((row[0], row[1], row[2]))
        except Exception as e:
            logger.error(f"sect-votes: failed to read market watchlist: {e}")
            await interaction.edit_original_response(content=f"❌ Failed to read market watchlist: `{e}`")
            return

        if not pids:
            await interaction.edit_original_response(content="📋 Market watchlist is empty. No players to check.")
            return

        logger.debug(f"sect-votes: fetching school data for {len(pids)} watchlist players (school_id={school_id})")

        # 2. Bulk-fetch player data with fields ["base", "school"]
        # "base" preset already includes is_online and last_online_ts
        try:
            raw_data = await get_bulk_players_info(pids, fields=["base", "school"])
        except Exception as e:
            logger.error(f"sect-votes: bulk fetch failed: {e}")
            await interaction.edit_original_response(content=f"❌ Failed to fetch player data: `{e}`")
            return

        if not raw_data or 'result' not in raw_data:
            await interaction.edit_original_response(content="❌ No player data returned from API.")
            return

        players_data = raw_data['result']

        # 3. Process each player
        results = []  # list of dicts for display
        for pid, nickname, number_id in watchlist_entries:
            player_entry = players_data.get(pid)
            if not player_entry:
                continue

            base = player_entry.get('base', {})
            player_school_id = base.get('school', 0)

            # Filter by selected school
            if player_school_id != school_id:
                continue

            # Get school status and vote info
            school_data = player_entry.get('school', {})
            if not isinstance(school_data, dict):
                continue

            status = school_data.get('status', '')
            if not status:
                continue

            chief_campaign = school_data.get('chief_campaign', {})
            if not isinstance(chief_campaign, dict):
                continue

            vote_num = chief_campaign.get('vote_num', 0)  # votes already used
            if not isinstance(vote_num, int):
                vote_num = int(vote_num) if vote_num else 0

            # Look up expected votes
            expected = VOTE_COUNTS.get(status, 0)
            remaining = expected - vote_num

            rank_name = SCHOOL_RANKING.get(status, status)

            # Calculate online recency (within last 7 days)
            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            last_online = base.get('logout_time', 0) or 0
            is_online = base.get('is_online', 0) == 1
            online_recent = (now_ts - last_online) <= (7 * 24 * 3600)  # within 7 days

            results.append({
                'nickname': nickname or base.get('nickname', 'Unknown'),
                'number_id': number_id or str(base.get('number_id', '')),
                'rank_name': rank_name,
                'vote_num': vote_num,
                'expected': expected,
                'remaining': remaining,
                'is_online': is_online,
                'online_recent': online_recent,
            })

        if not results:
            await interaction.edit_original_response(
                content=f"{school_emoji} No players from **{school_name}** found in the market watchlist."
            )
            return

        # Sort: players with remaining votes first, then by remaining desc
        results.sort(key=lambda x: (-x['remaining'], x['nickname']))
        results_with_remaining = [r for r in results if r['remaining'] > 0]
        results_no_remaining = [r for r in results if r['remaining'] <= 0]

        # Pagination settings
        PAGE_SIZE = 10
        total_pages = max(1, (len(results) + PAGE_SIZE - 1) // PAGE_SIZE)

        # Build single-page view
        view = self.SectVotesView(school_emoji, school_name, results, results_with_remaining, results_no_remaining, PAGE_SIZE)
        await view._show_page(interaction, 1, total_pages)

    class SectVotesView(discord.ui.View):
        """Paginated view for sect vote check results with smart filter toggles."""

        def __init__(self, school_emoji: str, school_name: str, all_results: list, with_remaining: list, no_remaining: list, page_size: int):
            super().__init__(timeout=300)
            self.school_emoji = school_emoji
            self.school_name = school_name
            self.all_results = all_results
            self.with_remaining = with_remaining
            self.no_remaining = no_remaining
            self.page_size = page_size
            self.current_page = 0
            self.total_pages = max(1, (len(all_results) + page_size - 1) // page_size)
            # Smart filter state: set of active filters
            # "online" and "online_7d" are mutually exclusive (only one can be active)
            # "has_remaining" can stack with either time filter
            self.active_filters = set()

        def _apply_filter(self) -> list:
            """Apply all active filters (AND logic, with mutual exclusion for time filters)."""
            if not self.active_filters:
                return self.all_results
            filtered = list(self.all_results)
            # Time filters (mutually exclusive in UI, but both here for safety)
            if "online" in self.active_filters:
                filtered = [r for r in filtered if r.get('is_online')]
            if "online_7d" in self.active_filters:
                filtered = [r for r in filtered if r.get('is_online') or r.get('online_recent')]
            # Independent filter
            if "has_remaining" in self.active_filters:
                filtered = [r for r in filtered if r['remaining'] > 0]
            return filtered

        async def _show_page(self, interaction: discord.Interaction, page: int, total_pages: int, filter_mode: str = None):
            if filter_mode is not None:
                # "online" and "online_7d" are mutually exclusive — clicking one clears the other
                # "has_remaining" can stack with either time filter
                if filter_mode in ("online", "online_7d"):
                    # "online" and "online_7d" are mutually exclusive
                    was_active = filter_mode in self.active_filters
                    # Clear both time filters first
                    self.active_filters.discard("online")
                    self.active_filters.discard("online_7d")
                    # Toggle: if it was active, leave it off; otherwise turn it on
                    if not was_active:
                        self.active_filters.add(filter_mode)
                elif filter_mode == "has_remaining":
                    # Toggle independently
                    if "has_remaining" in self.active_filters:
                        self.active_filters.discard("has_remaining")
                    else:
                        self.active_filters.add("has_remaining")
                elif filter_mode == "all":
                    self.active_filters.clear()

            self.current_page = page
            filtered = self._apply_filter()
            total_pages = max(1, (len(filtered) + self.page_size - 1) // self.page_size)
            if page > total_pages:
                page = total_pages
            if page < 1:
                page = 1
            self.current_page = page

            start = (page - 1) * self.page_size
            end = min(start + self.page_size, len(filtered))
            page_results = filtered[start:end]

            lines = [
                f"# {self.school_emoji} Vote Check — {self.school_name}",
                f"Showing **{start + 1}–{end}** of **{len(filtered)}** players",
            ]
            if self.active_filters:
                filter_labels = {
                    "online": "🟢 Online",
                    "online_7d": "🕐 7 Days",
                    "has_remaining": "🗳️ Remaining",
                }
                active_labels = []
                if "online" in self.active_filters:
                    active_labels.append(filter_labels["online"])
                if "online_7d" in self.active_filters:
                    active_labels.append(filter_labels["online_7d"])
                if "has_remaining" in self.active_filters:
                    active_labels.append(filter_labels["has_remaining"])
                lines.append(f"*Filters: {' + '.join(active_labels)}*")
            lines.append("")

            for r in page_results:
                lines.append(
                    f"• **{r['nickname']}** (`{r['number_id']}`) — {r['rank_name']} — "
                    f"{r['vote_num']}/{r['expected']} used"
                )

            embed = discord.Embed(
                title=f"{self.school_emoji} {self.school_name} — Vote Check",
                color=PURPLE,
                description="\n".join(lines)
            )
            embed.set_footer(text=f"Page {page}/{total_pages} • {len(filtered)} total players")

            # Build buttons
            self.clear_items()

            # Filter toggles (row 0)
            all_btn = discord.ui.Button(
                label="All",
                style=discord.ButtonStyle.primary if not self.active_filters else discord.ButtonStyle.secondary,
                row=0,
            )
            async def all_cb(i: discord.Interaction):
                if i.user.id != interaction.user.id:
                    await i.response.send_message("❌ Not your menu.", ephemeral=False)
                    return
                await i.response.defer()
                await self._show_page(i, 1, total_pages, filter_mode="all")
            all_btn.callback = all_cb
            self.add_item(all_btn)

            online_btn = discord.ui.Button(
                label="🟢 Online",
                style=discord.ButtonStyle.primary if "online" in self.active_filters else discord.ButtonStyle.secondary,
                row=0,
            )
            async def online_cb(i: discord.Interaction):
                if i.user.id != interaction.user.id:
                    await i.response.send_message("❌ Not your menu.", ephemeral=False)
                    return
                await i.response.defer()
                await self._show_page(i, 1, total_pages, filter_mode="online")
            online_btn.callback = online_cb
            self.add_item(online_btn)

            online_7d_btn = discord.ui.Button(
                label="🕐 7 Days",
                style=discord.ButtonStyle.primary if "online_7d" in self.active_filters else discord.ButtonStyle.secondary,
                row=0,
            )
            async def online_7d_cb(i: discord.Interaction):
                if i.user.id != interaction.user.id:
                    await i.response.send_message("❌ Not your menu.", ephemeral=False)
                    return
                await i.response.defer()
                await self._show_page(i, 1, total_pages, filter_mode="online_7d")
            online_7d_btn.callback = online_7d_cb
            self.add_item(online_7d_btn)

            has_remaining_btn = discord.ui.Button(
                label="🗳️ Remaining",
                style=discord.ButtonStyle.primary if "has_remaining" in self.active_filters else discord.ButtonStyle.secondary,
                row=0,
            )
            async def has_remaining_cb(i: discord.Interaction):
                if i.user.id != interaction.user.id:
                    await i.response.send_message("❌ Not your menu.", ephemeral=False)
                    return
                await i.response.defer()
                await self._show_page(i, 1, total_pages, filter_mode="has_remaining")
            has_remaining_btn.callback = has_remaining_cb
            self.add_item(has_remaining_btn)

            # Pagination row
            prev_btn = discord.ui.Button(
                label="◀ Previous",
                style=discord.ButtonStyle.secondary,
                disabled=(page <= 1),
                row=1,
            )
            async def prev_cb(i: discord.Interaction):
                if i.user.id != interaction.user.id:
                    await i.response.send_message("❌ Not your menu.", ephemeral=False)
                    return
                await i.response.defer()
                await self._show_page(i, max(1, page - 1), total_pages)
            prev_btn.callback = prev_cb

            page_btn = discord.ui.Button(
                label=f"{page}/{total_pages}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1,
            )

            next_btn = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(page >= total_pages),
                row=1,
            )
            async def next_cb(i: discord.Interaction):
                if i.user.id != interaction.user.id:
                    await i.response.send_message("❌ Not your menu.", ephemeral=False)
                    return
                await i.response.defer()
                await self._show_page(i, min(total_pages, page + 1), total_pages)
            next_btn.callback = next_cb

            self.add_item(prev_btn)
            self.add_item(page_btn)
            self.add_item(next_btn)

            # Always edit the original response (interaction is already deferred from command/button)
            await interaction.edit_original_response(embed=embed, view=self)


async def setup(bot: commands.Bot):
    await bot.add_cog(SectCog(bot))
