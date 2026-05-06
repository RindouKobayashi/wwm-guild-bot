import discord
import settings
import sqlite3
import random
from discord.ext import commands
from discord import app_commands, ButtonStyle
from settings import logger, BASE_DIR, WWM_UID, WWM_TOKEN, WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, CLUB_ID
from datetime import datetime
from utility.wwm import get_player_info, get_club_hostnums

DB_PATH = BASE_DIR / "data" / "guild_verification.db"

class GuildVerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.init_database()
        self.load_config()

    def init_database(self):
        """Initialize database tables"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Configuration table
        c.execute('''
            CREATE TABLE IF NOT EXISTS verification_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # Verification requests history
        c.execute('''
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
            c.execute("ALTER TABLE verification_requests ADD COLUMN verification_code TEXT")
            conn.commit()
        except:
            # Column already exists
            pass
        
        # Approved members registry
        c.execute('''
            CREATE TABLE IF NOT EXISTS verified_members (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                character_uid TEXT NOT NULL,
                verified_at TIMESTAMP NOT NULL,
                verified_by INTEGER NOT NULL
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("Guild Verification database initialized")
    
    def load_config(self):
        """Load configuration from database into runtime"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT key, value FROM verification_config")
        config = dict(c.fetchall())
        conn.close()
        
        for key, value in config.items():
            if value.isdigit():
                setattr(settings, key, int(value))
            else:
                setattr(settings, key, value)
        
        logger.info(f"Loaded {len(config)} configuration entries from database")

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("✅ Guild Verification cog ready")
        logger.info("✅ Persistent views already registered in setup_hook")

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
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        result = None
        if member:
            c.execute("SELECT user_id, username, character_uid, verified_at, verified_by FROM verified_members WHERE user_id = ?", (member.id,))
            result = c.fetchone()
        
        if character_uid and not result:
            c.execute("SELECT user_id, username, character_uid, verified_at, verified_by FROM verified_members WHERE character_uid = ?", (character_uid.strip(),))
            result = c.fetchone()
        
        conn.close()
        
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
            # Convert stored UTC timestamp properly
            from datetime import timezone
            verified_dt = datetime.fromisoformat(result[3]).replace(tzinfo=timezone.utc)
            verified_timestamp = int(verified_dt.timestamp())
            embed.add_field(name="Verified At", value=f"<t:{verified_timestamp}>", inline=False)
            embed.add_field(name="Verified By", value=f"<@{result[4]}>", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"Member lookup performed by {interaction.user}")

    @app_commands.command(name="add-verified-member", description="Manually add a verified guild member")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        member="The discord member to add",
        character_uid="The in-game character UID of this member"
    )
    async def add_verified_member(self, interaction: discord.Interaction, member: discord.Member, character_uid: str):
        """Admin command to manually add existing members to verified database"""
        
        # Add to verified members database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute('''
            REPLACE INTO verified_members
            (user_id, username, character_uid, verified_at, verified_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            member.id,
            str(member),
            character_uid.strip(),
            datetime.utcnow(),
            interaction.user.id
        ))
        
        conn.commit()
        conn.close()
        
        # Assign guild member role if configured
        if hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
            guild_role = interaction.guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
            if guild_role and guild_role not in member.roles:
                await member.add_roles(guild_role)
        
        embed = discord.Embed(
            title="✅ Member Added Successfully",
            description=f"Member {member.mention} has been added to the verified members registry.\n\n"
                       f"**Character UID:** `{character_uid.strip()}`",
            color=discord.Color.green()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"Manual member added: {member} | UID: {character_uid} | by {interaction.user}")

    @app_commands.command(name="setup-verification", description="Start guild verification system setup wizard")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_verification(self, interaction: discord.Interaction):
        """Admin command to start the verification setup wizard"""
        logger.info(f"Verification setup wizard started by {interaction.user}")
        
        # Check if existing configuration exists
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM verification_config WHERE key = 'GUILD_VERIFICATION_CHANNEL_ID'")
        existing_config = c.fetchone()
        conn.close()
        
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

class ExistingConfigCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="Replace Existing", style=ButtonStyle.danger, emoji="🔄")
    async def replace_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Delete old configuration
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM verification_config")
        conn.commit()
        conn.close()
        
        # Clear runtime settings
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
        
        # Pagination buttons
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
        
        # Pagination buttons
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
        
        # Pagination buttons
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
        
        # Pagination buttons
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
        
        # Pagination buttons
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
        
        # Save configuration to database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        for key, value in self.config.items():
            c.execute("REPLACE INTO verification_config (key, value) VALUES (?, ?)", (key, str(value)))
            setattr(settings, key, value)
        
        conn.commit()
        conn.close()
        
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
        
        # Save message ID to database for persistence
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("REPLACE INTO verification_config (key, value) VALUES (?, ?)", ("VERIFICATION_MESSAGE_ID", str(message.id)))
        conn.commit()
        conn.close()
        
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
        
        # Validate Character UID format
        if not uid.isdigit() or len(uid) != 10:
            await interaction.response.send_message(
                "❌ **Invalid Character UID**\n\nCharacter UID must be exactly 10 numbers.\nPlease try again with a valid UID.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Fetch player info from API
            player_data = get_player_info(uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            
            if not player_data or 'result' not in player_data:
                await interaction.followup.send(
                    "❌ Failed to retrieve character information. Please verify the UID and try again.",
                    ephemeral=True
                )
                return
            
            player = player_data.get('result', {})
            nickname = player.get('base', {}).get('nickname', 'Unknown')
            level = player.get('base', {}).get('level', 0)
            
            # Check guild membership
            is_guild_member = False
            player_pid = player.get('id')
            
            if player_pid:
                club_data = get_club_hostnums(player_pid)
                if club_data and 'result' in club_data:
                    player_club_data = club_data['result'].get(player_pid, {})
                    club_id = player_club_data.get('club', {}).get('club_id')
                    is_guild_member = (club_id == CLUB_ID)
            
            # Show confirmation embed
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
            # Re-fetch live player profile
            player_data = get_player_info(self.character_uid, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            
            if not player_data or 'result' not in player_data:
                await interaction.followup.send(
                    "❌ Failed to retrieve character information. Please try again later.",
                    ephemeral=True
                )
                return
            
            player = player_data.get('result', {})
            # Signature is stored under name_card field NOT base
            name_card = player.get('name_card', {})
            signature = name_card.get('sign', '')
            
            # Check if verification code exists anywhere in signature
            if self.verify_code in str(signature):
                # ✅ Verification successful!
                target_user = interaction.guild.get_member(self.user_id)
                
                if target_user:
                    # Assign correct role
                    if self.is_member and hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
                        guild_role = interaction.guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
                        if guild_role:
                            await target_user.add_roles(guild_role)
                    elif hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID'):
                        community_role = interaction.guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
                        if community_role:
                            await target_user.add_roles(community_role)
                
                # Add to verified members database
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''
                    REPLACE INTO verified_members
                    (user_id, username, character_uid, verified_at, verified_by)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    self.user_id,
                    self.username,
                    self.character_uid,
                    datetime.utcnow(),
                    self.user_id # Verified automatically by user themselves
                ))
                conn.commit()
                conn.close()
                
                await interaction.followup.send(
                    "✅ **Verification Successful!**\n\n"
                    "Your account has been successfully bound and verified.\n"
                    "You now have access to all member features.\n\n"
                    "You may now remove the code from your signature if you wish.",
                    ephemeral=True
                )
                
                logger.info(f"Automatic verification completed for user {self.username} | Character UID: {self.character_uid}")
            
            else:
                # ❌ Code not found
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
        # Fix for persistent view restoration: extract user_id from embed if not in instance
        if not self.user_id:
            # Extract from message embed fields
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
        
        # Get is_member status from embed
        is_member = False
        for field in interaction.message.embeds[0].fields:
            if field.name == "Guild Member":
                is_member = ("✅" in field.value)
        
        # Assign correct role based on guild membership
        if is_member and hasattr(settings, 'GUILD_MEMBER_ROLE_ID'):
            guild_role = interaction.guild.get_role(settings.GUILD_MEMBER_ROLE_ID)
            if guild_role:
                await target_user.add_roles(guild_role)
                logger.info(f"Guild member role assigned to {target_user} by {interaction.user}")
        elif hasattr(settings, 'COMMUNITY_MEMBER_ROLE_ID'):
            community_role = interaction.guild.get_role(settings.COMMUNITY_MEMBER_ROLE_ID)
            if community_role:
                await target_user.add_roles(community_role)
                logger.info(f"Community member role assigned to {target_user} by {interaction.user}")
        
        # Update database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Update request status
        c.execute('''
            UPDATE verification_requests
            SET status = 'approved', admin_id = ?, processed_at = ?
            WHERE user_id = ? AND status = 'pending'
        ''', (interaction.user.id, datetime.utcnow(), self.user_id))
        
        # Add to verified members registry
        c.execute('''
            REPLACE INTO verified_members
            (user_id, username, character_uid, verified_at, verified_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            self.user_id,
            self.username,
            self.character_uid,
            datetime.utcnow(),
            interaction.user.id
        ))
        
        conn.commit()
        conn.close()
        
        # Update original message
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Status", value=f"✅ **APPROVED** by {interaction.user.mention}", inline=False)
        
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("✅ Verification approved and role assigned!", ephemeral=True)
        
        # Send DM notification to user
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
        # Check if user has admin role
        admin_role = interaction.guild.get_role(settings.GUILD_ADMIN_ROLE_ID)
        if not admin_role or admin_role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ You are not authorized to reject verification requests.",
                ephemeral=True
            )
            return
        
        # Open reject reason modal
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
        
        # Get target user
        target_user = interaction.guild.get_member(self.user_id)
        
        # Update original admin message
        embed = self.original_message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Status", value=f"❌ **REJECTED** by {interaction.user.mention}", inline=False)
        embed.add_field(name="Reason", value=f"`{reject_reason}`", inline=False)
        
        await self.original_message.edit(embed=embed, view=None)
        await interaction.response.send_message("✅ Verification rejected with reason!", ephemeral=True)
        
        # Try to DM the user with reason
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

async def setup(bot: commands.Bot):
    await bot.add_cog(GuildVerificationCog(bot))