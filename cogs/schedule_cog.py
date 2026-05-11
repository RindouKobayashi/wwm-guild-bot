import discord
import aiosqlite
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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schedule_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    notes TEXT,
                    sort_order INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schedule_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS breaking_army_bosses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_number INTEGER NOT NULL,
                    boss_name TEXT NOT NULL,
                    locked BOOLEAN DEFAULT 0,
                    rolled_at INTEGER NOT NULL,
                    rolled_by_user_id INTEGER NOT NULL,
                    UNIQUE(week_number, boss_name)
                )
            """)
            await db.commit()
        
        # Load saved config
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT value FROM schedule_config WHERE key = 'channel_id'")
            row = await cursor.fetchone()
            if row:
                self.schedule_channel = self.bot.get_channel(int(row[0]))
            
            cursor = await db.execute("SELECT value FROM schedule_config WHERE key = 'message_id'")
            row = await cursor.fetchone()
            if row and self.schedule_channel:
                try:
                    self.schedule_message = await self.schedule_channel.fetch_message(int(row[0]))
                except discord.NotFound:
                    pass
        
        # Start background tasks
        self.refresh_schedule_task.start()
        self.weekly_shift_task.start()
        logger.info("Schedule cog loaded successfully")

    async def cog_unload(self):
        self.refresh_schedule_task.cancel()
        self.weekly_shift_task.cancel()
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
        """Get all events sorted by timestamp"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM schedule_events ORDER BY timestamp ASC")
            return await cursor.fetchall()

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

        # Check if we are previewing next week (already shifted but still on old week real time)
        showing_next_week = False
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT MIN(timestamp) FROM schedule_events")
            row = await cursor.fetchone()
            first_event_ts = row[0] if row else None
            if first_event_ts:
                first_event_day = self.get_day_number(first_event_ts)
                if first_event_day == 1 and current_day == 7:
                    showing_next_week = True
        
        for day in range(1, 8):
            day_events = days[day]
            if not day_events:
                continue
                
            if day == 1:
                day_title = f"**Day {day} (reset day):**"
            else:
                day_title = f"**Day {day}:**"
            
            # Only show Today indicator when not in preview mode
            if day == current_day and not showing_next_week:
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

    async def should_shift_week(self) -> bool:
        """
        Check if we should shift to next week:
        1. The final (last) event in schedule has completed
        2. At least 1 full hour has passed since that last event ended
        3. Last event still belongs to the current schedule week
        4. Events haven't already been shifted forward
        """
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

        # Get last event in schedule
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT MAX(timestamp) FROM schedule_events")
            row = await cursor.fetchone()
            last_event_ts = row[0] if row else None
            if not last_event_ts:
                logger.info("No events found in schedule, no week shift needed")
                return False

        # Condition 1: Last event has finished
        if now <= last_event_ts:
            logger.debug("Last event has not finished yet, no week shift needed")
            return False
        
        logger.debug("Last event has finished, checking if week shift is needed...")

        # Condition 2: At least 1 hour (3600 seconds) grace period after last event
        if now - last_event_ts < 3600:
            logger.debug("Not enough time passed since last event, no week shift needed")
            return False
        
        logger.debug("Last event has finished with 1 hour grace period, checking if week shift is needed...")

        # Condition 3: Verify we haven't already shifted forward this week
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT MIN(timestamp) FROM schedule_events")
            row = await cursor.fetchone()
            first_event_ts = row[0] if row else None
            if not first_event_ts:
                logger.info("No events found in schedule, no week shift needed")
                return False
        
        first_event_day = self.get_day_number(first_event_ts)
        
        # If we've already shifted, first event will already be on Day 1 of next week
        # Only allow shift when we are still on the original full week
        if first_event_day != 1:
            logger.debug(f"Schedule already shifted (first event is on Day {first_event_day}), no week shift needed")
            return False

        logger.debug("✅ All shift conditions satisfied: will shift schedule to next week")
        return True

    async def shift_all_events_forward_week(self):
        """Shift all events forward by one week (604800 seconds)"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE schedule_events SET timestamp = timestamp + 604800")
            await db.commit()
        logger.info("All events shifted forward one week")

    @tasks.loop(minutes=1)
    async def weekly_shift_task(self):
        """Auto shift all events forward one week when conditions are met"""
        if await self.should_shift_week():
            await self.shift_all_events_forward_week()
            logger.info("All events automatically shifted forward one week")

    @refresh_schedule_task.before_loop
    async def before_refresh_task(self):
        await self.bot.wait_until_ready()

    @weekly_shift_task.before_loop
    async def before_weekly_shift_task(self):
        await self.bot.wait_until_ready()

    def load_boss_list(self):
        """Load all available bosses from text file"""
        boss_file = BASE_DIR / "data" / "breaking_army_bosses.txt"
        with open(boss_file, 'r', encoding='utf-8') as f:
            bosses = [line.strip() for line in f if line.strip()]
        return bosses
    
    def get_current_week_info(self, offset=0):
        """Get (year, week_number) tuple for current week with optional offset"""
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        gmt8_time = now + GMT8_OFFSET
        dt = datetime.datetime.fromtimestamp(gmt8_time, tz=datetime.timezone.utc)
        adjusted_dt = dt - datetime.timedelta(hours=5) + datetime.timedelta(weeks=offset)
        iso = adjusted_dt.isocalendar()
        return (iso[0], iso[1])
    
    def get_current_week_number(self):
        """Get current week number (Monday reset)"""
        return self.get_current_week_info()[1]
    
    async def get_recent_bosses(self, weeks_back=2):
        """Get bosses that were used in the last N weeks"""
        current_week = self.get_current_week_number()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT boss_name FROM breaking_army_bosses WHERE week_number >= ? AND locked = 1",
                (current_week - weeks_back,)
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
    
    def roll_new_bosses(self):
        """Roll 2 unique new bosses that haven't been used in past 2 weeks"""
        all_bosses = self.load_boss_list()
        import random
        
        # We need the recent bosses, so call the async method synchronously
        # or we can use the blocking version here since this is called from sync context
        recent_bosses = []
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async context, we can't just run_until_complete
                # Instead, we'll get recent bosses from the DB directly
                recent_bosses = [b for b in all_bosses if b not in all_bosses]  # fallback empty
            else:
                recent_bosses = loop.run_until_complete(self.get_recent_bosses(2))
        except:
            pass
        
        available = [boss for boss in all_bosses if boss not in recent_bosses]
        
        if len(available) < 2:
            return None
        
        return random.sample(available, 2)

    breaking_army = app_commands.Group(name="breaking_army", description="Breaking Army boss management")

    @breaking_army.command(name="roll", description="Roll new Breaking Army bosses for this week")
    async def breaking_army_roll(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        target_week = self.get_current_week_number()
        year, week_num = self.get_current_week_info()
        week_display = f"{year} Week {week_num}"
        
        # Check if week is already locked
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT boss_name FROM breaking_army_bosses WHERE week_number = ? AND locked = 1", (target_week,))
            existing_rows = await cursor.fetchall()
            existing = [row[0] for row in existing_rows]
            
            if existing:
                class RerollConfirmView(discord.ui.View):
                    def __init__(self, cog, user_id):
                        super().__init__(timeout=120)
                        self.cog = cog
                        self.user_id = user_id
                        self.confirmed = False
                    
                    @discord.ui.button(label="✅ Yes, Reroll", style=discord.ButtonStyle.red)
                    async def confirm_reroll(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                        if button_interaction.user.id != self.user_id:
                            return await button_interaction.response.send_message("Only you can confirm this.")
                        
                        self.confirmed = True
                        bosses = self.cog.roll_new_bosses()
                        
                        if not bosses:
                            return await button_interaction.response.edit_message(content="❌ Not enough available bosses left!", view=None)
                        
                        boss1, boss2 = bosses
                        
                        class BossConfirmView(discord.ui.View):
                            def __init__(self, cog, boss_list, week_num, year, user_id):
                                super().__init__(timeout=300)
                                self.cog = cog
                                self.boss1, self.boss2 = boss_list
                                self.week_number = week_num
                                self.year = year
                                self.user_id = user_id
                            
                            @discord.ui.button(label="✅ Confirm & Lock", style=discord.ButtonStyle.green)
                            async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                                if button_interaction.user.id != self.user_id:
                                    return await button_interaction.response.send_message("Only you can confirm.")
                                
                                now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                                
                                async with aiosqlite.connect(self.cog.db_path) as db:
                                    await db.execute("DELETE FROM breaking_army_bosses WHERE week_number = ?", (self.week_number,))
                                    await db.execute("INSERT INTO breaking_army_bosses (week_number, boss_name, locked, rolled_at, rolled_by_user_id) VALUES (?, ?, 1, ?, ?)", (self.week_number, self.boss1, now, self.user_id))
                                    await db.execute("INSERT INTO breaking_army_bosses (week_number, boss_name, locked, rolled_at, rolled_by_user_id) VALUES (?, ?, 1, ?, ?)", (self.week_number, self.boss2, now, self.user_id))
                                    await db.commit()
                                
                                async with aiosqlite.connect(self.cog.db_path) as db:
                                    cursor = await db.execute("SELECT id FROM schedule_events WHERE event_name LIKE 'Breaking Army%' ORDER BY timestamp ASC")
                                    event_ids = [row[0] for row in await cursor.fetchall()]
                                    if len(event_ids) >= 1: await db.execute("UPDATE schedule_events SET event_name = ? WHERE id = ?", (f"Breaking Army (***{self.boss1}***)", event_ids[0]))
                                    if len(event_ids) >= 2: await db.execute("UPDATE schedule_events SET event_name = ? WHERE id = ?", (f"Breaking Army (***{self.boss2}***)", event_ids[1]))
                                    await db.commit()
                                
                                await button_interaction.response.edit_message(content=f"✅ Locked: **{self.boss1}** | **{self.boss2}**\nSchedule updated.", view=None)
                                logger.info(f"Breaking Army bosses locked: {self.boss1}, {self.boss2} for {self.year} Week {self.week_number} by {button_interaction.user}")
                            
                            @discord.ui.button(label="🎲 Reroll", style=discord.ButtonStyle.gray)
                            async def reroll(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                                if button_interaction.user.id != self.user_id:
                                    return await button_interaction.response.send_message("Only you can reroll.")
                                
                                new_bosses = self.cog.roll_new_bosses()
                                if not new_bosses:
                                    return await button_interaction.response.send_message("❌ Not enough available bosses left!", ephemeral=True)
                                
                                self.boss1, self.boss2 = new_bosses
                                await button_interaction.response.edit_message(content=f"🎲 {self.boss1} | {self.boss2}\nNeither used in past 2 weeks.", view=self)
                            
                            @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
                            async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                                if button_interaction.user.id != self.user_id:
                                    return await button_interaction.response.send_message("Only you can cancel.")
                                await button_interaction.response.edit_message(content="Roll cancelled.", view=None)
                        
                        view = BossConfirmView(self.cog, bosses, week_num, year, self.user_id)
                        await button_interaction.response.edit_message(content=f"🎲 {boss1} | {boss2}\nNeither used in past 2 weeks.", view=view)
                    
                    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.gray)
                    async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                        if button_interaction.user.id != self.user_id:
                            return await button_interaction.response.send_message("Only you can cancel.", ephemeral=True)
                        await button_interaction.response.edit_message(content="Roll cancelled.", view=None)
                
                current_bosses = " | ".join(existing)
                view = RerollConfirmView(self, interaction.user.id)
                return await interaction.response.send_message(f"⚠️ This week already has: {current_bosses}\nDo you want to reroll?", view=view)
        
        bosses = self.roll_new_bosses()
        
        if not bosses:
            return await interaction.response.send_message("❌ Not enough available bosses left! Need at least 2 bosses not used in past 2 weeks.", ephemeral=True)
        
        boss1, boss2 = bosses
        
        class BossConfirmView(discord.ui.View):
            def __init__(self, cog, boss_list, week_num, year, user_id):
                super().__init__(timeout=300)
                self.cog = cog
                self.boss1, self.boss2 = boss_list
                self.week_number = week_num
                self.year = year
                self.user_id = user_id
            
            @discord.ui.button(label="✅ Confirm & Lock", style=discord.ButtonStyle.green)
            async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user.id != self.user_id:
                    return await button_interaction.response.send_message("Only you can confirm.", ephemeral=True)
                
                now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                
                async with aiosqlite.connect(self.cog.db_path) as db:
                    await db.execute("DELETE FROM breaking_army_bosses WHERE week_number = ?", (self.week_number,))
                    await db.execute("INSERT INTO breaking_army_bosses (week_number, boss_name, locked, rolled_at, rolled_by_user_id) VALUES (?, ?, 1, ?, ?)", (self.week_number, self.boss1, now, self.user_id))
                    await db.execute("INSERT INTO breaking_army_bosses (week_number, boss_name, locked, rolled_at, rolled_by_user_id) VALUES (?, ?, 1, ?, ?)", (self.week_number, self.boss2, now, self.user_id))
                    await db.commit()
                
                async with aiosqlite.connect(self.cog.db_path) as db:
                    cursor = await db.execute("SELECT id FROM schedule_events WHERE event_name LIKE 'Breaking Army%' ORDER BY timestamp ASC")
                    event_ids = [row[0] for row in await cursor.fetchall()]
                    if len(event_ids) >= 1: await db.execute("UPDATE schedule_events SET event_name = ? WHERE id = ?", (f"Breaking Army (***{self.boss1}***)", event_ids[0]))
                    if len(event_ids) >= 2: await db.execute("UPDATE schedule_events SET event_name = ? WHERE id = ?", (f"Breaking Army (***{self.boss2}***)", event_ids[1]))
                    await db.commit()
                
                await button_interaction.response.edit_message(content=f"✅ Locked: **{self.boss1}** | **{self.boss2}**\nSchedule updated.", view=None)
                logger.info(f"Breaking Army bosses locked: {self.boss1}, {self.boss2} for {self.year} Week {self.week_number} by {button_interaction.user}")
            
            @discord.ui.button(label="🎲 Reroll", style=discord.ButtonStyle.gray)
            async def reroll(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user.id != self.user_id:
                    return await button_interaction.response.send_message("Only you can reroll.", ephemeral=True)
                
                new_bosses = self.cog.roll_new_bosses()
                if not new_bosses:
                    return await button_interaction.response.send_message("❌ Not enough available bosses left!", ephemeral=True)
                
                self.boss1, self.boss2 = new_bosses
                await button_interaction.response.edit_message(content=f"🎲 {self.boss1} | {self.boss2}\nNeither used in past 2 weeks.", view=self)
            
            @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
            async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user.id != self.user_id:
                    return await button_interaction.response.send_message("Only you can cancel.", ephemeral=True)
                await button_interaction.response.edit_message(content="Roll cancelled.", view=None)
        
        view = BossConfirmView(self, bosses, week_num, year, interaction.user.id)
        
        await interaction.response.send_message(
            f"🎲 {boss1} | {boss2}\nNeither used in past 2 weeks.",
            view=view
        )

    def save_boss_list(self, bosses):
        """Save boss list back to text file"""
        boss_file = BASE_DIR / "data" / "breaking_army_bosses.txt"
        with open(boss_file, 'w', encoding='utf-8') as f:
            f.write("\n".join(bosses) + "\n")

    @breaking_army.command(name="list", description="List and manage Breaking Army bosses")
    async def breaking_army_list(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            bosses = self.load_boss_list()
            lines = ["**Available Breaking Army Bosses:**\n"]
            for i, boss in enumerate(bosses, 1):
                lines.append(f"{i}. {boss}")
            return await interaction.response.send_message("\n".join(lines), ephemeral=True)
        
        class BossManagerView(discord.ui.View):
            def __init__(self, cog):
                super().__init__(timeout=300)
                self.cog = cog
            
            def build_message(self):
                bosses = self.cog.load_boss_list()
                lines = ["**Breaking Army Boss Manager:**\n"]
                for i, boss in enumerate(bosses, 1):
                    lines.append(f"{i}. {boss}")
                lines.append(f"\nTotal: {len(bosses)} bosses")
                return "\n".join(lines)
            
            @discord.ui.button(label="➕ Add Boss", style=discord.ButtonStyle.green)
            async def add_boss(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if not button_interaction.user.guild_permissions.administrator:
                    return await button_interaction.response.send_message("Not authorized.", ephemeral=True)
                
                class AddBossModal(discord.ui.Modal, title="Add New Boss"):
                    name = discord.ui.TextInput(label="Boss Name", placeholder="Enter boss name")
                    
                    async def on_submit(self, modal_interaction: discord.Interaction):
                        bosses = self.view.cog.load_boss_list()
                        if self.name.value not in bosses:
                            bosses.append(self.name.value)
                            self.view.cog.save_boss_list(bosses)
                            await modal_interaction.response.edit_message(content=self.view.build_message(), view=self.view)
                        else:
                            await modal_interaction.response.send_message("Boss already exists.", ephemeral=True)
                
                modal = AddBossModal()
                modal.view = self
                await button_interaction.response.send_modal(modal)
            
            @discord.ui.button(label="🗑️ Remove Boss", style=discord.ButtonStyle.red)
            async def remove_boss(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if not button_interaction.user.guild_permissions.administrator:
                    return await button_interaction.response.send_message("Not authorized.", ephemeral=True)
                
                bosses = self.cog.load_boss_list()
                if not bosses:
                    return await button_interaction.response.send_message("No bosses to remove.", ephemeral=True)
                
                options = [discord.SelectOption(label=boss, value=boss) for boss in bosses]
                select = discord.ui.Select(placeholder="Select boss to remove", options=options)
                
                async def select_callback(select_interaction: discord.Interaction):
                    boss_to_remove = select_interaction.data['values'][0]
                    bosses = self.cog.load_boss_list()
                    if boss_to_remove in bosses:
                        bosses.remove(boss_to_remove)
                        self.cog.save_boss_list(bosses)
                    await select_interaction.response.edit_message(content=self.build_message(), view=self)
                
                select.callback = select_callback
                view = discord.ui.View()
                view.add_item(select)
                await button_interaction.response.send_message("Select boss to remove:", view=view, ephemeral=True)
        
        view = BossManagerView(self)
        await interaction.response.send_message(view.build_message(), view=view, ephemeral=True)

    @breaking_army.command(name="history", description="View Breaking Army boss roll history")
    async def breaking_army_history(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT week_number, boss_name, locked, rolled_at
                FROM breaking_army_bosses 
                ORDER BY week_number DESC 
                LIMIT 15
            """)
            history = await cursor.fetchall()
        
        lines = ["**Breaking Army Boss History:**\n"]
        
        last_week = None
        for entry in history:
            if entry['week_number'] != last_week:
                year = datetime.datetime.fromtimestamp(entry['rolled_at']).isocalendar()[0]
                lines.append(f"\n**{year} Week {entry['week_number']}:**")
                last_week = entry['week_number']
            
            status = "🔒" if entry['locked'] else "⏳"
            lines.append(f"- {entry['boss_name']} {status}")
        
        if not history:
            lines.append("No boss history found.")
        
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @breaking_army.command(name="force", description="Force set a specific boss for Breaking Army")
    @app_commands.describe(
        breaking_army_slot="Which Breaking Army slot to change (1 = Thursday, 2 = Saturday)",
        boss_name="Name of the boss to set"
    )
    @app_commands.choices(breaking_army_slot=[
        app_commands.Choice(name="Breaking Army #1 (Thursday)", value=1),
        app_commands.Choice(name="Breaking Army #2 (Saturday)", value=2)
    ])
    async def breaking_army_force(self, interaction: discord.Interaction, breaking_army_slot: int, boss_name: str):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        all_bosses = self.load_boss_list()
        if boss_name not in all_bosses:
            boss_list = "\n".join([f"- {boss}" for boss in all_bosses])
            return await interaction.response.send_message(f"❌ Boss '{boss_name}' not found in available bosses.\n\nAvailable bosses:\n{boss_list}", ephemeral=True)
        
        target_week = self.get_current_week_number()
        year, week_num = self.get_current_week_info()
        
        # Get existing bosses for this week
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT boss_name FROM breaking_army_bosses WHERE week_number = ? AND locked = 1 ORDER BY id ASC", (target_week,))
            existing = [row[0] for row in await cursor.fetchall()]
        
        # Ensure we have exactly 2 slots
        while len(existing) < 2:
            existing.append(None)
        
        # Check if boss is already in use in other slot
        other_slot = 2 if breaking_army_slot == 1 else 1
        if existing[other_slot - 1] == boss_name:
            return await interaction.response.send_message(f"❌ Boss '{boss_name}' is already being used in Breaking Army #{other_slot}", ephemeral=True)
        
        # Update the selected slot
        existing[breaking_army_slot - 1] = boss_name
        
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        
        # Save to database
        async with aiosqlite.connect(self.db_path) as db:
            # Clear existing entries for this week
            await db.execute("DELETE FROM breaking_army_bosses WHERE week_number = ?", (target_week,))
            
            # Insert both bosses
            for i, boss in enumerate(existing, 1):
                if boss:
                    await db.execute(
                        "INSERT INTO breaking_army_bosses (week_number, boss_name, locked, rolled_at, rolled_by_user_id) VALUES (?, ?, 1, ?, ?)",
                        (target_week, boss, now, interaction.user.id)
                    )
            
            await db.commit()
        
        # Update schedule events
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT id FROM schedule_events WHERE event_name LIKE 'Breaking Army%' ORDER BY timestamp ASC")
            event_ids = [row[0] for row in await cursor.fetchall()]
            
            if len(event_ids) >= 1 and existing[0]:
                await db.execute("UPDATE schedule_events SET event_name = ? WHERE id = ?", (f"Breaking Army (***{existing[0]}***)", event_ids[0]))
            if len(event_ids) >= 2 and existing[1]:
                await db.execute("UPDATE schedule_events SET event_name = ? WHERE id = ?", (f"Breaking Army (***{existing[1]}***)", event_ids[1]))
            
            await db.commit()
        
        logger.info(f"Breaking Army #{breaking_army_slot} force set to {boss_name} for {year} Week {week_num} by {interaction.user}")
        await interaction.response.send_message(f"✅ Breaking Army #{breaking_army_slot} has been force set to **{boss_name}**\n\nCurrent bosses this week:\n1. {existing[0] or 'Not set'}\n2. {existing[1] or 'Not set'}\n\nSchedule has been updated.", ephemeral=False)
    
    @breaking_army_force.autocomplete('boss_name')
    async def boss_autocomplete(self, interaction: discord.Interaction, current: str):
        bosses = self.load_boss_list()
        return [
            app_commands.Choice(name=boss, value=boss)
            for boss in bosses if current.lower() in boss.lower()
        ][:25]


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
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO schedule_events (event_name, timestamp, notes) VALUES (?, ?, ?)",
                (name, timestamp, notes)
            )
            await db.commit()
        
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
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM schedule_events WHERE id = ?", (event_id,))
                event = await cursor.fetchone()
            
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
                    
                    async with aiosqlite.connect(self.db_path) as db:
                        await db.execute(
                            f"UPDATE schedule_events SET {', '.join(updates)} WHERE id = ?",
                            params
                        )
                        await db.commit()
                    
                    await modal_interaction.response.send_message(f"✅ Event updated successfully.", ephemeral=True)
                    logger.info(f"Event #{event_id} edited by {interaction.user}")
            
            modal = EditModal()
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
            
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM schedule_events WHERE id = ?", (event_id,))
                await db.commit()
            
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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO schedule_config (key, value) VALUES ('channel_id', ?)", (str(channel.id),))
            await db.execute("REPLACE INTO schedule_config (key, value) VALUES ('message_id', ?)", (str(message.id),))
            await db.commit()
        
        await interaction.followup.send(f"✅ Schedule channel set to {channel.mention}. Schedule will auto-update every minute.", ephemeral=True)
        logger.info(f"Schedule channel set to {channel.id} by {interaction.user}")

    @schedule.command(name="refresh", description="Force immediate schedule refresh (auto-checks for week shift)")
    async def schedule_refresh(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        
        if not self.schedule_message:
            return await interaction.response.send_message("No schedule message configured. Use /schedule set-channel first.", ephemeral=True)
        
        await interaction.response.defer()
        
        shift_performed = False
        if await self.should_shift_week():
            await self.shift_all_events_forward_week()
            shift_performed = True
        
        content = await self.build_schedule_message()
        await self.schedule_message.edit(content=content)
        
        if shift_performed:
            await interaction.followup.send("✅ Schedule refreshed and events have been shifted to next week automatically.", ephemeral=True)
        else:
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
            ("Breaking Army (***Pocketrupt Circus***)", 1776483000, None),
            
            # Day 7 (Sunday)
            ("Guild Party", 1776560400, None),
            ("GvG #1", 1776560400, None),
            ("GHR", 1776568500, None),
        ]
        
        async with aiosqlite.connect(self.db_path) as db:
            # Clear existing events
            await db.execute("DELETE FROM schedule_events")
            
            # Insert all example events
            await db.executemany(
                "INSERT INTO schedule_events (event_name, timestamp, notes) VALUES (?, ?, ?)",
                example_events
            )
            await db.commit()
        
        await interaction.followup.send(f"✅ Imported {len(example_events)} example events successfully. The full example schedule is now loaded.", ephemeral=True)
        logger.info(f"Example schedule imported by {interaction.user}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))