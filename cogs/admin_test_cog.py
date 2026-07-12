"""
Admin Cog for WWM Guild Bot
============================
JSON Tree Explorer with Select menus for interactive affix mapping.
"""

import discord
import asyncio
from cogs.market_cog import is_admin_or_staff
import settings
import io
from typing import Any, Dict, List, Optional
from discord.ext import commands
from discord import app_commands
from settings import logger, WWM_REDIS_PLAYER_URL, WWM_UID
from utility.wwm import _wwm_api_post, get_player_info, find_people_by_nickname, ALL_KNOWN_FIELDS
from utility import affix_mapper
import json


def _prepare_for_json_sort(obj):
    if isinstance(obj, dict):
        return {str(k): _prepare_for_json_sort(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_prepare_for_json_sort(item) for item in obj]
    return obj


def _format_value_preview(value: Any, max_len: int = 40) -> str:
    if isinstance(value, dict):
        return f"dict ({len(value)} keys)"
    elif isinstance(value, list):
        return f"list ({len(value)} items)"
    elif isinstance(value, str):
        v = f'"{value}"'
        return v[:max_len] + "..." if len(v) > max_len else v
    elif isinstance(value, bool):
        return str(value)
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        return f"{value:.4f}" if value != int(value) else str(int(value))
    elif value is None:
        return "null"
    return str(value)[:max_len]


async def _get_affix_name(affix_id: int) -> Optional[str]:
    mapping = await affix_mapper.get_mapping(affix_id)
    return mapping["name"] if mapping else None


def _looks_like_affix_id(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        val = int(value)
        return 10_000 <= val <= 99_999_999
    if isinstance(value, str) and value.isdigit():
        val = int(value)
        return 10_000 <= val <= 99_999_999
    return False


# ===========================================================================
# MODAL: Map an affix ID
# ===========================================================================
class AffixMapModal(discord.ui.Modal):
    def __init__(self, explorer: "DataExplorerView", affix_id: int, value: Any, id_path: str):
        super().__init__(title=f"Map affix {affix_id}")  # Keep under 45 chars
        self.explorer = explorer
        self.affix_id = affix_id
        self.affix_value = value
        self.id_path = id_path

        val_str = str(value)[:50] if value is not None else "?"
        self.add_item(discord.ui.TextInput(
            label=f"Name (value: {val_str})"[:45],
            placeholder="e.g. Min Stonesplit Attack",
            max_length=100, required=True,
        ))
        cat_placeholder = "e.g. weapon, armor, tone"
        hints = {"wear_equips": "equipment", "base_affixes": "base_affix",
                 "tone_determin": "tone", "det_history": "determin", "retone": "retone"}
        for kw, cat in hints.items():
            if kw in id_path:
                cat_placeholder = f"e.g. {cat}"
        self.add_item(discord.ui.TextInput(
            label="Category", placeholder=cat_placeholder,
            max_length=50, required=False,
        ))
        self.add_item(discord.ui.TextInput(
            label="Description", placeholder="Short description",
            max_length=300, required=False, style=discord.TextStyle.paragraph,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        name = self.children[0].value.strip()
        category = self.children[1].value.strip()
        description = self.children[2].value.strip()
        if not name:
            return await interaction.response.send_message("❌ Name cannot be empty!", ephemeral=True)
        success = await affix_mapper.add_mapping(self.affix_id, name, category, description)
        if success:
            embed = discord.Embed(title="✅ Affix Mapped",
                                  description=f"`{self.affix_id}` → **{name}**", color=discord.Color.green())
            if category: embed.add_field(name="Category", value=category, inline=True)
            if description: embed.add_field(name="Description", value=description, inline=True)
        else:
            await affix_mapper.edit_mapping(self.affix_id, name=name, category=category or None, description=description or None)
            embed = discord.Embed(title="✏️ Affix Updated",
                                  description=f"`{self.affix_id}` → **{name}**", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ===========================================================================
# TREE EXPLORER VIEW
# ===========================================================================
class DataExplorerView(discord.ui.View):
    def __init__(self, data: Any, source_name: str, pid: str, player_name: str,
                 path: Optional[List[str]] = None):
        super().__init__(timeout=300)
        self.root_data = data
        self.source_name = source_name
        self.pid = pid
        self.player_name = player_name
        self.path: List[str] = path or []
        self.page = 0
        self.items_per_page = 15
        self._rebuild_ui()

    def _navigate_to(self, key: str) -> "DataExplorerView":
        return DataExplorerView(self.root_data, self.source_name, self.pid, self.player_name,
                                self.path + [key])

    def _go_back(self) -> Optional["DataExplorerView"]:
        if not self.path:
            return None
        return DataExplorerView(self.root_data, self.source_name, self.pid, self.player_name,
                                self.path[:-1])

    @property
    def current_node(self) -> Any:
        node = self.root_data
        for key in self.path:
            if node is None:
                return None
            if isinstance(node, dict):
                # Try exact key first, then try other representations
                if key in node:
                    node = node[key]
                else:
                    # Maybe key was quoted or formatted differently
                    found = False
                    for actual_key in node:
                        if str(actual_key) == str(key) or repr(actual_key) == repr(key):
                            node = node[actual_key]
                            found = True
                            break
                    if not found:
                        logger.debug(f"Key '{key}' not found in dict with keys: {list(node.keys())[:10]}")
                        return None
            elif isinstance(node, list):
                try:
                    node = node[int(key)]
                except (ValueError, IndexError):
                    logger.debug(f"Index '{key}' not found in list of len {len(node)}")
                    return None
            else:
                return None
        return node

    @property
    def path_str(self) -> str:
        return "." + ".".join(self.path) if self.path else "."

    async def _build_embed(self) -> discord.Embed:
        node = self.current_node
        if node is None:
            return discord.Embed(title="Error", description="Invalid path", color=discord.Color.red())
        embed = discord.Embed(
            title=f"🗺️ {self.source_name} Explorer",
            description=f"**Player:** {self.player_name} (`{self.pid}`)\n**📍 Path:** `{self.path_str}`",
            color=discord.Color.blue(),
        )
        if isinstance(node, dict):
            items = list(node.items())
            total = len(items)
            start = self.page * self.items_per_page
            end = min(start + self.items_per_page, total)
            embed.add_field(name="📁 Contents", value=f"{total} key(s) — {start+1}-{end}", inline=False)
            for key, value in items[start:end]:
                preview = _format_value_preview(value)
                ks = str(key)[:60]
                aid = None
                if isinstance(value, (int, float)) and _looks_like_affix_id(value):
                    aid = int(value)
                elif isinstance(value, list) and len(value) >= 2 and _looks_like_affix_id(value[0]):
                    aid = int(value[0])
                if aid is not None:
                    nm = await _get_affix_name(aid)
                    if nm:
                        embed.add_field(name=f"🗺️ `{ks}`", value=f"**{nm}** `{preview}`", inline=False)
                    else:
                        embed.add_field(name=f"🔢 `{ks}`", value=f"Affix `{aid}` `{preview}`", inline=False)
                elif isinstance(value, (dict, list)):
                    embed.add_field(name=f"📁 `{ks}`", value=f"`{preview}`", inline=False)
                else:
                    embed.add_field(name=f"📝 `{ks}`", value=f"`{preview}`", inline=False)
        elif isinstance(node, list):
            total = len(node)
            start = self.page * self.items_per_page
            end = min(start + self.items_per_page, total)
            embed.add_field(name="📋 Contents", value=f"{total} item(s) — {start+1}-{end}", inline=False)
            for idx, value in list(enumerate(node))[start:end]:
                preview = _format_value_preview(value)
                aid = None
                if isinstance(value, (int, float)) and _looks_like_affix_id(value):
                    aid = int(value)
                elif isinstance(value, list) and len(value) >= 2 and _looks_like_affix_id(value[0]):
                    aid = int(value[0])
                if aid is not None:
                    nm = await _get_affix_name(aid)
                    if nm:
                        embed.add_field(name=f"🗺️ `[{idx}]`", value=f"**{nm}** `{preview}`", inline=False)
                    else:
                        embed.add_field(name=f"🔢 `[{idx}]`", value=f"Affix `{aid}` `{preview}`", inline=False)
                elif isinstance(value, (dict, list)):
                    embed.add_field(name=f"📁 `[{idx}]`", value=f"`{preview}`", inline=False)
                else:
                    embed.add_field(name=f"📝 `[{idx}]`", value=f"`{preview}`", inline=False)
        else:
            embed.add_field(name="🔢 Value", value=f"`{_format_value_preview(node)}`", inline=False)
            if isinstance(node, (int, float)) and _looks_like_affix_id(node):
                nm = await _get_affix_name(int(node))
                if nm:
                    embed.add_field(name="🗺️ Mapped As", value=nm, inline=False)
        total_pages = max(1, -(-(len(node) if isinstance(node, (dict, list)) else 1) // self.items_per_page))
        embed.set_footer(text=f"Page {self.page + 1}/{total_pages}")
        return embed

    def _rebuild_ui(self):
        self.clear_items()
        node = self.current_node

        # Row 0: Back, Send Raw, Cancel, page nav
        if self.path:
            self.add_item(ExplorerButton("⬅️ Back", "back", discord.ButtonStyle.secondary, self))
        self.add_item(ExplorerButton("📄 Send Raw", "send_raw", discord.ButtonStyle.secondary, self))
        self.add_item(ExplorerButton("❌ Cancel", "cancel", discord.ButtonStyle.danger, self))
        if isinstance(node, (dict, list)):
            total = len(node)
            m = max(0, (total - 1) // self.items_per_page)
            if self.page > 0:
                self.add_item(ExplorerButton("◀ Prev", "prev", discord.ButtonStyle.primary, self))
            if self.page < m:
                self.add_item(ExplorerButton("Next ▶", "next", discord.ButtonStyle.primary, self))

        # Row 1: Navigate dropdown
        if isinstance(node, dict):
            opts = []
            keys = list(node.keys())
            st = self.page * self.items_per_page
            en = st + self.items_per_page
            for k in keys[st:en]:
                v = node[k]
                if isinstance(v, (dict, list)):
                    opts.append(discord.SelectOption(
                        label=f"📁 {str(k)[:80]}", description=_format_value_preview(v)[:50],
                        value=f"enter:{k}"
                    ))
            if opts:
                s = discord.ui.Select(placeholder="📁 Navigate into...", options=opts[:25], row=1)
                s.callback = self._make_nav_cb(s)
                self.add_item(s)

        # Row 2: Map dropdown
        map_opts = []
        if isinstance(node, dict):
            for k, v in node.items():
                aid = self._detect_affix(v)
                if aid is not None:
                    map_opts.append(discord.SelectOption(
                        label=f"🗺️ {str(k)[:60]}", description=f"ID: {aid}",
                        value=f"map:{k}:{aid}"
                    ))
        elif isinstance(node, list):
            st = self.page * self.items_per_page
            en = st + self.items_per_page
            for idx, v in enumerate(node):
                if st <= idx < en:
                    aid = self._detect_affix(v)
                    if aid is not None:
                        map_opts.append(discord.SelectOption(
                            label=f"🗺️ [{idx}]", description=f"ID: {aid}",
                            value=f"map:{idx}:{aid}"
                        ))
        if map_opts:
            s = discord.ui.Select(placeholder="🗺️ Map an affix...", options=map_opts[:25], row=2)
            s.callback = self._make_map_cb(s)
            self.add_item(s)

    def _detect_affix(self, value: Any) -> Optional[int]:
        if isinstance(value, (int, float)) and _looks_like_affix_id(value):
            return int(value)
        if isinstance(value, list) and len(value) >= 2 and _looks_like_affix_id(value[0]):
            return int(value[0])
        return None

    def _make_nav_cb(self, select: discord.ui.Select):
        async def cb(interaction: discord.Interaction):
            val = select.values[0]
            if val.startswith("enter:"):
                child = self._navigate_to(val[6:])
                embed = await child._build_embed()
                child._rebuild_ui()
                await interaction.response.edit_message(embed=embed, view=child)
        return cb

    def _make_map_cb(self, select: discord.ui.Select):
        async def cb(interaction: discord.Interaction):
            val = select.values[0]
            if val.startswith("map:"):
                # Format: map:{key}:{affix_id}  (e.g. "map:0:9213001")
                # Use rsplit to handle any colons in dict keys
                _, k, aid_str = val.split(":", 2)
                aid = int(aid_str)
                node = self.current_node
                v = None
                if isinstance(node, dict) and k in node:
                    v = node[k]
                elif isinstance(node, list):
                    try:
                        v = node[int(k)]
                    except (ValueError, IndexError):
                        pass
                modal = AffixMapModal(self, aid, v, f"{self.path_str}.{k}")
                await interaction.response.send_modal(modal)
        return cb

    async def handle_button(self, interaction: discord.Interaction, cid: str):
        if cid == "cancel":
            em = discord.Embed(title="⏹️ Explorer Closed", description="No file was sent.", color=discord.Color.red())
            for c in self.children:
                c.disabled = True
            return await interaction.response.edit_message(embed=em, view=self)
        if cid == "send_raw":
            b = json.dumps(_prepare_for_json_sort(self.root_data), indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()
            fn = f"{self.source_name.lower().replace(' ', '_')}_{self.pid}.json"
            em = discord.Embed(title="📄 Raw Data Sent", color=discord.Color.light_gray())
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(embed=em, view=self)
            return await interaction.followup.send(
                content=f"📄 **{self.source_name}** — {self.player_name} (`{self.pid}`)",
                file=discord.File(io.BytesIO(b), filename=fn))
        if cid == "back":
            p = self._go_back()
            if p:
                embed = await p._build_embed()
                p._rebuild_ui()
                return await interaction.response.edit_message(embed=embed, view=p)
        if cid == "prev":
            self.page = max(0, self.page - 1)
            self._rebuild_ui()
            embed = await self._build_embed()
            return await interaction.response.edit_message(embed=embed, view=self)
        if cid == "next":
            t = len(self.current_node) if isinstance(self.current_node, (dict, list)) else 0
            m = max(0, (t - 1) // self.items_per_page)
            self.page = min(m, self.page + 1)
            self._rebuild_ui()
            embed = await self._build_embed()
            return await interaction.response.edit_message(embed=embed, view=self)


class ExplorerButton(discord.ui.Button):
    def __init__(self, label: str, cid: str, style: discord.ButtonStyle, explorer: DataExplorerView):
        super().__init__(label=label, style=style, row=0)
        self._exp = explorer
        self._cid = cid

    async def callback(self, interaction: discord.Interaction):
        await self._exp.handle_button(interaction, self._cid)


# ===========================================================================
# COG
# ===========================================================================
class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._known_fields = ALL_KNOWN_FIELDS

    async def _resolve_player(self, identifier: str):
        if identifier.isdigit() and len(identifier) == 10:
            r = await get_player_info(identifier, fields=["base"], force_search=True)
            if r and r.get('result') and r['result'].get('id'):
                return r['result'], r['result']['id'], r['result'].get('hostnum', 10595)
        r = await find_people_by_nickname(identifier, force_search=True)
        if r and r.get('result'):
            return r['result'], r['result']['id'], r['result'].get('hostnum', 10595)
        return None, None, None

    async def _parse_field_list(self, fs: str) -> list:
        if not fs or not fs.strip():
            return ["base"]
        return list(dict.fromkeys(f.strip() for f in fs.split(",") if f.strip())) or ["base"]

    async def mode_autocomplete(self, i, c):
        return [app_commands.Choice(name="all_fields", value="all_fields"),
                app_commands.Choice(name="custom", value="custom")]

    async def fields_autocomplete(self, i, c):
        parts = c.rsplit(",", 1)
        prefix = parts[0] + "," if len(parts) > 1 else ""
        seg = parts[-1].strip().lower() if parts else ""
        suggestions = []
        if seg:
            for f in self._known_fields:
                if seg in f.lower():
                    suggestions.append(app_commands.Choice(name=f"{prefix}{f}", value=f"{prefix}{f}"))
                    if len(suggestions) >= 25:
                        break
        else:
            suggestions.append(app_commands.Choice(name="← All known fields", value="all"))
            for f in self._known_fields[:24]:
                suggestions.append(app_commands.Choice(name=f, value=f))
        return suggestions[:25]

    @app_commands.command(name="list_game_history", description="List game history for a specific player")
    async def list_game_history(self, i: discord.Interaction, type: int, id: str, sub_type: int = None):
        if not await is_admin_or_staff(i):
            return await i.response.send_message("No permission.", ephemeral=True)
        await i.response.defer()
        p = await get_player_info(id, fields=["base"])
        if not p:
            return await i.followup.send(f"Not found: {id}")
        av = p["result"]["id"]
        eid = av[:-1] + chr(ord(av[-1]) + 2)
        payload = {"type": type, "entity_id": eid, "avatar": av, "start": 0, "uid": "1"}
        if sub_type:
            payload["sub_type"] = sub_type
        result = await _wwm_api_post(settings.WWM_LIST_GAME_HISTORY_URL, payload)
        if result:
            await i.followup.send(file=discord.File(
                io.BytesIO(json.dumps(_prepare_for_json_sort(result), indent=4,
                                      ensure_ascii=False, sort_keys=True, default=str).encode()),
                "game_history.json"))
        else:
            await i.followup.send(f"No game history for {id}")
        await i.edit_original_response(content=f"Game history for {id} sent.", view=None)

    @app_commands.describe(
        identifier="Player name or number ID",
        mode="all_fields or custom",
        fields="Comma-separated fields (custom mode)",
        mapped="🗺️ Open tree explorer to view/map affix IDs",
    )
    @app_commands.autocomplete(mode=mode_autocomplete, fields=fields_autocomplete)
    @app_commands.command(name="get_player_data", description="Fetch player data with selectable fields")
    async def get_player_data(self, i: discord.Interaction, identifier: str, mode: str,
                              fields: str = None, mapped: bool = False):
        if not await is_admin_or_staff(i):
            return await i.response.send_message("No permission.", ephemeral=True)
        await i.response.defer()
        r, pid, hn = await self._resolve_player(identifier)
        if not r or not pid:
            return await i.followup.send(f"❌ Not found: {identifier}")
        name = r.get("name", r.get("nickname", identifier))
        hn = r.get("hostnum", 10595)
        fl = ALL_KNOWN_FIELDS if mode == "all_fields" else (await self._parse_field_list(fields) if fields else ["base"])
        if "all" in [f.lower() for f in fl]:
            fl = ALL_KNOWN_FIELDS
        raw = await _wwm_api_post(WWM_REDIS_PLAYER_URL,
                                   {"fields": fl, "hostnum2pids": {hn: [pid]}, "uid": WWM_UID, "token": "1"},
                                   timeout=30)
        if not raw or not isinstance(raw, dict):
            return await i.followup.send("❌ API error.")
        pd = {}
        if raw.get('result') and isinstance(raw['result'], dict):
            rd = raw['result']
            pd = rd.get(pid) or rd.get(next(iter(rd), "")) or {}
        if not pd:
            pd = raw
        out = {"query": {"identifier": identifier, "name": name, "pid": pid}, "data": pd}
        if not mapped:
            b = json.dumps(_prepare_for_json_sort(out), indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()
            return await i.followup.send(content=f"✅ **{name}** (`{pid}`)",
                                          file=discord.File(io.BytesIO(b), f"player_data_{pid}.json"))
        x = DataExplorerView(out, "Player Data", pid, name)
        await i.followup.send(embed=await x._build_embed(), view=x)

    @app_commands.describe(identifier="Player identifier", mapped="🗺️ Open tree explorer")
    @app_commands.command(name="get_player_combat_plan", description="Fetch a player's combat plan")
    async def get_player_combat_plan(self, i: discord.Interaction, identifier: str, mapped: bool = False):
        if not await is_admin_or_staff(i):
            return await i.response.send_message("No permission.", ephemeral=True)
        await i.response.defer()
        r, pid, hn = await self._resolve_player(identifier)
        if not r or not pid:
            return await i.followup.send(f"❌ Not found: {identifier}")
        name = r.get("name", r.get("nickname", identifier))
        resp = await _wwm_api_post(settings.WWM_GET_PLAYER_COMBAT_PLAN_URL,
                                    {"uid": WWM_UID, "pid": pid, "hostnum": hn}, timeout=30)
        if not resp or not isinstance(resp, dict):
            return await i.followup.send("❌ API error.")
        if not mapped:
            b = json.dumps(_prepare_for_json_sort(resp), indent=4, ensure_ascii=False, sort_keys=True, default=str).encode()
            return await i.followup.send(content=f"✅ **{name}** (`{pid}`)",
                                          file=discord.File(io.BytesIO(b), f"combat_plan_{pid}.json"))
        x = DataExplorerView(resp, "Combat Plan", pid, name)
        await i.followup.send(embed=await x._build_embed(), view=x)


async def setup(bot: commands.Bot):
    await affix_mapper.init_db()
    await bot.add_cog(AdminCog(bot))