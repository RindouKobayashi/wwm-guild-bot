import discord
import aiosqlite
import asyncio
import datetime
from discord.ext import commands, tasks
from discord import app_commands
from settings import logger, BASE_DIR

GMT8_OFFSET = 8 * 3600  # 8 hours in seconds

class ReminderPreviewView(discord.ui.View):
    """A view that allows users to cycle through upcoming Guild Party reminders."""
    def __init__(self, cog, events: list):
        super().__init__(timeout=120)
        self.cog = cog
        self.events = events  # List of event rows
        self.current_index = 0

    async def update_message(self, interaction: discord.Interaction):
        event = self.events[self.current_index]
        embed = await self.cog.create_reminder_embed(event)
        
        # Update the text to show progress (e.g., "Previewing 1 of 5")
        description = f"**Previewing {self.current_index + 1} of {len(self.events)} upcoming Guild Party reminders.**"
        
        # We edit the existing message with the new embed and text
        await interaction.response.edit_message(embed=embed, content=description, view=self)

    @discord.ui.button(label="⬅️ Previous", style=discord.ButtonStyle.gray)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index = (self.current_index - 1) % len(self.events)
        await self.update_message(interaction)

    @discord.ui.button(label="Next ➡️", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index = (self.current_index + 1) % len(self.events)
        await self.update_message(interaction)


class ReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = BASE_DIR / "data" / "schedule.db"
        self.reminder_channel = None
        self.reminder_message_id = None
        self.ping_target = "" # Stores @everyone or <@&role_id>
        self.delete_old_reminder = True 

    async def cog_load(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS reminder_config (key TEXT PRIMARY KEY, value TEXT)")
            await db.commit()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT value FROM reminder_config WHERE key = 'channel_id'")
            row = await cursor.fetchone()
            if row: self.reminder_channel = self.bot.get_channel(int(row[0]))

            cursor = await db.execute("SELECT value FROM reminder_config WHERE key = 'message_id'")
            row = await cursor.fetchone()
            if row and self.reminder_channel: self.reminder_message_id = int(row[0])

            cursor = await db.execute("SELECT value FROM reminder_config WHERE key = 'delete_old'")
            row = await cursor.fetchone()
            if row: self.delete_old_reminder = row[0].lower() == 'true'
            
            cursor = await db.execute("SELECT value FROM reminder_config WHERE key = 'ping_target'")
            row = await cursor.fetchone()
            if row: self.ping_target = row[0]

        self.daily_reminder_task.start()

    async def cog_unload(self):
        self.daily_reminder_task.cancel()

    def get_day_number(self, timestamp: int) -> int:
        gmt8_time = timestamp + GMT8_OFFSET
        dt = datetime.datetime.fromtimestamp(gmt8_time, tz=datetime.timezone.utc)
        adjusted_dt = dt - datetime.timedelta(hours=5)
        return adjusted_dt.weekday() + 1

    def get_day_label(self, day_num: int) -> str:
        """Returns the custom formatted Day name."""
        labels = {
            1: "Day 1 of the week (Reset)",
            2: "Day 2",
            3: "Day 3",
            4: "Day 4",
            5: "Day 5",
            6: "Day 6",
            7: "Day 7 (Last day before reset)"
        }
        return labels.get(day_num, f"Day {day_num}")

    async def create_reminder_embed(self, gp_event) -> discord.Embed:
        """The core logic for generating the embed content."""
        gp_ts = gp_event['timestamp']
        day_num = self.get_day_number(gp_ts)
        day_label = self.get_day_label(day_num)
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM schedule_events WHERE timestamp > ? ORDER BY timestamp ASC", (gp_ts,))
            future_events = await cursor.fetchall()

            embed = discord.Embed(
                title=f"📅 Upcoming Events: {day_label}",
                description=f"Reminder: **{gp_event['event_name']}** is starting in <t:{gp_ts}:R>!",
                color=discord.Color.blue()
            )
            
            events_found = False
            for event in future_events:
                if self.get_day_number(event['timestamp']) == day_num:
                    ts = event['timestamp']
                    # Use Discord relative timestamp for the countdown effect
                    embed.add_field(name=f"• {event['event_name']}", value=f"Time: <t:{ts}:R>", inline=False)
                    events_found = True
            
            if not events_found:
                embed.add_field(name="No other events", value="There are no further events scheduled for the rest of today.", inline=False)

            embed.set_footer(text="Check the above message for full schedule.")
        return embed

    @tasks.loop(minutes=1)
    async def daily_reminder_task(self):
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        target_ts = now + 900 

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM schedule_events WHERE event_name LIKE ? AND timestamp BETWEEN ? AND ?",
                ("%Guild Party%", target_ts - 30, target_ts + 30)
            )
            gp_event = await cursor.fetchone()

            if gp_event:
                await self.send_live_reminder(gp_event)

    async def send_live_reminder(self, gp_event):
        """Sends the actual live notification to the channel."""
        if not self.reminder_channel: return

        embed = await self.create_reminder_embed(gp_event)
        # Put the ping in the message content so it actually pings people
        content = f"{self.ping_target}\n" if self.ping_target else None

        try:
            if self.delete_old_reminder and self.reminder_message_id:
                try:
                    old_msg = await self.reminder_channel.fetch_message(self.reminder_message_id)
                    # We must pass content here too, otherwise the ping disappears on edit
                    await old_msg.edit(content=content, embed=embed)
                    return
                except discord.NotFound: pass

            new_msg = await self.reminder_channel.send(content=content, embed=embed)
            if self.delete_old_reminder:
                self.reminder_message_id = new_msg.id
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("REPLACE INTO reminder_config (key, value) VALUES ('message_id', ?)", (str(new_msg.id),))
                    await db.commit()
        except Exception as e:
            logger.error(f"Failed to send live reminder: {e}")

    # --- Admin Commands ---
    reminder_manager = app_commands.Group(name="reminder_manager", description="Manage daily reminders")

    @reminder_manager.command(name="set-ping", description="Set the ping target for reminders (e.g. @everyone or <@&ROLE_ID>)")
    async def reminder_set_ping(self, interaction: discord.Interaction, ping_target: str):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Unauthorized.", ephemeral=True)
        
        self.ping_target = ping_target
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO reminder_config (key, value) VALUES ('ping_target', ?)", (ping_target,))
            await db.commit()
        
        await interaction.response.send_message(f"✅ Reminder ping target set to: {ping_target}", ephemeral=False)

    @reminder_manager.command(name="preview_week", description="Preview all upcoming Guild Party reminders for the week")
    async def reminder_preview(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Unauthorized.", ephemeral=False)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM schedule_events WHERE event_name LIKE ? ORDER BY timestamp ASC", ("%Guild Party%",))
            all_gp_events = await cursor.fetchall()

        if not all_gp_events:
            return await interaction.response.send_message("No upcoming Guild Party events found in the schedule.", ephemeral=False)

        view = ReminderPreviewView(self, all_gp_events)
        embed = await self.create_reminder_embed(all_gp_events[0])
        
        await interaction.response.send_message(
            content="**Previewing 1 of {} upcoming Guild Party reminders.**".format(len(all_gp_events)),
            embed=embed,
            view=view,
            ephemeral=False
        )

    @reminder_manager.command(name="view", description="View current reminder configuration")
    async def reminder_view(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Unauthorized.", ephemeral=True)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM reminder_config")
            rows = await cursor.fetchall()
            config_text = "\n".join([f"**{r['key']}**: `{r['value']}`" for r in rows]) if rows else "No config."

            channel_str = f"<#{self.reminder_channel.id}>" if self.reminder_channel else "Not set"
            ping_str = f"{self.ping_target}" if self.ping_target else "None"
            
            embed = discord.Embed(title="Reminder Configuration", description=config_text, color=discord.Color.gold())
            embed.add_field(name="Target Channel", value=channel_str)
            embed.add_field(name="Ping Target", value=ping_str)
            embed.add_field(name="Replace Old Message", value="Yes" if self.delete_old_reminder else "No")
            await interaction.response.send_message(embed=embed, ephemeral=False)

    @reminder_manager.command(name="set-channel", description="Set the channel for daily reminders")
    async def reminder_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator: return
        self.reminder_channel = channel
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO reminder_config (key, value) VALUES ('channel_id', ?)", (str(channel.id),))
            await db.commit()
        await interaction.response.send_message(f"✅ Reminder channel set to {channel.mention}", ephemeral=False)

    @reminder_manager.command(name="toggle-replace", description="Toggle whether to replace the old reminder or send a new one")
    async def reminder_toggle_replace(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator: return
        self.delete_old_reminder = not self.delete_old_reminder
        status = "Replace" if self.delete_old_reminder else "Append"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO reminder_config (key, value) VALUES ('delete_old', ?)", (str(self.delete_old_reminder).lower(),))
            await db.commit()
        await interaction.response.send_message(f"✅ Reminder mode changed to: **{status}**", ephemeral=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(ReminderCog(bot))
