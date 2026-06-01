import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, Separator, ActionRow
from settings import logger

# Color palette
BLURPLE = 0x5865F2

# Accent colors for each category (cycling through a set)
CATEGORY_COLORS = [
    0x5865F2,  # Blurple
    0x2ECC71,  # Green
    0xE67E22,  # Orange
    0x9B59B6,  # Purple
    0xE74C3C,  # Red
    0x1ABC9C,  # Teal
    0xF39C12,  # Yellow
    0x3498DB,  # Blue
    0x979C9F,  # Gray
]


class HelpLayoutView(LayoutView):
    """Components V2 LayoutView for the universal help command.
    
    Automatically discovers all registered slash commands grouped by cog.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot
        self.cog_data = {}  # cog_name -> list of commands
        self._discover_commands()
        self._build_overview()

    def _discover_commands(self):
        """Walk the bot's command tree and group commands by cog."""
        self.cog_data = {}
        
        for cmd in self.bot.tree.walk_commands():
            # Determine which cog this command belongs to
            cog_name = "Other"
            
            # Try to get the cog via the command's parent or binding
            # cmd.binding may be a cog instance or class depending on discord.py version
            cog_instance = getattr(cmd, 'cog', None)
            if cog_instance is None and hasattr(cmd, 'binding'):
                binding = cmd.binding
                if binding is not None:
                    # binding might be a class or an instance
                    if isinstance(binding, type):
                        # It's a class — find the matching cog instance
                        for name, cog in self.bot.cogs.items():
                            if isinstance(cog, binding):
                                cog_instance = cog
                                break
                    else:
                        # It's already an instance
                        cog_instance = binding
            
            if cog_instance is not None:
                # Use the cog's name from bot.cogs dict for a clean display name
                for name, cog in self.bot.cogs.items():
                    if cog is cog_instance:
                        friendly_name = name.replace("Cog", "").replace("_", " ").strip().title()
                        cog_name = friendly_name
                        break
                else:
                    # Fallback to class name
                    cog_name = type(cog_instance).__name__.replace("Cog", "").strip().title()
            
            if cog_name not in self.cog_data:
                self.cog_data[cog_name] = []
            
            # Build a description of the command
            cmd_desc = {
                'name': cmd.qualified_name,
                'description': cmd.description or 'No description',
                'parameters': [],
                'is_parent': isinstance(cmd, app_commands.Group),
                'children': [],
            }
            
            # Extract parameter info
            if hasattr(cmd, 'parameters'):
                for param in cmd.parameters:
                    if not param.required:
                        cmd_desc['parameters'].append(f"[{param.name}]")
                    else:
                        cmd_desc['parameters'].append(f"<{param.name}>")
            
            # If it's a group, collect children
            if isinstance(cmd, app_commands.Group):
                for child in cmd.commands:
                    child_desc = f"**`/{child.qualified_name}`**"
                    if child.description:
                        child_desc += f" — {child.description}"
                    cmd_desc['children'].append(child_desc)
            
            self.cog_data[cog_name].append(cmd_desc)

    def _build_overview(self):
        """Build the overview/home page with buttons for each cog."""
        self.clear_items()
        inner_items = []
        
        inner_items.append(TextDisplay(
            "# 🤖 **Bot Command Help**\n\n"
            "Select a category below to view available commands and usage."
        ))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        # List all categories with command counts
        sorted_cogs = sorted(self.cog_data.keys())
        overview_lines = []
        category_buttons_data = []
        
        for i, cog_name in enumerate(sorted_cogs):
            cmds = self.cog_data[cog_name]
            # Count top-level commands + group children
            top_count = len(cmds)
            emoji = self._get_cog_emoji(cog_name)
            overview_lines.append(f"**{emoji} {cog_name}** — {top_count} command(s)")
            category_buttons_data.append(cog_name)
        
        overview_text = "\n".join(overview_lines)
        inner_items.append(TextDisplay(overview_text))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        master = Container(*inner_items, accent_color=BLURPLE)
        self.add_item(master)
        
        # Splits buttons into rows of up to 5
        rows_needed = (len(category_buttons_data) + 4) // 5
        for row_idx in range(rows_needed):
            row = ActionRow()
            start = row_idx * 5
            end = start + 5
            for cog_name in category_buttons_data[start:end]:
                color_idx = sorted_cogs.index(cog_name) % len(self._get_button_styles())
                style = self._get_button_styles()[color_idx]
                btn = discord.ui.Button(
                    label=f"{self._get_cog_emoji(cog_name)} {cog_name}",
                    style=style,
                    custom_id=f"help_cat_{cog_name}"
                )
                btn.callback = self._make_category_callback(cog_name)
                row.add_item(btn)
            self.add_item(row)

    def _build_category_page(self, cog_name: str):
        """Build a page showing all commands for a specific cog."""
        self.clear_items()
        inner_items = []
        
        emoji = self._get_cog_emoji(cog_name)
        inner_items.append(TextDisplay(
            f"# {emoji} **{cog_name} Commands**"
        ))
        inner_items.append(Separator(spacing=discord.SeparatorSpacing.small))
        
        cmds = self.cog_data.get(cog_name, [])
        cmd_lines = []
        
        for cmd in cmds:
            if cmd['is_parent']:
                # Group command: show the group name and its children
                param_str = " ".join(cmd['parameters'])
                if param_str:
                    cmd_lines.append(f"**`/{cmd['name']} {param_str}`**")
                else:
                    cmd_lines.append(f"**`/{cmd['name']}`**")
                
                if cmd['description']:
                    cmd_lines.append(f"> {cmd['description']}")
                
                if cmd['children']:
                    for child in cmd['children']:
                        cmd_lines.append(f"• {child}")
                cmd_lines.append("")
            else:
                # Regular command
                param_str = " ".join(cmd['parameters'])
                if param_str:
                    cmd_lines.append(f"**`/{cmd['name']} {param_str}`**")
                else:
                    cmd_lines.append(f"**`/{cmd['name']}`**")
                
                if cmd['description']:
                    cmd_lines.append(f"> {cmd['description']}")
                cmd_lines.append("")
        
        if cmd_lines:
            inner_items.append(TextDisplay("\n".join(cmd_lines).strip()))
        else:
            inner_items.append(TextDisplay("No commands found in this category."))
        
        color_idx = sorted(self.cog_data.keys()).index(cog_name) % len(CATEGORY_COLORS)
        master = Container(*inner_items, accent_color=CATEGORY_COLORS[color_idx])
        self.add_item(master)
        
        self._add_back_row()

    def _add_back_row(self):
        """Add a back button row to return to the overview."""
        row = ActionRow()
        back_btn = discord.ui.Button(
            label="🔙 Back to Overview",
            style=discord.ButtonStyle.primary,
            custom_id="help_back"
        )
        back_btn.callback = self._make_back_callback()
        row.add_item(back_btn)
        self.add_item(row)

    def _get_cog_emoji(self, cog_name: str) -> str:
        """Return an appropriate emoji for a cog based on its name."""
        name_lower = cog_name.lower()
        if 'player' in name_lower or 'wwm' in name_lower:
            return '👤'
        elif 'guild' in name_lower:
            return '🏰'
        elif 'schedule' in name_lower or 'event' in name_lower:
            return '📅'
        elif 'music' in name_lower or 'song' in name_lower:
            return '🎵'
        elif 'admin' in name_lower or 'staff' in name_lower:
            return '⚙️'
        elif 'activity' in name_lower or 'leaderboard' in name_lower:
            return '🏆'
        elif 'translate' in name_lower or 'language' in name_lower:
            return '🌐'
        elif 'role' in name_lower:
            return '🎭'
        elif 'presence' in name_lower or 'status' in name_lower:
            return '🟢'
        elif 'reminder' in name_lower:
            return '⏰'
        elif 'sticky' in name_lower:
            return '📌'
        elif 'verify' in name_lower or 'verification' in name_lower:
            return '✅'
        elif 'basic' in name_lower or 'general' in name_lower:
            return 'ℹ️'
        elif 'help' in name_lower:
            return '❓'
        elif 'log' in name_lower:
            return '📋'
        elif 'custom' in name_lower:
            return '🔧'
        elif 'music' in name_lower:
            return '🎵'
        elif 'live' in name_lower or 'chat' in name_lower:
            return '💬'
        else:
            return '📁'

    def _get_button_styles(self):
        """Return a cycle of button styles for categories."""
        return [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.danger,
        ]

    def _make_category_callback(self, cog_name: str):
        async def callback(interaction: discord.Interaction):
            self._build_category_page(cog_name)
            await interaction.response.edit_message(view=self)
        return callback

    def _make_back_callback(self):
        async def callback(interaction: discord.Interaction):
            self._build_overview()
            await interaction.response.edit_message(view=self)
        return callback


class HelpCog(commands.Cog):
    """Cog that provides a universal help command using Components V2."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="View all available bot commands grouped by category")
    async def help_command(self, interaction: discord.Interaction):
        """Display a universal help overview using Components V2 layout."""
        view = HelpLayoutView(self.bot)
        await interaction.response.send_message(view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))