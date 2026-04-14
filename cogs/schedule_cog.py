import discord
import sqlite3
import asyncio
import datetime
from discord.ext import commands, tasks
from discord import app_commands
from settings import logger, BASE_DIR, BOT_OWNER_ID

GMT8_OFFSET = 8 * 3600  # 8 hours in seconds

class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = BASE_DIR / "data" / "schedule.db"
        self.schedule_message = None
        self.schedule_channel = None

    async def cog_load(self):
        # Ensure data directory exists
        (BASE_DIR / "data").mkdir(exist_ok=True)
        
        # Initialize database
        with sqlite3.connect(self.db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS schedule_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    notes TEXT,
                    sort_order INTEGER DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS schedule_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            db.commit()
        
        # Load saved config
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute("SELECT value FROM schedule_config WHERE key = 'channel_id'")
            row = cursor.fetchone()
            if row:
                self.schedule_channel = self.bot.get_channel(int(row[0]))
            
            cursor = db.execute("SELECT value FROM schedule_config WHERE key = 'message_id'")
            row = cursor.fetchone()
            if row and self.schedule_channel:
                try:
                    self.schedule_message = await self.schedule_channel.fetch_message(int(row[0]))
                except discord.NotFound:
                    pass
        
        # Start background refresh task
        self.refresh_schedule_task.start()
        logger.info("Schedule cog loaded successfully")

    async def cog_unload(self):
        self.refresh_schedule_task.cancel()
        logger.info("Schedule cog unloaded")

    def get_day_number(self, timestamp: int) -> int:
        """
        Calculate which schedule day this timestamp falls on.
        Day starts at 5 AM GMT+8, ends at 4:59 AM next day.
        Returns 1-7 for weekly cycle.
        """
        # Convert to GMT+8 time
        gmt8_time = timestamp + GMT8_OFFSET
        dt = datetime.datetime.fromtimestamp(gmt8_time, tz=datetime.timezone.utc)
        
        # Subtract 5 hours so that 5 AM becomes midnight for date calculation
        adjusted_dt = dt - datetime.timedelta(hours=5)
        weekday = adjusted_dt.weekday()  # 0 = Monday, 6 = Sunday
        
        # Reset day (Day 1) is Monday
        return weekday + 1

    def get_current_schedule_day(self) -> int:
        """Get current schedule day based on real time"""
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        return self.get_day_number(now)

    async def get_all_events(self):
        """Get all events with automatically calculated current week timestamps"""
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        
        # Get day number and time
        current_day = self.get_current_schedule_day()
        gmt8_now = now + GMT8_OFFSET
        current_hour = (gmt8_now % 86400) // 3600
        current_minute = ((gmt8_now % 86400) % 3600) // 60
        
        # Check if it's Sunday (Day 7) after 5:00 PM - show next week
        show_next_week = False
        if current_day == 7 and (current_hour >= 17):
            show_next_week = True
        
        # Get base monday timestamp (5 AM GMT+8 of this week)
        adjusted_now = now + GMT8_OFFSET - (5 * 3600)  # Subtract 5 hours for day boundary
        monday_dt = datetime.datetime.fromtimestamp(adjusted_now, datetime.timezone.utc)
        monday_dt = monday_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        monday_dt = monday_dt - datetime.timedelta(days=monday_dt.weekday())
        
        if show_next_week:
            monday_dt += datetime.timedelta(days=7)
        
        base_monday_timestamp = int(monday_dt.timestamp()) + (5 * 3600)  # Add back 5 hours
        
        events = []
        
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute("SELECT * FROM schedule_events ORDER BY timestamp ASC")
            raw_events = cursor.fetchall()
            
            for raw_event in raw_events:
                event = dict(raw_event)
                
                # Calculate which day and offset this event is
                event_day = self.get_day_number(event['timestamp'])
                gmt8_event_time = event['timestamp'] + GMT8_OFFSET
                seconds_since_5am = gmt8_event_time % 86400
                
                # Calculate actual timestamp for current/next week
                actual_timestamp = base_monday_timestamp + ((event_day - 1) * 86400) + seconds_since_5am
                
                event['timestamp'] = actual_timestamp
                events.append(event)
        
        return sorted(events, key=lambda x: x['timestamp'])

    async def build_schedule_message(self):
        """Build the full schedule message content"""
        events = await self.get_all_events()
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        
        # Group events by day
        days = {1: [], 2: [], 3: [], 4: [], 5: [], 6: [], 7: []}
        for event in events:
            day_num = self.get_day_number(event['timestamp'])
            if 1 <= day_num <= 7:
                days[day_num].append(event)
        
        # Find next upcoming event
        next_event = None
        for event in sorted(events, key=lambda x: x['timestamp']):
            if event['timestamp'] > now:
                next_event = event
                break
        
        lines = []
        current_day = self.get_current_schedule_day()
        
        for day in range(1, 8):
            day_events = days[day]
            if not day_events:
                continue
                
            if day == 1:
                day_title = f"**Day {day} (reset day):**"
            else:
                day_title = f"**Day {day}:**"
            
            if day == current_day:
                day_title = f"> {day_title} 👈 Today"
            
            lines.append(day_title)
            
            for event in day_events:
                ts = event['timestamp']
                name = event['event_name']
                
                line = f"- <t:{ts}:F> (<t:{ts}:R>) - {name}"
                
                # Highlight next event
                if next_event and event['id'] == next_event['id']:
                    line = f"- **<t:{ts}:F> (<t:{ts}:R>) - {name}** ⬅️ NEXT"
                # Dim past events
                elif ts < now:
                    line = f"- ~~<t:{ts}:F> (<t:{ts}:R>) - {name}~~"
                
                lines.append(line)
        
        # Add footnotes
        lines.append("")
        lines.append("-# GvG stands for Guild Wars")
        lines.append("-# GHR stands for Guild Hero Realm")
        lines.append("-# Special note: For GvG days, guild party will begin at different timing than usual, check above")
        lines.append(f"*Last updated: <t:{now}:T>*")
        
        return "\n".join(lines)

    @tasks.loop(minutes=1)
    async def refresh_schedule_task(self):
        """Background task to refresh schedule message every minute"""
        if not self.schedule_channel or not self.schedule_message:
            return
            
        try:
            content = await self.build_schedule_message()
            await self.schedule_message.edit(content=content)
            logger.debug("Schedule message updated successfully")
        except Exception as e:
            logger.error(f"Failed to update schedule message: {e}")

    @refresh_schedule_task.before_loop
    async def before_refresh_task(self):
        await self.bot.wait_until_ready()

    schedule = app_commands.Group(name="schedule", description="Manage guild event schedule")

    @schedule.command(name="view", description="View the full event schedule")
    async def schedule_view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        content = await self.build_schedule_message()
        await interaction.followup.send(content)

    @schedule.command(name="add", description="Add a new event to the schedule")
    @app_commands.describe(
        name="Name of the event",
        timestamp="Unix timestamp for the event",
        notes="Optional notes for the event"
    )
    async def schedule_add(self, interaction: discord.Interaction, name: str, timestamp: int, notes: str = None):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO schedule_events (event_name, timestamp, notes) VALUES (?, ?, ?)",
                (name, timestamp, notes)
            )
            db.commit()
        
        await interaction.response.send_message(f"✅ Event '{name}' added successfully.", ephemeral=True)
        logger.info(f"Event added by {interaction.user}: {name} at {timestamp}")


    async def get_event_select_menu(self):
        """Create a select dropdown menu with all events"""
        events = await self.get_all_events()
        
        if not events:
            return None
        
        options = []
        for event in events:
            day_num = self.get_day_number(event['timestamp'])
            # Convert to GMT+8 time
            gmt8_time = datetime.datetime.fromtimestamp(event['timestamp'] + GMT8_OFFSET, datetime.timezone.utc)
            time_str = gmt8_time.strftime("%H:%M")
            
            options.append(discord.SelectOption(
                label=event['event_name'],
                value=str(event['id']),
                description=f"Day {day_num} at {time_str} (GMT+8)",
                emoji="📅"
            ))
        
        return discord.ui.Select(
            placeholder="Select an event...",
            options=options,
            min_values=1,
            max_values=1
        )

    @schedule.command(name="edit", description="Edit an existing event")
    async def schedule_edit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        select = await self.get_event_select_menu()
        
        if not select:
            return await interaction.response.send_message("No events found to edit.", ephemeral=True)
        
        async def select_callback(select_interaction: discord.Interaction):
            event_id = int(select_interaction.data['values'][0])
            
            # Get current event values
            with sqlite3.connect(self.db_path) as db:
                db.row_factory = sqlite3.Row
                cursor = db.execute("SELECT * FROM schedule_events WHERE id = ?", (event_id,))
                event = cursor.fetchone()
            
            class EditModal(discord.ui.Modal, title="Edit Event"):
                name = discord.ui.TextInput(label="Event Name", default=event['event_name'])
                timestamp = discord.ui.TextInput(label="Unix Timestamp", default=str(event['timestamp']))
                notes = discord.ui.TextInput(label="Notes", style=discord.TextStyle.long, default=event['notes'] if event['notes'] else "", required=False)
                
                async def on_submit(self, modal_interaction: discord.Interaction):
                    updates = []
                    params = []
                    
                    updates.append("event_name = ?")
                    params.append(self.name.value)
                    
                    updates.append("timestamp = ?")
                    params.append(int(self.timestamp.value))
                    
                    updates.append("notes = ?")
                    params.append(self.notes.value)
                    
                    params.append(event_id)
                    
                    with sqlite3.connect(self.db_path) as db:
                        db.execute(
                            f"UPDATE schedule_events SET {', '.join(updates)} WHERE id = ?",
                            params
                        )
                        db.commit()
                    
                    await modal_interaction.response.send_message(f"✅ Event updated successfully.", ephemeral=True)
                    logger.info(f"Event #{event_id} edited by {interaction.user}")
            
            modal = EditModal()
            # Pass db_path to modal instance
            modal.db_path = self.db_path
            
            await select_interaction.response.send_modal(modal)
        
        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)
        
        await interaction.response.send_message("Select an event to edit:", view=view, ephemeral=True)

    @schedule.command(name="delete", description="Delete an event from the schedule")
    async def schedule_delete(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        select = await self.get_event_select_menu()
        
        if not select:
            return await interaction.response.send_message("No events found to delete.", ephemeral=True)
        
        async def select_callback(select_interaction: discord.Interaction):
            event_id = int(select_interaction.data['values'][0])
            
            with sqlite3.connect(self.db_path) as db:
                db.execute("DELETE FROM schedule_events WHERE id = ?", (event_id,))
                db.commit()
            
            await select_interaction.response.send_message(f"✅ Event deleted successfully.", ephemeral=True)
            logger.info(f"Event #{event_id} deleted by {interaction.user}")
        
        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)
        
        await interaction.response.send_message("Select an event to delete:", view=view, ephemeral=True)

    @schedule.command(name="set-channel", description="Set channel for auto-updating schedule")
    async def schedule_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        await interaction.response.defer()
        
        # Send initial schedule message
        content = await self.build_schedule_message()
        message = await channel.send(content)
        
        self.schedule_channel = channel
        self.schedule_message = message
        
        # Save to database
        with sqlite3.connect(self.db_path) as db:
            db.execute("REPLACE INTO schedule_config (key, value) VALUES ('channel_id', ?)", (str(channel.id),))
            db.execute("REPLACE INTO schedule_config (key, value) VALUES ('message_id', ?)", (str(message.id),))
            db.commit()
        
        await interaction.followup.send(f"✅ Schedule channel set to {channel.mention}. Schedule will auto-update every minute.", ephemeral=True)
        logger.info(f"Schedule channel set to {channel.id} by {interaction.user}")

    @schedule.command(name="refresh", description="Force immediate schedule refresh")
    async def schedule_refresh(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        if not self.schedule_message:
            return await interaction.response.send_message("No schedule message configured. Use /schedule set-channel first.", ephemeral=True)
        
        await interaction.response.defer()
        content = await self.build_schedule_message()
        await self.schedule_message.edit(content=content)
        await interaction.followup.send("✅ Schedule refreshed manually.", ephemeral=True)

    @schedule.command(name="import-example", description="Import the example template schedule")
    async def schedule_import_example(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        await interaction.response.defer()
        
        # Example events from your template
        example_events = [
            # Day 1 (Monday / Reset Day)
            ("Weekly Reset", 1776027600, None),
            ("Guild Party", 1776042000, None),
            ("GvG #2", 1776043800, None),
            ("GHR", 1776050100, None),
            
            # Day 2 (Tuesday)
            ("Guild Party", 1776135600, None),
            ("Showdown", 1776137400, None),
            
            # Day 3 (Wednesday)
            ("Guild Party", 1776222000, None),
            
            # Day 4 (Thursday)
            ("Guild Party", 1776308400, None),
            ("GHR", 1776309300, None),
            ("Breaking Army (***Sentinel Howlion***)", 1776310200, None),
            
            # Day 5 (Friday)
            ("Guild Party", 1776394800, None),
            ("Showdown", 1776396600, None),
            
            # Day 6 (Saturday)
            ("Guild Party", 1776481200, None),
            ("Breaking Army (***Pocketrupt Circus***)", 1776486600, None),
            
            # Day 7 (Sunday)
            ("Guild Party", 1776560400, None),
            ("GvG #1", 1776560400, None),
            ("GHR", 1776568500, None),
        ]
        
        with sqlite3.connect(self.db_path) as db:
            # Clear existing events
            db.execute("DELETE FROM schedule_events")
            
            # Insert all example events
            db.executemany(
                "INSERT INTO schedule_events (event_name, timestamp, notes) VALUES (?, ?, ?)",
                example_events
            )
            db.commit()
        
        await interaction.followup.send(f"✅ Imported {len(example_events)} example events successfully. The full example schedule is now loaded.", ephemeral=True)
        logger.info(f"Example schedule imported by {interaction.user}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))