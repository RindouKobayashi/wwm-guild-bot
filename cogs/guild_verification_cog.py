import discord
import settings
import aiosqlite
import random
import time
from discord.ext import commands
from discord import app_commands, ButtonStyle
from settings import logger, BASE_DIR, WWM_UID, WWM_TOKEN, WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, CLUB_ID
from datetime import datetime, timezone
from discord.ext import tasks
from utility.wwm import get_player_info, get_club_hostnums
from utility.api_constants import SCHOOL_ROLES
from cogs.wwm_cog import _compute_energy, MAX_ENERGY, ENERGY_REGEN_SECONDS

# ---------------------------------------------------------------------------
# Energy regen alerts (opt-in)
# ---------------------------------------------------------------------------
# A subscriber is alerted when their energy rises past `threshold` while
# offline. Three guards stop the 1-minute poll from pinging every minute:
#
#   * edge-triggered — we alert on the CROSSING, not on "energy > threshold",
#     and remember the crossing in `was_above` so it survives restarts;
#   * hysteresis — the user must fall back below `threshold - REARM_MARGIN`
#     before another crossing counts, so hovering on the line cannot flap;
#   * cooldown — a hard floor between any two alerts.
REARM_MARGIN = 30        # must drop this far below the threshold to re-arm
ALERT_COOLDOWN = 3600    # seconds between any two alerts (1 hour)
# How long a cached reading may still be extrapolated. Energy only ever rises
# while a player is offline, and the extrapolation saturates at MAX_ENERGY, so
# a generous window is safe: anyone offline longer than ~80h is full regardless
# of the cached base. It must comfortably exceed the 36h needed to regen from
# an empty bar past a typical threshold, or a player who logged off at 180 with
# a 450 threshold could never be alerted.
CACHE_MAX_AGE = 604800   # 7 days

OWNER_DENY_MSG = "❌ Only the user who ran this command can use these buttons."


async def ensure_owner(interaction: discord.Interaction, view) -> bool:
    """Return True if the interacting user is the command invoker.

    Otherwise send an ephemeral denial and return False.
    """
    if getattr(view, "owner_id", None) is None or interaction.user.id == view.owner_id:
        return True
    if interaction.response.is_done():
        await interaction.followup.send(OWNER_DENY_MSG, ephemeral=True)
    else:
        await interaction.response.send_message(OWNER_DENY_MSG, ephemeral=True)
    return False


def extrapolate_energy(cached_energy: int, cached_ts: int, now_ts: int) -> tuple:
    """
    Project a cached energy reading forward using offline regen.

    The live API drops ``gameplay_resources`` once a player has been gone long
    enough, but the 1-minute poll witnesses their energy on the way out. So we
    bank the last real reading and continue it here at +1 per 8 minutes.

    Returns ``(energy, extrapolated)``. ``energy`` is None when the cache is
    too old to be worth anything — better to say nothing than to guess.
    """
    if not cached_ts or not cached_energy:
        return None, False
    age = now_ts - cached_ts
    if age < 0 or age > CACHE_MAX_AGE:
        return None, False
    gained = age // ENERGY_REGEN_SECONDS
    # Energy caps at MAX_ENERGY — never extrapolate past the real ceiling.
    return min(MAX_ENERGY, int(cached_energy) + gained), True


def should_alert(energy: int, threshold: int, was_above: bool, last_alert_ts, now_ts: int) -> tuple:
    """
    Decide whether this poll should alert, and the ``was_above`` value to store.

    Returns ``(alert, new_was_above)``. Alerting is edge-triggered: we only
    fire on the upward crossing, so a user who stays above their threshold is
    pinged exactly once no matter how many polls run.
    """
    # Below the re-arm line (threshold - margin): the user spent energy, so
    # arm the next crossing. This also covers "below threshold at all".
    if energy <= threshold - REARM_MARGIN:
        return False, False
    if was_above:
        return False, True  # already alerted for this climb — stay quiet
    if last_alert_ts and (now_ts - last_alert_ts) < ALERT_COOLDOWN:
        return False, False  # too soon; re-arm so the next real dip can fire
    return True, True


def get_player_school(player_data: dict) -> int:
    """Extract the school/sect ID from player data. Returns 100 (Sectless) if not found."""
    return player_data.get('base', {}).get('school', 100)


async def assign_school_role(member: discord.Member, player_data: dict):
    """Assign the appropriate school role based on player's in-game sect.
    Only updates if the sect has changed — skips unnecessary role churn.
    Safely skips if SCHOOL_ROLES is empty/not configured for this branch.
    """
    if not SCHOOL_ROLES:
        return None
    
    school_id = get_player_school(player_data)
    role_id = SCHOOL_ROLES.get(school_id)
    
    # Check if they already have the correct role equipped
    correct_role = member.guild.get_role(role_id) if role_id else None
    if correct_role and correct_role in member.roles:
        # Already has the right school role — no change needed
        return correct_role
    
    # Remove any existing school roles that are now incorrect
    existing_school_roles = [r for r in member.roles if r.id in SCHOOL_ROLES.values()]
    if existing_school_roles:
        await member.remove_roles(*existing_school_roles, reason="Updating school/sect role")
    
    if correct_role:
        await member.add_roles(correct_role, reason=f"Player school/sect ID: {school_id}")
        return correct_role
    return None


DB_PATH = BASE_DIR / "data" / "guild_verification.db"

class GuildVerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.init_database()
        self.load_config()
        self.guild_member_sync_task.start()

    async def init_database(self):
        """Initialize database tables"""
        async with aiosqlite.connect(DB_PATH) as conn:
            # Configuration table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS verification_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            
            # Verification requests history
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS verification_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    character_uid TEXT NOT NULL,
                    status TEXT NOT NULL,
                    admin_id INTEGER,
                    reason TEXT,
                    message_id INTEGER,
                    created_at TIMESTAMP NOT NULL,
                    processed_at TIMESTAMP,
                    verification_code TEXT
                )
            ''')
            
            # Add verification_code column if table already exists
            try:
                await conn.execute("ALTER TABLE verification_requests ADD COLUMN verification_code TEXT")
            except:
                pass
            
            # Opt-in energy regen alerts. Opt-in means this table stays empty
            # until a user runs /energy-alert, so nobody is notified by default.
            # last_energy/last_seen_ts bank the last reading the API gave us:
            # once a player has been offline long enough the live service drops
            # gameplay_resources, and this cache is what keeps their alert working.
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS energy_alerts (
                    user_id INTEGER PRIMARY KEY,
                    threshold INTEGER NOT NULL,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    last_energy INTEGER NOT NULL DEFAULT 0,
                    last_seen_ts INTEGER NOT NULL DEFAULT 0,
                    was_above INTEGER NOT NULL DEFAULT 0,
                    last_alert_ts INTEGER,
                    updated_at TIMESTAMP NOT NULL
                )
            ''')

            # Migration: add cache columns if the table predates them
            for col, ddl in [
                ("last_energy", "ALTER TABLE energy_alerts ADD COLUMN last_energy INTEGER NOT NULL DEFAULT 0"),
                ("last_seen_ts", "ALTER TABLE energy_alerts ADD COLUMN last_seen_ts INTEGER NOT NULL DEFAULT 0"),
                ("was_above", "ALTER TABLE energy_alerts ADD COLUMN was_above INTEGER NOT NULL DEFAULT 0"),
                ("last_alert_ts", "ALTER TABLE energy_alerts ADD COLUMN last_alert_ts INTEGER"),
            ]:
                try:
                    await conn.execute(ddl)
                    await conn.commit()
                    logger.info(f"✅ Added {col} column to energy_alerts table")
                except Exception:
                    pass

            # Approved members registry
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS verified_members (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    character_uid TEXT NOT NULL,
                    player_pid TEXT,
                    verified_at TIMESTAMP NOT NULL,
                    verified_by INTEGER NOT NULL
                )
            ''')
            
            await conn.commit()
            
            # Migration: add player_pid column if it doesn't exist
            try:
                await conn.execute("ALTER TABLE verified_members ADD COLUMN player_pid TEXT")
                await conn.commit()
                logger.info("✅ Added player_pid column to verified_members table")
            except:
                pass
            
            # Migration: resolve existing records without player_pid
            cursor = await conn.execute("SELECT rowid, user_id, character_uid FROM verified_members WHERE player_pid IS NULL")
            rows_to_migrate = await cursor.fetchall()
            if rows_to_migrate:
                logger.info(f"⚙️ Migrating {len(rows_to_migrate)} existing verified member records to resolve PIDs...")
                for row in rows_to_migrate:
                    rowid, user_id, character_uid = row
                    try:
                        player_data = await get_player_info(character_uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
                        if player_data and 'result' in player_data:
                            player = player_data['result']
                            pid = player.get('id')
                            if pid:
                                await conn.execute("UPDATE verified_members SET player_pid = ? WHERE rowid = ?", (str(pid), rowid))
                                logger.debug(f"  ✅ Resolved {character_uid} -> PID {pid}")
                            else:
                                logger.warning(f"  ❌ Could not resolve PID for {character_uid} (user_id: {user_id})")
                        else:
                            logger.warning(f"  ❌ Failed to fetch player data for {character_uid} (user_id: {user_id})")
                    except Exception as e:
                        logger.error(f"  ❌ Error migrating {character_uid} (user_id: {user_id}): {e}")
                await conn.commit()
                logger.info(f"✅ Migration complete: {len(rows_to_migrate)} records processed")
        
        logger.debug("Guild Verification database initialized")
    
    def load_config(self):
        """Load configuration from database into runtime"""
        import asyncio
        
        async def _load():
            async with aiosqlite.connect(DB_PATH) as conn:
                cursor = await conn.execute("SELECT key, value FROM verification_config")
                rows = await cursor.fetchall()
                config = dict(rows)
            
            for key, value in config.items():
                if value.isdigit():
                    setattr(settings, key, int(value))
                else:
                    setattr(settings, key, value)
            
            logger.debug(f"Loaded {len(config)} configuration entries from database")
        
        asyncio.get_event_loop().create_task(_load())

        
    async def cog_unload(self):
        if self.guild_member_sync_task.is_running():
            self.guild_member_sync_task.cancel()

    @tasks.loop(minutes=1)
    async def guild_member_sync_task(self):
        """Background task to sync verified members guild membership status every minute"""
        
        if not hasattr(settings, 'GUILD_MEMBER_ROLE_ID') or not hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID') or not hasattr(settings, 'DISCORD_SERVER_ID'):
            return
            
        try:
            # Get all verified members from database (include player_pid)
            async with aiosqlite.connect(DB_PATH) as conn:
                cursor = await conn.execute("SELECT user_id, character_uid, player_pid FROM verified_members")
                verified_members = await cursor.fetchall()
            
            if not verified_members:
                return
                
            logger.debug(f"Running guild membership sync for {len(verified_members)} verified members")
            
            all_pids = []
            pid_to_userid_map = {}
            
            for user_id, character_uid, player_pid in verified_members:
                if player_pid:
                    all_pids.append(player_pid)
                    pid_to_userid_map[player_pid] = user_id
                else:
                    logger.warning(f"Skipping member {user_id} (UID: {character_uid}) - no player_pid resolved yet")
                
            if not all_pids:
                logger.warning("No resolved player PIDs available for membership sync")
                return
                
            from utility.wwm import get_bulk_players_info
            # gameplay_resources is added for the energy regen alerts — same
            # single API call, two consumers.
            bulk_data = await get_bulk_players_info(all_pids, fields=["club", "base", "gameplay_resources"])
            
            if not bulk_data or bulk_data.get('code') != 0:
                logger.warning("Failed to get bulk player data for membership sync")
                return
                
            players = bulk_data.get('result', {})
            
            guild = self.bot.get_guild(settings.DISCORD_SERVER_ID)
            if not guild:
                logger.warning(f"Could not find guild with ID {settings.DISCORD_SERVER_ID} for membership sync")
                return
            
            guild_role = guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
            community_role = guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
                
            if not guild_role or not community_role:
                logger.warning("Guild or community role not found for membership sync")
                return
                
            for pid, player_data in players.items():
                if pid not in pid_to_userid_map:
                    continue
                    
                user_id = pid_to_userid_map[pid]
                
                club_data = player_data.get('club', {})
                club_id = club_data.get('club_id')
                is_current_guild_member = (club_id == CLUB_ID)
                
                member = guild.get_member(user_id)
                
                if not member:
                    continue
                    
                has_guild_role = guild_role in member.roles
                has_community_role = community_role in member.roles
                
                if is_current_guild_member:
                    if not has_guild_role:
                        await member.add_roles(guild_role)
                        logger.info(f"Added guild role to {member} - joined guild")
                    if has_community_role:
                        await member.remove_roles(community_role)
                        logger.info(f"Removed community role from {member} - joined guild")
                else:
                    if has_guild_role:
                        await member.remove_roles(guild_role)
                        logger.info(f"Removed guild role from {member} - left guild")
                    if not has_community_role:
                        await member.add_roles(community_role)
                        logger.info(f"Added community role to {member} - left guild")
                
                # Sync school/sect role based on player's in-game sect
                await assign_school_role(member, player_data)

            # Opt-in energy regen alerts, evaluated on the payload already
            # fetched above — no extra API call. Failures must never interrupt
            # role sync, hence the guard.
            try:
                await self._check_energy_alerts(players, pid_to_userid_map)
            except Exception as alert_err:
                logger.error(f"Energy alert check failed: {alert_err}", exc_info=True)

        except Exception as e:
            logger.error(f"Guild member sync task failed: {str(e)}", exc_info=True)

    async def _check_energy_alerts(self, players: dict, pid_to_userid_map: dict):
        """
        Alert subscribers whose offline energy has risen past their threshold.

        Runs on the 1-minute sync loop using the same bulk payload, so this
        costs no extra API call. Returns quietly when nobody has opted in.
        """
        now_ts = int(time.time())

        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT user_id, threshold, last_energy, last_seen_ts, was_above, last_alert_ts "
                "FROM energy_alerts WHERE is_enabled = 1"
            )
            subscribers = await cursor.fetchall()

        if not subscribers:
            return

        userid_to_pid = {user_id: pid for pid, user_id in pid_to_userid_map.items()}
        guild = self.bot.get_guild(settings.DISCORD_SERVER_ID)

        # Collect state changes first, write once at the end.
        updates = []
        for user_id, threshold, cached_energy, cached_ts, was_above, last_alert_ts in subscribers:
            pid = userid_to_pid.get(user_id)
            player_data = players.get(pid) if pid else None
            if not player_data:
                continue

            base = player_data.get('base', {}) or {}
            resources = player_data.get('gameplay_resources', {}) or {}
            is_online = base.get('is_online', 0) == 1

            energy, has_data, _regen = _compute_energy(resources, base, is_online)
            extrapolated = False

            if not has_data:
                # Live service dropped the reading (offline too long) — continue
                # from our own last sighting instead of going blind.
                energy, extrapolated = extrapolate_energy(cached_energy, cached_ts, now_ts)
                if energy is None:
                    logger.debug(f"Energy alert: no live or cached energy for {user_id}, skipping")
                    continue
            else:
                # Fresh reading — bank it so we survive the data disappearing.
                cached_energy, cached_ts = energy, now_ts

            alert, new_was_above = should_alert(
                energy, threshold, bool(was_above), last_alert_ts, now_ts
            )
            new_last_alert = now_ts if alert else last_alert_ts
            updates.append((user_id, cached_energy, cached_ts, int(new_was_above), new_last_alert))

            if not alert:
                continue

            member = guild.get_member(user_id) if guild else None
            if member is None:
                logger.debug(f"Energy alert: member {user_id} not in guild, skipping send")
                continue

            await self._send_energy_alert(member, energy, threshold, extrapolated)
            logger.info(
                f"⚡ Energy alert sent to {member} ({energy}/{MAX_ENERGY} >= threshold "
                f"{threshold}{', extrapolated' if extrapolated else ''})"
            )

        if updates:
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.executemany(
                    "UPDATE energy_alerts SET last_energy = ?, last_seen_ts = ?, "
                    "was_above = ?, last_alert_ts = ? WHERE user_id = ?",
                    [(e, ts, wa, lat, uid) for uid, e, ts, wa, lat in updates],
                )
                await conn.commit()

    async def _send_energy_alert(
        self, member: discord.Member, energy: int, threshold: int,
        extrapolated: bool, is_test: bool = False,
    ):
        """DM the alert, falling back to the branch's alert channel if DMs fail."""
        minutes_to_full = max(0, (MAX_ENERGY - energy) * 8)

        embed = discord.Embed(
            title="🧪 Energy Alert Sample" if is_test else "⚡ Energy Recharged",
            description=(
                (
                    f"**This is a sample** — your alert is working.\n"
                    f"Your energy is **{energy:,} / {MAX_ENERGY}** right now."
                    if is_test
                    else f"Your energy has risen to **{energy:,} / {MAX_ENERGY}**, "
                         f"past your alert threshold of **{threshold:,}**."
                )
            ),
            color=discord.Color.blurple() if is_test else discord.Color.green(),
        )
        embed.add_field(name="Energy", value=f"**{energy:,} / {MAX_ENERGY}**", inline=True)
        embed.add_field(name="Alert threshold", value=f"**{threshold:,}**", inline=True)
        if energy >= MAX_ENERGY:
            embed.add_field(name="Status", value="**Full**", inline=True)
        else:
            embed.add_field(
                name="Estimated time to full",
                value=f"~{minutes_to_full // 60}h {minutes_to_full % 60}m",
                inline=True,
            )
        embed.set_footer(
            text="Sample only — no action was taken. Manage this alert with /energy-alert"
            if is_test
            else ("Estimated from your last known reading (live data unavailable)."
                  if extrapolated
                  else "Change or disable this alert with /energy-alert")
        )

        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            # DMs are closed — fall back to this branch's alert channel.
            channel = self.bot.get_channel(getattr(settings, 'ENERGY_ALERT_CHANNEL_ID', 0))
            if channel:
                await channel.send(
                    content=f"{member.mention} *(couldn't DM — sending here)*",
                    embed=embed,
                )
            else:
                logger.warning(
                    f"Energy alert: DMs closed for {member} and no ENERGY_ALERT_CHANNEL_ID"
                )
        except discord.HTTPException as dm_err:
            logger.error(f"Energy alert DM failed for {member}: {dm_err}")

    
    @guild_member_sync_task.before_loop
    async def before_sync_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="energy-alert", description="Get a DM when your energy regens past a threshold you choose")
    async def energy_alert(self, interaction: discord.Interaction):
        """Opt in to (or out of) an energy regen alert. Configure it with the buttons below."""
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM verified_members WHERE user_id = ?", (interaction.user.id,)
            )
            if await cursor.fetchone() is None:
                await interaction.response.send_message(
                    "❌ You need to bind your game account first.\n"
                    "Use the account binding system, then run this command again.",
                    ephemeral=True,
                )
                return

            cursor = await conn.execute(
                "SELECT threshold, is_enabled, last_energy, last_seen_ts, was_above, last_alert_ts "
                "FROM energy_alerts WHERE user_id = ?", (interaction.user.id,)
            )
            row = await cursor.fetchone()

        # Defer before the live energy fetch — it can take a moment.
        await interaction.response.defer(ephemeral=True)
        energy, _extrapolated = await self.fetch_my_energy(interaction.user.id)

        view = EnergyAlertView(
            cog=self,
            owner_id=interaction.user.id,
            member=interaction.user,
            threshold=row[0] if row else None,
            is_enabled=bool(row[1]) if row else False,
            last_alert_ts=row[5] if row else None,
        )
        view.current_energy = energy
        view._rebuild()
        await interaction.followup.send(embed=view.embed, view=view, ephemeral=True)

    async def fetch_my_energy(self, user_id: int) -> tuple:
        """
        Resolve the caller's CURRENT energy for display purposes.

        Returns ``(energy, extrapolated)``; energy is None when neither a live
        nor a usable cached reading exists. Uses the same live-then-cache logic
        as the background poll, so the panel and a real alert always agree.
        """
        now_ts = int(time.time())

        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT player_pid FROM verified_members WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            pid = row[0] if row else None

            cursor = await conn.execute(
                "SELECT last_energy, last_seen_ts FROM energy_alerts WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        cached_energy, cached_ts = (row[0], row[1]) if row else (0, 0)

        if pid:
            try:
                from utility.wwm import get_bulk_players_info
                bulk = await get_bulk_players_info([pid], fields=["base", "gameplay_resources"])
                if bulk and bulk.get('code') == 0:
                    player_data = bulk.get('result', {}).get(pid)
                    if player_data:
                        base = player_data.get('base', {}) or {}
                        energy, has_data, _ = _compute_energy(
                            player_data.get('gameplay_resources', {}) or {},
                            base,
                            base.get('is_online', 0) == 1,
                        )
                        if has_data:
                            return energy, False
            except Exception as fetch_err:
                logger.warning(f"Energy panel: live fetch failed for {user_id}: {fetch_err}")

        # No live reading (or the fetch failed) — fall back to our cache.
        energy, extrapolated = extrapolate_energy(cached_energy, cached_ts, now_ts)
        return energy, extrapolated

    async def get_energy_alert_row(self, user_id: int):
        """Return the caller's energy_alerts row, or None."""
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT threshold, is_enabled, last_energy, last_seen_ts, was_above, last_alert_ts "
                "FROM energy_alerts WHERE user_id = ?", (user_id,)
            )
            return await cursor.fetchone()

    async def save_energy_alert(self, user_id: int, threshold: int, is_enabled: bool = True):
        """Upsert a subscriber. Resets was_above so the next crossing alerts."""
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "REPLACE INTO energy_alerts "
                "(user_id, threshold, is_enabled, last_energy, last_seen_ts, was_above, last_alert_ts, updated_at) "
                "VALUES (?, ?, ?, 0, 0, 0, NULL, ?)",
                (user_id, threshold, int(is_enabled), datetime.utcnow()),
            )
            await conn.commit()

    async def set_energy_alert_enabled(self, user_id: int, enabled: bool):
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE energy_alerts SET is_enabled = ?, updated_at = ? WHERE user_id = ?",
                (int(enabled), datetime.utcnow(), user_id),
            )
            await conn.commit()

    async def reset_energy_alert_tracking(self, user_id: int):
        """Clear was_above so the very next crossing alerts immediately."""
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE energy_alerts SET was_above = 0, updated_at = ? WHERE user_id = ?",
                (datetime.utcnow(), user_id),
            )
            await conn.commit()

    async def remove_energy_alert(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM energy_alerts WHERE user_id = ?", (user_id,))
            await conn.commit()

    @app_commands.command(name="lookup-member", description="Lookup a verified guild member by user or character UID")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        member="Lookup by Discord member (optional)",
        character_uid="Lookup by Character UID (optional)"
    )
    async def lookup_member(self, interaction: discord.Interaction, member: discord.Member = None, character_uid: str = None):
        """Admin command to lookup verified members"""
        
        if not member and not character_uid:
            await interaction.response.send_message(
                "❌ Please provide either a member or character UID to lookup.",
                ephemeral=True
            )
            return
        
        async with aiosqlite.connect(DB_PATH) as conn:
            result = None
            if member:
                cursor = await conn.execute("SELECT user_id, username, character_uid, verified_at, verified_by FROM verified_members WHERE user_id = ?", (member.id,))
                result = await cursor.fetchone()
            
            if character_uid and not result:
                cursor = await conn.execute("SELECT user_id, username, character_uid, verified_at, verified_by FROM verified_members WHERE character_uid = ?", (character_uid.strip(),))
                result = await cursor.fetchone()
        
        if not result:
            embed = discord.Embed(
                title="❌ Member Not Found",
                description="This member is not in the verified members database.",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="✅ Verified Member Found",
                color=discord.Color.green()
            )
            
            embed.add_field(name="Discord User", value=f"<@{result[0]}>\n`{result[1]}`", inline=True)
            embed.add_field(name="Character UID", value=f"`{result[2]}`", inline=True)
            from datetime import timezone
            verified_dt = datetime.fromisoformat(result[3]).replace(tzinfo=timezone.utc)
            verified_timestamp = int(verified_dt.timestamp())
            embed.add_field(name="Verified At", value=f"<t:{verified_timestamp}>", inline=False)
            embed.add_field(name="Verified By", value=f"<@{result[4]}>", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"Member lookup performed by {interaction.user}")

    @app_commands.command(name="list-bound-accounts", description="List all verified and bound accounts in the database")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        show_values="Show live character values and stats (default: False)",
        show_duplicates="Show duplicate character bindings (default: True)"
    )
    async def list_bound_accounts(self, interaction: discord.Interaction, show_values: bool = False, show_duplicates: bool = True):
        """Admin command to list all bound/verified accounts and detect duplicates"""
        
        await interaction.response.defer(ephemeral=False)
        
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM verified_members")
            total_count_row = await cursor.fetchone()
            total_count = total_count_row[0]
            
            cursor = await conn.execute("SELECT user_id, username, character_uid, player_pid, verified_at FROM verified_members ORDER BY verified_at DESC")
            all_members = await cursor.fetchall()
        
        if total_count == 0:
            embed = discord.Embed(
                title="📋 Bound Accounts List",
                description="No bound accounts found in the database.",
                color=discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed, ephemeral=False)
            return
        
        # Check for duplicates if requested
        from collections import defaultdict
        uid_groups = defaultdict(list)
        pid_groups = defaultdict(list)
        
        for row in all_members:
            user_id, username, character_uid, player_pid, verified_at = row
            uid_groups[character_uid].append((user_id, username, verified_at))
            if player_pid:
                pid_groups[player_pid].append((user_id, username, character_uid, verified_at))
        
        uid_duplicates = {uid: users for uid, users in uid_groups.items() if len(users) > 1}
        pid_duplicates = {pid: users for pid, users in pid_groups.items() if len(users) > 1}
        
        # Build duplicate report embed if duplicates found
        if show_duplicates and (uid_duplicates or pid_duplicates):
            total_duplicate_users = set()
            for users in uid_duplicates.values():
                for user_id, username, verified_at in users:
                    total_duplicate_users.add(user_id)
            for users in pid_duplicates.values():
                for user_id, username, character_uid, verified_at in users:
                    total_duplicate_users.add(user_id)
            
            dup_embed = discord.Embed(
                title="⚠️ Duplicate Binding Detection",
                description=f"Found **{len(total_duplicate_users)}** user(s) with duplicate character bindings",
                color=discord.Color.orange()
            )
            
            if uid_duplicates:
                dup_embed.add_field(
                    name=f"🔁 Duplicate Character UIDs ({len(uid_duplicates)} found)",
                    value="\n".join([
                        f"**`{uid}`** — bound by {len(users)} Discord user(s):\n" +
                    "\n".join([f"  • <@{u_id}> (`{uname}`) — bound <t:{int(datetime.fromisoformat(v_at).replace(tzinfo=timezone.utc).timestamp())}:D>"
                                   for u_id, uname, v_at in sorted(users, key=lambda x: x[2])[:3]])
                        for uid, users in sorted(uid_duplicates.items(), key=lambda x: len(x[1]), reverse=True)[:10]
                    ])[:4000] or "None",
                    inline=False
                )
            
            if pid_duplicates:
                dup_embed.add_field(
                    name=f"🔁 Duplicate Player PIDs ({len(pid_duplicates)} found)",
                    value="\n".join([
                        f"**PID `{pid}`** — bound by {len(users)} Discord user(s):\n" +
                        "\n".join([f"  • <@{u_id}> (`{uname}`) — UID `{c_uid}`"
                                   for u_id, uname, c_uid, v_at in sorted(users, key=lambda x: x[3])[:3]])
                        for pid, users in sorted(pid_duplicates.items(), key=lambda x: len(x[1]), reverse=True)[:10]
                    ])[:4000] or "None",
                    inline=False
                )
            
            dup_embed.set_footer(text="Duplicate bindings may indicate account sharing or unauthorized binding")
            await interaction.followup.send(embed=dup_embed, ephemeral=False)
        
        # Send paginated list
        # Strip player_pid (index 3) before passing to view for existing pagination logic
        stripped_members = [(row[0], row[1], row[2], row[4]) for row in all_members]
        pagination_view = BoundAccountsPaginationView(stripped_members, show_values, interaction.user.id, current_page=1)
        embed = await pagination_view.generate_embed()
        
        message = await interaction.followup.send(embed=embed, view=pagination_view, ephemeral=False)
        pagination_view.message = message
        
        logger.info(f"Bound accounts list viewed by {interaction.user}")

    @app_commands.command(name="add-verified-member", description="Manually add a verified guild member")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        member="The discord member to add",
        character_uid="The in-game character UID of this member"
    )
    async def add_verified_member(self, interaction: discord.Interaction, member: discord.Member, character_uid: str):
        """Admin command to manually add existing members to verified database"""
        await interaction.response.defer(ephemeral=True)
        
        is_guild_member = False
        player_pid = ''
        try:
            from utility.wwm import get_club_hostnums
            player_data = await get_player_info(character_uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            if player_data and 'result' in player_data:
                player = player_data['result']
                player_pid = str(player.get('id', ''))
                if player_pid:
                    club_data = await get_club_hostnums(player_pid)
                    if club_data and 'result' in club_data:
                        player_club_data = club_data['result'].get(player_pid, {})
                        club_id = player_club_data.get('club', {}).get('club_id')
                        is_guild_member = (club_id == CLUB_ID)
        except:
            pass
        
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute('''
                REPLACE INTO verified_members
                (user_id, username, character_uid, player_pid, verified_at, verified_by)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                member.id,
                str(member),
                character_uid.strip(),
                player_pid,
                datetime.utcnow(),
                interaction.user.id
            ))
            await conn.commit()
        
        guild_role = None
        community_role = None
        
        if hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
            guild_role = interaction.guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
        if hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID'):
            community_role = interaction.guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
        
        if is_guild_member and guild_role:
            await member.add_roles(guild_role)
            if community_role and community_role in member.roles:
                await member.remove_roles(community_role)
        elif community_role:
            await member.add_roles(community_role)
            if guild_role and guild_role in member.roles:
                await member.remove_roles(guild_role)
        
        # Assign school/sect role based on player's in-game sect
        if player_data and 'result' in player_data:
            await assign_school_role(member, player_data['result'])
        
        embed = discord.Embed(
            title="✅ Member Added Successfully",
            description=f"Member {member.mention} has been added to the verified members registry.\n\n"
                       f"**Character UID:** `{character_uid.strip()}`",
            color=discord.Color.green()
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        try:
            mod_channel_id = getattr(settings, 'MOD_CHANNEL_LOG_ID', None)
            if mod_channel_id:
                log_channel = interaction.guild.get_channel(mod_channel_id)
                if log_channel:
                    notification_embed = discord.Embed(
                        title="🔗 Account Bound",
                        description=f"{member.mention} has just bound their account.",
                        color=discord.Color.green()
                    )
                    notification_embed.add_field(name="Discord User", value=f"{member.mention}\n`{str(member)}`", inline=True)
                    notification_embed.add_field(name="Character UID", value=f"`{character_uid.strip()}`", inline=True)
                    notification_embed.set_footer(text="WWM Guild Verification System")
                    await log_channel.send(embed=notification_embed)
        except Exception as e:
            logger.error(f"Failed to send binding notification: {str(e)}")
        
        logger.info(f"Manual member added: {member} | UID: {character_uid} | by {interaction.user}")

    @app_commands.command(name="unbind-account", description="Unbind your game account from Discord")
    @app_commands.describe(
        member="(Admin) Force unbind a specific Discord member",
        character_uid="(Admin) Force unbind by Character UID"
    )
    async def unbind_account(self, interaction: discord.Interaction, member: discord.Member = None, character_uid: str = None):
        """Unbind a verified account - self-service or admin force unbind"""
        
        # If member or character_uid is provided, it's an admin force unbind
        is_admin_force = member is not None or character_uid is not None
        
        if is_admin_force:
            # Admin force unbind - check permissions
            if not interaction.user.guild_permissions.administrator:
                # Also check if they have the GUILD_ADMIN_ROLE
                if hasattr(settings, 'GUILD_ADMIN_ROLE_ID'):
                    admin_role = interaction.guild.get_role(settings.GUILD_ADMIN_ROLE_ID)
                    if not admin_role or admin_role not in interaction.user.roles:
                        await interaction.response.send_message(
                            "❌ You don't have permission to force unbind users.",
                            ephemeral=True
                        )
                        return
                else:
                    await interaction.response.send_message(
                        "❌ You don't have permission to force unbind users.",
                        ephemeral=True
                    )
                    return
        await interaction.response.defer(ephemeral=True)
        
        # Look up the verified member record
        async with aiosqlite.connect(DB_PATH) as conn:
            result = None
            if is_admin_force:
                if member:
                    cursor = await conn.execute(
                        "SELECT user_id, username, character_uid, verified_at FROM verified_members WHERE user_id = ?",
                        (member.id,)
                    )
                    result = await cursor.fetchone()
                elif character_uid:
                    cursor = await conn.execute(
                        "SELECT user_id, username, character_uid, verified_at FROM verified_members WHERE character_uid = ?",
                        (character_uid.strip(),)
                    )
                    result = await cursor.fetchone()
            else:
                # Self-service: lookup by the command user
                cursor = await conn.execute(
                    "SELECT user_id, username, character_uid, verified_at FROM verified_members WHERE user_id = ?",
                    (interaction.user.id,)
                )
                result = await cursor.fetchone()
        
        if not result:
            if is_admin_force:
                await interaction.followup.send(
                    "❌ No bound account found for the provided search criteria.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ You don't have a bound account to unbind.",
                    ephemeral=True
                )
            return
        
        target_user_id, target_username, target_character_uid, verified_at_str = result
        
        from datetime import timezone
        verified_dt = datetime.fromisoformat(verified_at_str).replace(tzinfo=timezone.utc)
        verified_timestamp = int(verified_dt.timestamp())
        
        if is_admin_force:
            embed = discord.Embed(
                title="⚠️ Force Unbind Account",
                description="Are you sure you want to force unbind this account? This action cannot be undone.",
                color=discord.Color.red()
            )
            embed.add_field(name="Target User", value=f"<@{target_user_id}>\n`{target_username}`", inline=True)
            embed.add_field(name="Character UID", value=f"`{target_character_uid}`", inline=True)
            embed.add_field(name="Bound Since", value=f"<t:{verified_timestamp}>", inline=False)
            embed.add_field(name="Action By", value=interaction.user.mention, inline=False)
        else:
            embed = discord.Embed(
                title="⚠️ Confirm Account Unbind",
                description="Are you sure you want to unbind your account? This action cannot be undone.",
                color=discord.Color.yellow()
            )
            embed.add_field(name="Discord User", value=f"{interaction.user.mention}\n`{target_username}`", inline=True)
            embed.add_field(name="Character UID", value=f"`{target_character_uid}`", inline=True)
            embed.add_field(name="Bound Since", value=f"<t:{verified_timestamp}>", inline=False)
        
        view = UnbindConfirmView(
            target_user_id=target_user_id,
            target_username=target_username,
            character_uid=target_character_uid,
            is_admin_force=is_admin_force,
            admin_id=interaction.user.id if is_admin_force else None
        )
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        logger.info(f"Unbind request initiated by {interaction.user} (admin_force={is_admin_force}) for user_id={target_user_id}")

    @app_commands.command(name="setup-verification", description="Start guild verification system setup wizard")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_verification(self, interaction: discord.Interaction):
        """Admin command to start the verification setup wizard"""
        logger.info(f"Verification setup wizard started by {interaction.user}")
        
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT value FROM verification_config WHERE key = 'GUILD_VERIFICATION_CHANNEL_ID'")
            existing_config = await cursor.fetchone()
        
        if existing_config:
            embed = discord.Embed(
                title="⚠️ Existing Configuration Found",
                description="A guild verification system is already configured and active.\n\nDo you want to replace the existing configuration?",
                color=discord.Color.yellow()
            )
            
            await interaction.response.send_message(
                embed=embed,
                view=ExistingConfigCheckView(),
                ephemeral=False
            )
        else:
            embed = discord.Embed(
                title="⚙️ Guild Verification Setup Wizard",
                description="Welcome to the guild verification setup wizard.\n\nPlease follow the steps below to configure the system.",
                color=discord.Color.blue()
            )
            
            await interaction.response.send_message(
                embed=embed,
                view=SetupWizardView(),
                ephemeral=False
            )

class EnergyAlertView(discord.ui.View):
    """Ephemeral control panel for /energy-alert — set, toggle, and test the alert."""

    def __init__(self, cog, owner_id: int, member, threshold=None, is_enabled=False, last_alert_ts=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.member = member
        self.threshold = threshold
        self.is_enabled = is_enabled
        self.last_alert_ts = last_alert_ts
        self.current_energy = None
        self.notice = None

    def _rebuild(self):
        """Rebuild the embed and button states from current state."""
        self.clear_items()

        if self.threshold is None:
            status = "⚙️ **Not set up yet** — pick a threshold below to start receiving alerts."
        else:
            state = "✅ **Enabled**" if self.is_enabled else "⏸️ **Paused**"
            status = (
                f"{state}\n"
                f"🔔 **Alert when energy rises past:** **{self.threshold} / {MAX_ENERGY}**"
            )
            if self.last_alert_ts:
                try:
                    status += f"\n🕐 **Last alert:** <t:{int(self.last_alert_ts)}:R>"
                except (ValueError, TypeError):
                    pass

        energy_line = ""
        if self.current_energy is not None:
            energy_line = f"\n⚡ **Current energy:** {self.current_energy} / {MAX_ENERGY}"

        description = status + energy_line
        if self.notice:
            description += f"\n{self.notice}"

        self.embed = discord.Embed(
            title="⚡ Energy Regen Alert",
            description=(
                "I'll DM you once when your energy regens **past** your threshold "
                "while you're offline — only once, not every minute.\n\n" + description
            ),
            color=discord.Color.green() if self.is_enabled else discord.Color.greyple(),
        )
        self.embed.set_footer(
            text="DMs are used first; if they're closed, the alert is posted in the bot channel."
        )

        for btn, handler in (
            (discord.ui.Button(
                label="Set Threshold", style=discord.ButtonStyle.primary,
                emoji="🎯", custom_id="energy_alert_set"), self._handle_set),
            (discord.ui.Button(
                label="Pause" if self.is_enabled else "Enable",
                style=discord.ButtonStyle.secondary,
                emoji="⏸️" if self.is_enabled else "▶️",
                custom_id="energy_alert_toggle",
                disabled=self.threshold is None), self._handle_toggle),
            (discord.ui.Button(
                label="Reset Tracking", style=discord.ButtonStyle.secondary,
                emoji="🔄", custom_id="energy_alert_reset",
                disabled=self.threshold is None), self._handle_reset),
            (discord.ui.Button(
                label="Test Alert", style=discord.ButtonStyle.secondary,
                emoji="🧪", custom_id="energy_alert_test",
                disabled=self.threshold is None), self._handle_test),
        ):
            btn.callback = handler
            self.add_item(btn)

    async def _refresh(self, interaction, notice=None):
        if notice:
            self.notice = notice
        row = await self.cog.get_energy_alert_row(self.owner_id)
        if row:
            self.threshold, self.is_enabled, self.last_alert_ts = row[0], bool(row[1]), row[5]
        self._rebuild()
        await interaction.edit_original_response(embed=self.embed, view=self)

    async def _handle_set(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.send_modal(
            EnergyThresholdModal(owner_id=self.owner_id, cog=self.cog)
        )

    async def _handle_toggle(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        new_state = not self.is_enabled
        await self.cog.set_energy_alert_enabled(self.owner_id, new_state)
        await self._refresh(interaction, f"✅ Alerts **{'enabled' if new_state else 'paused'}**.")

    async def _handle_reset(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer()
        await self.cog.reset_energy_alert_tracking(self.owner_id)
        await self._refresh(interaction, "↺ Tracking reset — the next crossing will alert.")

    async def _handle_test(self, interaction: discord.Interaction):
        if not await ensure_owner(interaction, self):
            return
        await interaction.response.defer(ephemeral=True)
        # Exercise the real delivery path, including the DM fallback. Uses the
        # caller's ACTUAL energy so the sample reflects what a real alert
        # looks like, rather than always claiming a full bar.
        energy, extrapolated = await self.cog.fetch_my_energy(self.owner_id)
        if energy is None:
            await interaction.followup.send(
                "⚠️ Couldn't read your energy right now (no live or cached data). "
                "Your alert is still set up and will fire once data is available.",
                ephemeral=True,
            )
            return

        self.current_energy = energy
        await self.cog._send_energy_alert(
            self.member, energy, self.threshold, extrapolated, is_test=True
        )
        await interaction.followup.send(
            f"🧪 Sample alert sent at your current energy (**{energy}/{MAX_ENERGY}**). "
            "If you got it, your DMs are open — otherwise check the bot's alert channel.",
            ephemeral=True,
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class EnergyThresholdModal(discord.ui.Modal, title="Set Energy Alert Threshold"):
    """Collects the 1..MAX_ENERGY value that triggers a DM."""

    threshold = discord.ui.TextInput(
        label=f"Energy to be alerted at ({MAX_ENERGY} max)",
        placeholder="e.g. 450",
        min_length=1,
        max_length=4,
        required=True,
        style=discord.TextStyle.short,
    )

    def __init__(self, owner_id: int, cog):
        super().__init__()
        self.owner_id = owner_id
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This isn't your alert setup.", ephemeral=True
            )
            return

        raw = self.threshold.value.strip()
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message(
                f"❌ `{raw}` isn't a number. Enter a value between 1 and {MAX_ENERGY}.",
                ephemeral=True,
            )
            return

        if not 1 <= value <= MAX_ENERGY:
            await interaction.response.send_message(
                f"❌ Energy threshold must be between **1** and **{MAX_ENERGY}**.",
                ephemeral=True,
            )
            return

        await self.cog.save_energy_alert(self.owner_id, value, is_enabled=True)

        view = EnergyAlertView(
            cog=self.cog,
            owner_id=self.owner_id,
            member=interaction.user,
            threshold=value,
            is_enabled=True,
        )
        view.notice = f"✅ Alert set — I'll DM you when your energy rises past **{value}**."
        view._rebuild()
        await interaction.response.edit_message(embed=view.embed, view=view)


class BoundAccountsPaginationView(discord.ui.View):
    def __init__(self, all_members, show_values, user_id, current_page=1):
        super().__init__(timeout=120)
        self.all_members = all_members
        self.show_values = show_values
        self.user_id = user_id
        self.current_page = current_page
        self.items_per_page = 10
        self.total_pages = (len(all_members) + self.items_per_page - 1) // self.items_per_page
        self.player_cache = {}
        self.update_button_states()
    
    def update_button_states(self):
        self.prev_page_button.disabled = self.current_page <= 1
        self.next_page_button.disabled = self.current_page >= self.total_pages
    
    async def generate_embed(self):
        start_idx = (self.current_page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_members = self.all_members[start_idx:end_idx]
        
        embed = discord.Embed(
            title="📋 Bound Accounts List",
            description=f"Total bound accounts: **{len(self.all_members)}**\nPage {self.current_page}/{self.total_pages}",
            color=discord.Color.blue()
        )

        if self.show_values:
            try:
                missing_uids = []
                for member in page_members:
                    number_id = member[2]
                    if number_id not in self.player_cache:
                        missing_uids.append(number_id)
                
                if missing_uids:
                    logger.debug(f"Fetching {len(missing_uids)} missing players, {len(self.player_cache)} already cached")
                    
                    pid_list = []
                    uid_to_pid_map = {}
                    
                    from utility.wwm import _wwm_api_post
                    for number_id in missing_uids:
                        try:
                            pid_result = await _wwm_api_post(
                                WWM_API_URL,
                                {
                                    "uid": WWM_UID,
                                    "number_id": number_id,
                                    "force_search": False
                                },
                                uid=WWM_UID,
                                token=WWM_TOKEN
                            )
                            if pid_result and 'result' in pid_result and 'id' in pid_result['result']:
                                pid = pid_result['result']['id']
                                pid_list.append(pid)
                                uid_to_pid_map[pid] = number_id
                        except:
                            continue
                    
                    from utility.wwm import get_bulk_players_info
                    bulk_data = await get_bulk_players_info(pid_list, fields=["base"])
                    
                    if bulk_data and bulk_data.get('code') == 0:
                        bulk_players = bulk_data.get('result', {})
                        for pid, player_data in bulk_players.items():
                            if pid in uid_to_pid_map:
                                number_id = uid_to_pid_map[pid]
                                self.player_cache[number_id] = player_data
                            
            except Exception as e:
                logger.warning(f"Bulk player fetch failed: {str(e)}", exc_info=True)
        
        for idx, member in enumerate(page_members, start=start_idx + 1):
            user_id, username, character_uid, verified_at = member
            
            field_value = f"Discord: <@{user_id}>\nUID: `{character_uid}`"
            
            if self.show_values:
                try:
                    if character_uid in self.player_cache:
                        player = self.player_cache[character_uid]
                        nickname = player.get('base', {}).get('nickname', 'Unknown')
                        level = player.get('base', {}).get('level', 0)
                        power = player.get('base', {}).get('max_xiuwei_kungfu', 0)
                        
                        field_value += f"\n**Name:** `{nickname}`\n**Lv:** {level} | **Power:** {power:,}"
                    else:
                        field_value += "\n⚠️ Failed to load character data"
                except:
                    field_value += "\n⚠️ Failed to load character data"
            
            from datetime import timezone
            verified_dt = datetime.fromisoformat(verified_at).replace(tzinfo=timezone.utc)
            verified_timestamp = int(verified_dt.timestamp())
            field_value += f"\nBound: <t:{verified_timestamp}:D>"
            
            embed.add_field(
                name=f"#{idx} - {username}",
                value=field_value,
                inline=False
            )
        
        return embed

    @discord.ui.button(label="← Previous", style=ButtonStyle.secondary)
    async def prev_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return
        
        await interaction.response.defer()
        self.current_page -= 1
        self.update_button_states()
        await interaction.edit_original_response(embed=await self.generate_embed(), view=self)
    
    @discord.ui.button(label="Next →", style=ButtonStyle.secondary)
    async def next_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return
        
        await interaction.response.defer()
        self.current_page += 1
        self.update_button_states()
        await interaction.edit_original_response(embed=await self.generate_embed(), view=self)
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        await self.message.edit(view=self)

class ExistingConfigCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Replace Existing", style=ButtonStyle.danger, emoji="🔄")
    async def replace_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("DELETE FROM verification_config")
            await conn.commit()
        
        for key in ['GUILD_VERIFICATION_CHANNEL_ID', 'GUILD_ADMIN_CHANNEL_ID', 'GUILD_MEMBER_ROLE_ID', 'GUILD_ADMIN_ROLE_ID', 'VERIFICATION_MESSAGE_ID']:
            if hasattr(settings, key):
                delattr(settings, key)
        
        embed = discord.Embed(
            title="⚙️ Guild Verification Setup Wizard",
            description="Existing configuration has been cleared.\n\nPlease follow the steps below to configure the new system.",
            color=discord.Color.blue()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=SetupWizardView()
        )
        logger.info(f"Existing verification configuration cleared by {interaction.user}")
    
    @discord.ui.button(label="Cancel", style=ButtonStyle.secondary, emoji="❌")
    async def cancel_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="✅ Setup Cancelled",
            description="Existing configuration has been preserved.",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(embed=embed, view=None)

class SetupWizardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        self.config = {}
    
    @discord.ui.button(label="Start Setup", style=ButtonStyle.primary, emoji="▶️")
    async def start_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Step 1/5 - Select Public Verification Channel",
            description="Select the channel where users will verify their guild membership:",
            color=discord.Color.blue()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=Step1_ChannelSelect(self.config, interaction.guild)
        )

class Step1_ChannelSelect(discord.ui.View):
    def __init__(self, config, guild, page=0):
        super().__init__(timeout=600)
        self.config = config
        self.guild = guild
        self.page = page
        
        all_channels = guild.text_channels
        start = page * 25
        end = start + 25
        
        options = []
        for channel in all_channels[start:end]:
            options.append(discord.SelectOption(
                label=f"#{channel.name}",
                value=str(channel.id),
                description=f"ID: {channel.id}"
            ))
        
        channel_select = discord.ui.Select(
            placeholder=f"Select verification channel... (Page {page+1}/{(len(all_channels)-1)//25 +1})",
            options=options,
            custom_id="select_verification_channel"
        )
        channel_select.callback = self.channel_selected
        self.add_item(channel_select)
        
        if page > 0:
            prev_btn = discord.ui.Button(style=ButtonStyle.secondary, label="← Previous", custom_id="prev_page")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        
        if end < len(all_channels):
            next_btn = discord.ui.Button(style=ButtonStyle.secondary, label="Next →", custom_id="next_page")
            next_btn.callback = self.next_page
            self.add_item(next_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step1_ChannelSelect(self.config, self.guild, self.page -1))
    
    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step1_ChannelSelect(self.config, self.guild, self.page +1))
    
    async def channel_selected(self, interaction: discord.Interaction):
        self.config['GUILD_VERIFICATION_CHANNEL_ID'] = int(interaction.data['values'][0])
        
        embed = discord.Embed(
            title="✅ Step 1 Completed",
            description=f"Verification Channel: <#{self.config['GUILD_VERIFICATION_CHANNEL_ID']}>\n\nStep 2/5 - Select Admin Review Channel",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=Step2_AdminChannelSelect(self.config, interaction.guild)
        )

class Step2_AdminChannelSelect(discord.ui.View):
    def __init__(self, config, guild, page=0):
        super().__init__(timeout=600)
        self.config = config
        self.guild = guild
        self.page = page
        
        all_channels = guild.text_channels
        start = page * 25
        end = start + 25
        
        options = []
        for channel in all_channels[start:end]:
            options.append(discord.SelectOption(
                label=f"#{channel.name}",
                value=str(channel.id),
                description=f"ID: {channel.id}"
            ))
        
        channel_select = discord.ui.Select(
            placeholder=f"Select admin review channel... (Page {page+1}/{(len(all_channels)-1)//25 +1})",
            options=options,
            custom_id="select_admin_channel"
        )
        channel_select.callback = self.channel_selected
        self.add_item(channel_select)
        
        if page > 0:
            prev_btn = discord.ui.Button(style=ButtonStyle.secondary, label="← Previous", custom_id="prev_page")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        
        if end < len(all_channels):
            next_btn = discord.ui.Button(style=ButtonStyle.secondary, label="Next →", custom_id="next_page")
            next_btn.callback = self.next_page
            self.add_item(next_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step2_AdminChannelSelect(self.config, self.guild, self.page -1))
    
    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step2_AdminChannelSelect(self.config, self.guild, self.page +1))
    
    async def channel_selected(self, interaction: discord.Interaction):
        self.config['GUILD_ADMIN_CHANNEL_ID'] = int(interaction.data['values'][0])
        
        embed = discord.Embed(
            title="✅ Step 2 Completed",
            description=f"Verification Channel: <#{self.config['GUILD_VERIFICATION_CHANNEL_ID']}>\nAdmin Channel: <#{self.config['GUILD_ADMIN_CHANNEL_ID']}>\n\nStep 3/5 - Select Guild Member Role",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=Step3_MemberRoleSelect(self.config, interaction.guild)
        )

class Step3_MemberRoleSelect(discord.ui.View):
    def __init__(self, config, guild, page=0):
        super().__init__(timeout=600)
        self.config = config
        self.guild = guild
        self.page = page
        
        all_roles = [r for r in guild.roles if not r.is_bot_managed() and not r.is_default()]
        start = page * 25
        end = start + 25
        
        options = []
        for role in all_roles[start:end]:
            options.append(discord.SelectOption(
                label=f"{role.name}",
                value=str(role.id),
                description=f"ID: {role.id}"
            ))
        
        role_select = discord.ui.Select(
            placeholder=f"Select guild member role... (Page {page+1}/{(len(all_roles)-1)//25 +1})",
            options=options,
            custom_id="select_member_role"
        )
        role_select.callback = self.role_selected
        self.add_item(role_select)
        
        if page > 0:
            prev_btn = discord.ui.Button(style=ButtonStyle.secondary, label="← Previous", custom_id="prev_page")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        
        if end < len(all_roles):
            next_btn = discord.ui.Button(style=ButtonStyle.secondary, label="Next →", custom_id="next_page")
            next_btn.callback = self.next_page
            self.add_item(next_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step3_MemberRoleSelect(self.config, self.guild, self.page -1))
    
    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step3_MemberRoleSelect(self.config, self.guild, self.page +1))
    
    async def role_selected(self, interaction: discord.Interaction):
        self.config['GUILD_MEMBER_ROLE_ID'] = int(interaction.data['values'][0])
        
        embed = discord.Embed(
            title="✅ Step 3 Completed",
            description=f"Verification Channel: <#{self.config['GUILD_VERIFICATION_CHANNEL_ID']}>\nAdmin Channel: <#{self.config['GUILD_ADMIN_CHANNEL_ID']}>\nGuild Member Role: <@&{self.config['GUILD_MEMBER_ROLE_ID']}>\n\nStep 4/5 - Select Community Member Role",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=Step4_CommunityRoleSelect(self.config, interaction.guild)
        )


class Step4_CommunityRoleSelect(discord.ui.View):
    def __init__(self, config, guild, page=0):
        super().__init__(timeout=600)
        self.config = config
        self.guild = guild
        self.page = page
        
        all_roles = [r for r in guild.roles if not r.is_bot_managed() and not r.is_default()]
        start = page * 25
        end = start + 25
        
        options = []
        for role in all_roles[start:end]:
            options.append(discord.SelectOption(
                label=f"{role.name}",
                value=str(role.id),
                description=f"ID: {role.id}"
            ))
        
        role_select = discord.ui.Select(
            placeholder=f"Select community member role... (Page {page+1}/{(len(all_roles)-1)//25 +1})",
            options=options,
            custom_id="select_community_role"
        )
        role_select.callback = self.role_selected
        self.add_item(role_select)
        
        if page > 0:
            prev_btn = discord.ui.Button(style=ButtonStyle.secondary, label="← Previous", custom_id="prev_page")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        
        if end < len(all_roles):
            next_btn = discord.ui.Button(style=ButtonStyle.secondary, label="Next →", custom_id="next_page")
            next_btn.callback = self.next_page
            self.add_item(next_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step4_CommunityRoleSelect(self.config, self.guild, self.page -1))
    
    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step4_CommunityRoleSelect(self.config, self.guild, self.page +1))
    
    async def role_selected(self, interaction: discord.Interaction):
        self.config['COMMUNITY_MEMBER_ROLE_ID'] = int(interaction.data['values'][0])
        
        embed = discord.Embed(
            title="✅ Step 4 Completed",
            description=f"Verification Channel: <#{self.config['GUILD_VERIFICATION_CHANNEL_ID']}>\nAdmin Channel: <#{self.config['GUILD_ADMIN_CHANNEL_ID']}>\nGuild Member Role: <@&{self.config['GUILD_MEMBER_ROLE_ID']}>\nCommunity Member Role: <@&{self.config['COMMUNITY_MEMBER_ROLE_ID']}>\n\nStep 5/5 - Select Admin Approver Role",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=Step5_AdminRoleSelect(self.config, interaction.guild)
        )

class Step5_AdminRoleSelect(discord.ui.View):
    def __init__(self, config, guild, page=0):
        super().__init__(timeout=600)
        self.config = config
        self.guild = guild
        self.page = page
        
        all_roles = [r for r in guild.roles if not r.is_bot_managed() and not r.is_default()]
        start = page * 25
        end = start + 25
        
        options = []
        for role in all_roles[start:end]:
            options.append(discord.SelectOption(
                label=f"{role.name}",
                value=str(role.id),
                description=f"ID: {role.id}"
            ))
        
        role_select = discord.ui.Select(
            placeholder=f"Select admin approver role... (Page {page+1}/{(len(all_roles)-1)//25 +1})",
            options=options,
            custom_id="select_admin_role"
        )
        role_select.callback = self.role_selected
        self.add_item(role_select)
        
        if page > 0:
            prev_btn = discord.ui.Button(style=ButtonStyle.secondary, label="← Previous", custom_id="prev_page")
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        
        if end < len(all_roles):
            next_btn = discord.ui.Button(style=ButtonStyle.secondary, label="Next →", custom_id="next_page")
            next_btn.callback = self.next_page
            self.add_item(next_btn)
    
    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step5_AdminRoleSelect(self.config, self.guild, self.page -1))
    
    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=Step5_AdminRoleSelect(self.config, self.guild, self.page +1))
    
    async def role_selected(self, interaction: discord.Interaction):
        self.config['GUILD_ADMIN_ROLE_ID'] = int(interaction.data['values'][0])
        
        async with aiosqlite.connect(DB_PATH) as conn:
            for key, value in self.config.items():
                await conn.execute("REPLACE INTO verification_config (key, value) VALUES (?, ?)", (key, str(value)))
                setattr(settings, key, value)
            await conn.commit()
        
        embed = discord.Embed(
            title="✅ Setup Complete!",
            description="All configuration values have been saved to database:\n\n"
                       f"🔹 Verification Channel: <#{self.config['GUILD_VERIFICATION_CHANNEL_ID']}>\n"
                       f"🔹 Admin Channel: <#{self.config['GUILD_ADMIN_CHANNEL_ID']}>\n"
                       f"🔹 Guild Member Role: <@&{self.config['GUILD_MEMBER_ROLE_ID']}>\n"
                       f"🔹 Community Member Role: <@&{self.config['COMMUNITY_MEMBER_ROLE_ID']}>\n"
                       f"🔹 Admin Role: <@&{self.config['GUILD_ADMIN_ROLE_ID']}>",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(
            embed=embed,
            view=FinalSetupView(self.config)
        )

class FinalSetupView(discord.ui.View):
    def __init__(self, config):
        super().__init__(timeout=600)
        self.config = config
    
    @discord.ui.button(label="Post Verification Message", style=ButtonStyle.green, emoji="✅")
    async def post_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        verification_channel = interaction.guild.get_channel(self.config['GUILD_VERIFICATION_CHANNEL_ID'])
        
        embed = discord.Embed(
            title="✅ Bind Your Account",
            description="Link your WWM game account to your Discord account.\n\nClick the button below to verify and bind your character.",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        embed.set_footer(text="WWM Account Verification System")
        
        message = await verification_channel.send(
            embed=embed,
            view=VerificationStartView()
        )
        
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("REPLACE INTO verification_config (key, value) VALUES (?, ?)", ("VERIFICATION_MESSAGE_ID", str(message.id)))
            await conn.commit()
        
        final_embed = discord.Embed(
            title="✅ System Successfully Activated!",
            description="The guild verification system is now live and fully persistent.\n\n"
                       "Configuration and all requests are saved in database and survive bot restarts.",
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(embed=final_embed, view=None)
        logger.info(f"Guild verification system fully setup by {interaction.user}")

class VerificationStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(
        label="Bind My Game Account",
        style=ButtonStyle.green,
        custom_id="guild_verify:start",
        emoji="🔗"
    )
    async def start_verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CharacterUIDModal())

class CharacterUIDModal(discord.ui.Modal, title="Bind Game Account"):
    character_uid = discord.ui.TextInput(
        label="Enter your Character Number ID",
        placeholder="Paste your 10 digit Character Number ID here...",
        min_length=3,
        max_length=50,
        required=True,
        style=discord.TextStyle.short
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        uid = self.character_uid.value.strip()
        
        if not uid.isdigit() or len(uid) != 10:
            await interaction.response.send_message(
                "❌ **Invalid Character UID**\n\nCharacter UID must be exactly 10 numbers.\nPlease try again with a valid UID.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            player_data = await get_player_info(uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL, force_search=True)
            
            if not player_data or 'result' not in player_data:
                await interaction.followup.send(
                    "❌ Failed to retrieve character information. Please verify the UID and try again.",
                    ephemeral=True
                )
                return
            
            player = player_data.get('result', {})
            nickname = player.get('base', {}).get('nickname', 'Unknown')
            level = player.get('base', {}).get('level', 0)
            
            is_guild_member = False
            player_pid = player.get('id')
            
            if player_pid:
                club_data = await get_club_hostnums(player_pid)
                if club_data and 'result' in club_data:
                    player_club_data = club_data['result'].get(player_pid, {})
                    club_id = player_club_data.get('club', {}).get('club_id')
                    is_guild_member = (club_id == CLUB_ID)
            
            embed = discord.Embed(
                title="✅ Character Found",
                color=discord.Color.green() if is_guild_member else discord.Color.red()
            )
            
            embed.add_field(name="Character Name", value=f"`{nickname}`", inline=True)
            embed.add_field(name="Level", value=f"`{level}`", inline=True)
            embed.add_field(name="Guild Member", value="✅ Yes" if is_guild_member else "❌ No", inline=True)
            
            embed.description = "Is this your character?"
            
            await interaction.followup.send(
                embed=embed,
                view=ConfirmCharacterView(
                    user_id=interaction.user.id,
                    username=str(interaction.user),
                    character_uid=uid,
                    is_member=is_guild_member,
                    nickname=nickname,
                    level=level
                ),
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error fetching player data: {str(e)}")
            await interaction.followup.send(
                "❌ An error occurred while verifying your character. Please try again later.",
                ephemeral=True
            )


class ConfirmCharacterView(discord.ui.View):
    def __init__(self, user_id, username, character_uid, is_member, nickname, level):
        super().__init__(timeout=3600)
        self.user_id = user_id
        self.username = username
        self.character_uid = character_uid
        self.is_member = is_member
        self.nickname = nickname
        self.level = level
        self.verify_code = ''.join([str(random.randint(0,9)) for _ in range(6)])
    
    @discord.ui.button(label="This is my character", style=ButtonStyle.green, emoji="✅")
    async def confirm_character(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This verification is not for you.", ephemeral=True)
            return
        
        await interaction.response.edit_message(
            content=f"✅ Confirmation received!\n\n"
                    f"**Your verification code is:** `{self.verify_code}`\n\n"
                    f"Please put this code **anywhere in your in-game profile signature**.\n"
                    f"Once you have added the code, click the button below to verify automatically.\n\n"
                    f"💡 Tip: The code can be placed anywhere, even at the end or hidden among other text.",
            embed=None,
            view=VerifySignatureView(
                user_id=self.user_id,
                username=self.username,
                character_uid=self.character_uid,
                is_member=self.is_member,
                verify_code=self.verify_code
            )
        )
    
    @discord.ui.button(label="Cancel", style=ButtonStyle.secondary, emoji="❌")
    async def cancel_verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This verification is not for you.", ephemeral=True)
            return
        
        await interaction.response.edit_message(
            content="Verification cancelled.",
            embed=None,
            view=None
        )


class VerifySignatureView(discord.ui.View):
    def __init__(self, user_id, username, character_uid, is_member, verify_code):
        super().__init__(timeout=3600)
        self.user_id = user_id
        self.username = username
        self.character_uid = character_uid
        self.is_member = is_member
        self.verify_code = verify_code
    
    @discord.ui.button(label="✓ I have added the code", style=ButtonStyle.green, emoji="🔍")
    async def verify_signature(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This verification is not for you.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            player_data = await get_player_info(self.character_uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL, force_search=True)
            
            if not player_data or 'result' not in player_data:
                await interaction.followup.send(
                    "❌ Failed to retrieve character information. Please try again later.",
                    ephemeral=True
                )
                return
            
            player = player_data.get('result', {})
            name_card = player.get('name_card', {})
            signature = name_card.get('sign', '')
            
            if self.verify_code in str(signature):
                target_user = interaction.guild.get_member(self.user_id)
                
                if target_user:
                    guild_role = None
                    community_role = None
                    
                    if hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
                        guild_role = interaction.guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
                    if hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID'):
                        community_role = interaction.guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
                    
                    if self.is_member and guild_role:
                        await target_user.add_roles(guild_role)
                        if community_role and community_role in target_user.roles:
                            await target_user.remove_roles(community_role)
                    elif community_role:
                        await target_user.add_roles(community_role)
                        if guild_role and guild_role in target_user.roles:
                            await target_user.remove_roles(guild_role)
                
                # Assign school/sect role based on player's in-game sect
                await assign_school_role(target_user, player)
                
                player_pid = str(player.get('id', ''))
                async with aiosqlite.connect(DB_PATH) as conn:
                    await conn.execute('''
                        REPLACE INTO verified_members
                        (user_id, username, character_uid, player_pid, verified_at, verified_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        self.user_id,
                        self.username,
                        self.character_uid,
                        player_pid,
                        datetime.utcnow(),
                        self.user_id
                    ))
                    await conn.commit()
                
                await interaction.followup.send(
                    "✅ **Verification Successful!**\n\n"
                    "Your account has been successfully bound and verified.\n"
                    "You now have access to all member features.\n\n"
                    "You may now remove the code from your signature if you wish.",
                    ephemeral=True
                )
                
                try:
                    mod_channel_id = getattr(settings, 'MOD_CHANNEL_LOG_ID', None)
                    if mod_channel_id:
                        log_channel = interaction.guild.get_channel(mod_channel_id)
                        if log_channel:
                            notification_embed = discord.Embed(
                                title="🔗 Account Bound",
                                description=f"{interaction.user.mention} has just bound their account.",
                                color=discord.Color.green()
                            )
                            notification_embed.add_field(name="Discord User", value=f"{interaction.user.mention}\n`{self.username}`", inline=True)
                            notification_embed.add_field(name="Character UID", value=f"`{self.character_uid}`", inline=True)
                            notification_embed.set_footer(text="WWM Guild Verification System")
                            await log_channel.send(embed=notification_embed)
                except Exception as e:
                    logger.error(f"Failed to send binding notification: {str(e)}")
                
                logger.info(f"Automatic verification completed for user {self.username} | Character UID: {self.character_uid}")
            
            else:
                signature_preview = str(signature).strip() if signature else "(empty signature)"
                if len(signature_preview) > 500:
                    signature_preview = signature_preview[:500] + "... (truncated)"
                
                await interaction.followup.send(
                    f"❌ **Verification Code Not Found**\n\n"
                    f"I could not find the code `{self.verify_code}` in your profile signature.\n\n"
                    f"**This is what I see in your signature right now:**\n"
                    f"```\n{signature_preview}\n```\n\n"
                    f"Please make sure you have entered the code correctly in your in-game signature, then try again.\n\n"
                    f"💡 Note: It may take up to 1 minute for profile changes to update on the server.",
                    ephemeral=True
                )
        
        except Exception as e:
            logger.error(f"Error verifying signature: {str(e)}")
            await interaction.followup.send(
                "❌ An error occurred while verifying your signature. Please try again later.",
                ephemeral=True
            )

class VerificationAdminView(discord.ui.View):
    def __init__(self, user_id: int = None, username: str = None, character_uid: str = None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.username = username
        self.character_uid = character_uid
    
    @discord.ui.button(
        label="Approve",
        style=ButtonStyle.green,
        custom_id="guild_verify:approve",
        emoji="✅"
    )
    async def approve_verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.user_id:
            for field in interaction.message.embeds[0].fields:
                if field.name == "User ID":
                    self.user_id = int(field.value.strip('`'))
                if field.name == "Character UID":
                    self.character_uid = field.value.strip('`')
            self.username = str(interaction.user)
        
        admin_role = interaction.guild.get_role(settings.GUILD_ADMIN_ROLE_ID)
        if not admin_role or admin_role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ You are not authorized to approve verification requests.",
                ephemeral=True
            )
            return
        
        target_user = interaction.guild.get_member(self.user_id)
        if not target_user:
            await interaction.response.send_message(
                "❌ User not found on the server.",
                ephemeral=True
            )
            return
        
        is_member = False
        for field in interaction.message.embeds[0].fields:
            if field.name == "Guild Member":
                is_member = ("✅" in field.value)
        
        guild_role = None
        community_role = None
        
        if hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
            guild_role = interaction.guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
        if hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID'):
            community_role = interaction.guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
        
        if is_member and guild_role:
            await target_user.add_roles(guild_role)
            if community_role and community_role in target_user.roles:
                await target_user.remove_roles(community_role)
            logger.info(f"Guild member role assigned to {target_user} by {interaction.user}")
        elif community_role:
            await target_user.add_roles(community_role)
            if guild_role and guild_role in target_user.roles:
                await target_user.remove_roles(guild_role)
            logger.info(f"Community member role assigned to {target_user} by {interaction.user}")
        
        player_pid = ''
        player_data_for_school = None
        try:
            pid_data = await get_player_info(self.character_uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            if pid_data and 'result' in pid_data:
                player_pid = str(pid_data['result'].get('id', ''))
                player_data_for_school = pid_data['result']
        except Exception as e:
            logger.warning(f"Failed to resolve PID for admin approval of {self.character_uid}: {e}")
        
        # Assign school/sect role based on player's in-game sect
        if player_data_for_school:
            await assign_school_role(target_user, player_data_for_school)
        
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute('''
                UPDATE verification_requests
                SET status = 'approved', admin_id = ?, processed_at = ?
                WHERE user_id = ? AND status = 'pending'
            ''', (interaction.user.id, datetime.utcnow(), self.user_id))
            
            await conn.execute('''
                REPLACE INTO verified_members
                (user_id, username, character_uid, player_pid, verified_at, verified_by)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                self.user_id,
                self.username,
                self.character_uid,
                player_pid,
                datetime.utcnow(),
                interaction.user.id
            ))
            await conn.commit()
        
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Status", value=f"✅ **APPROVED** by {interaction.user.mention}", inline=False)
        
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("✅ Verification approved and role assigned!", ephemeral=True)
        
        try:
            await target_user.send(
                "✅ **Your account binding has been approved!**\n\n"
                "You now have access to all member features."
            )
        except:
            logger.warning(f"Could not send approval DM to user {target_user}")

    @discord.ui.button(
        label="Reject",
        style=ButtonStyle.red,
        custom_id="guild_verify:reject",
        emoji="❌"
    )
    async def reject_verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        admin_role = interaction.guild.get_role(settings.GUILD_ADMIN_ROLE_ID)
        if not admin_role or admin_role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ You are not authorized to reject verification requests.",
                ephemeral=True
            )
            return
        
        await interaction.response.send_modal(RejectReasonModal(
            user_id=self.user_id,
            username=self.username,
            character_uid=self.character_uid,
            original_message=interaction.message
        ))

class RejectReasonModal(discord.ui.Modal, title="Reject Verification Request"):
    def __init__(self, user_id, username, character_uid, original_message):
        super().__init__()
        self.user_id = user_id
        self.username = username
        self.character_uid = character_uid
        self.original_message = original_message
    
    reason = discord.ui.TextInput(
        label="Rejection Reason",
        placeholder="Enter reason for rejection (will be sent to user)...",
        required=True,
        min_length=3,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        reject_reason = self.reason.value.strip()
        
        target_user = interaction.guild.get_member(self.user_id)
        
        embed = self.original_message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Status", value=f"❌ **REJECTED** by {interaction.user.mention}", inline=False)
        embed.add_field(name="Reason", value=f"`{reject_reason}`", inline=False)
        
        await self.original_message.edit(embed=embed, view=None)
        await interaction.response.send_message("✅ Verification rejected with reason!", ephemeral=True)
        
        if target_user:
            try:
                await target_user.send(
                    f"❌ Your guild membership verification has been rejected.\n\n"
                    f"**Reason:** {reject_reason}\n\n"
                    f"Please contact an admin if you believe this is an error."
                )
            except:
                logger.warning(f"Could not send DM to user {target_user}")
        
        logger.info(f"Verification rejected for {self.username} by {interaction.user} | Reason: {reject_reason}")


class UnbindConfirmView(discord.ui.View):
    """Confirmation view for unbinding accounts - used for both self-service and admin force unbind"""
    
    def __init__(self, target_user_id: int, target_username: str, character_uid: str,
                 is_admin_force: bool = False, admin_id: int = None):
        super().__init__(timeout=120)
        self.target_user_id = target_user_id
        self.target_username = target_username
        self.character_uid = character_uid
        self.is_admin_force = is_admin_force
        self.admin_id = admin_id
        
        if is_admin_force:
            self.confirm_button.label = "Yes, Force Unbind"
            self.confirm_button.style = ButtonStyle.danger
        else:
            self.confirm_button.label = "Yes, Unbind My Account"
            self.confirm_button.style = ButtonStyle.danger
    
    @discord.ui.button(label="Yes, Unbind My Account", style=ButtonStyle.danger, emoji="⚠️")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Verify the user clicking has permission
        if self.is_admin_force:
            # Admin force: verify the clicking user is the same admin or has admin perms
            if interaction.user.id != self.admin_id:
                if not interaction.user.guild_permissions.administrator:
                    if hasattr(settings, 'GUILD_ADMIN_ROLE_ID'):
                        admin_role = interaction.guild.get_role(settings.GUILD_ADMIN_ROLE_ID)
                        if not admin_role or admin_role not in interaction.user.roles:
                            await interaction.response.send_message("You are not authorized to perform this action.", ephemeral=True)
                            return
                    else:
                        await interaction.response.send_message("You are not authorized to perform this action.", ephemeral=True)
                        return
        else:
            # Self-service: verify the clicking user is the target user
            if interaction.user.id != self.target_user_id:
                await interaction.response.send_message("This unbind request is not for you.", ephemeral=True)
                return
        
        await interaction.response.defer(ephemeral=True)
        
        # Verify the record still exists (race condition check)
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute("SELECT user_id FROM verified_members WHERE user_id = ?", (self.target_user_id,))
            existing = await cursor.fetchone()
            
            if not existing:
                await interaction.followup.send(
                    "❌ This account has already been unbound.",
                    ephemeral=True
                )
                return
            
            # Delete from verified_members
            await conn.execute("DELETE FROM verified_members WHERE user_id = ?", (self.target_user_id,))
            # Drop any energy alert subscription too — the table is keyed by
            # user_id, so without this it would outlive the binding.
            await conn.execute("DELETE FROM energy_alerts WHERE user_id = ?", (self.target_user_id,))
            await conn.commit()
        
        # Remove verification-related roles from the member if they're still in the guild
        guild = interaction.guild
        target_member = guild.get_member(self.target_user_id)
        
        if target_member:
            roles_to_remove = []
            if hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
                guild_role = guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
                if guild_role and guild_role in target_member.roles:
                    roles_to_remove.append(guild_role)
            if hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID'):
                community_role = guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
                if community_role and community_role in target_member.roles:
                    roles_to_remove.append(community_role)
            
            # Also remove any school/sect role
            school_roles_to_remove = [r for r in target_member.roles if r.id in SCHOOL_ROLES.values()]
            roles_to_remove.extend(school_roles_to_remove)
            
            if roles_to_remove:
                await target_member.remove_roles(*roles_to_remove)
                logger.info(f"Removed roles {[r.name for r in roles_to_remove]} from {target_member} (unbind)")
        
        # Send success response
        if self.is_admin_force:
            embed = discord.Embed(
                title="✅ Account Force Unbound",
                description=f"The account for {self.target_username} has been successfully unbound.",
                color=discord.Color.green()
            )
            embed.add_field(name="Target User", value=f"<@{self.target_user_id}>", inline=True)
            embed.add_field(name="Character UID", value=f"`{self.character_uid}`", inline=True)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Notify the unbound user via DM
            if target_member:
                try:
                    await target_member.send(
                        f"❌ **Your account binding has been removed by staff.**\n\n"
                        f"If you believe this is an error, please contact the server administrators."
                    )
                except:
                    logger.warning(f"Could not send unbind DM to user {target_member}")
            
            logger.info(f"Admin force unbind: {self.target_username} (UID: {self.character_uid}) by {interaction.user}")
        else:
            embed = discord.Embed(
                title="✅ Account Unbound",
                description=f"Your account has been successfully unbound.\n\n"
                           f"You can always bind a new account later using the verification system.",
                color=discord.Color.green()
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            logger.info(f"Self-service unbind: {self.target_username} (UID: {self.character_uid})")
        
        # Log to the binding log channel
        try:
            mod_channel_id = getattr(settings, 'MOD_CHANNEL_LOG_ID', None)
            if mod_channel_id:
                log_channel = guild.get_channel(mod_channel_id)
                if log_channel:
                    if self.is_admin_force:
                        log_embed = discord.Embed(
                            title="🔓 Account Unbound (Force)",
                            description=f"An administrator has force unbound a member's account.",
                            color=discord.Color.red()
                        )
                        log_embed.add_field(name="Target User", value=f"<@{self.target_user_id}>\n`{self.target_username}`", inline=True)
                        log_embed.add_field(name="Character UID", value=f"`{self.character_uid}`", inline=True)
                        log_embed.add_field(name="Unbound By", value=interaction.user.mention, inline=False)
                    else:
                        log_embed = discord.Embed(
                            title="🔓 Account Unbound (Self)",
                            description=f"A member has unbound their own account.",
                            color=discord.Color.yellow()
                        )
                        log_embed.add_field(name="User", value=f"<@{self.target_user_id}>\n`{self.target_username}`", inline=True)
                        log_embed.add_field(name="Character UID", value=f"`{self.character_uid}`", inline=True)
                    
                    log_embed.set_footer(text="WWM Guild Verification System")
                    await log_channel.send(embed=log_embed)
        except Exception as e:
            logger.error(f"Failed to send unbind log: {str(e)}")
        
    
    @discord.ui.button(label="Cancel", style=ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_admin_force:
            # Allow the same admin or any admin to cancel
            if interaction.user.id != self.admin_id:
                if not interaction.user.guild_permissions.administrator:
                    if hasattr(settings, 'GUILD_ADMIN_ROLE_ID'):
                        admin_role = interaction.guild.get_role(settings.GUILD_ADMIN_ROLE_ID)
                        if not admin_role or admin_role not in interaction.user.roles:
                            await interaction.response.send_message("You cannot cancel this action.", ephemeral=True)
                            return
                    else:
                        await interaction.response.send_message("You cannot cancel this action.", ephemeral=True)
                        return
        else:
            if interaction.user.id != self.target_user_id:
                await interaction.response.send_message("This unbind request is not for you.", ephemeral=True)
                return
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        
        if self.is_admin_force:
            embed = discord.Embed(
                title="✅ Force Unbind Cancelled",
                description="The force unbind action has been cancelled.",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="✅ Unbind Cancelled",
                description="Your account unbind has been cancelled.",
                color=discord.Color.green()
            )
        
        await interaction.response.edit_message(embed=embed, view=self)
        logger.info(f"Unbind cancelled by {interaction.user} (admin_force={self.is_admin_force})")
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        
        # Edit the message to show it timed out if we have a reference
        try:
            embed = discord.Embed(
                title="⏰ Request Expired",
                description="This unbind request has expired. Please use the command again.",
                color=discord.Color.greyple()
            )
            await self.message.edit(embed=embed, view=self)
        except:
            pass


from cogs.view_registry import register

# Self-register persistent views for restart recovery
register(VerificationStartView)
register(VerificationAdminView)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildVerificationCog(bot))
