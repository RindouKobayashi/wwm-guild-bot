"""Player-scoped battle details displayed in the original profile message."""
import math
import asyncio
import discord

def safe(text,limit=200):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))[:limit]

def metric(value):
    if isinstance(value,bool): return 'Yes' if value else 'No'
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '—'
    return f'{value:,.1f}'.rstrip('0').rstrip('.') if isinstance(value,float) else f'{value:,}'

def battle_embed(data, nickname, number, index=0):
    matches=data['matches']
    embed=discord.Embed(title='Guild battles · '+safe(nickname,100),color=0x26A69A)
    scope='Current guild’s returned records only; not arena history or lifetime history.'
    if data['status']=='no_guild': embed.description='This player is not currently in a guild. Previous guild history cannot be discovered through this lookup.'
    elif data['status']=='unavailable': embed.description='The game API is unavailable or did not return this player’s guild data. Try again later.'
    elif data['status']=='no_records': embed.description='The game returned no battle IDs for this player’s current guild. No participation check was possible; this does not mean the player has never participated.'
    elif not matches: embed.description=f'No returned battle report contains this player. Checked {data["scanned"]} guild reports. The player may not have participated, or older records may no longer be retained.' + ('\n⚠️ The lookup is incomplete; some reports could not be checked.' if data.get('partial') else '')
    else:
        match=matches[index];stats=match['stats'];opponent=match.get('opponent_id')
        when=f'<t:{int(match["timestamp"])}:f>' if match['timestamp'] else 'Date unavailable'
        opponent_name=data.get('names',{}).get(opponent) or ('Unresolved guild · '+str(opponent) if opponent else 'Unavailable')
        embed.description=f'**{safe(nickname)}** · Player `{safe(number)}`\n{when} · **{match["outcome"]}**\nOpponent: {safe(opponent_name)}\nTeam: {match["team"] or "Unavailable"} · Season {safe(match["season"])} · Round {safe(match["round"])}'
        for key,label in [('total_damage','Damage dealt'),('damage_taken','Damage taken'),('healing_done','Healing'),('kills','Kills'),('deaths','Deaths'),('assists','Assists'),('damage_wall','Wall damage'),('damage_goose','Objective damage'),('baiye_token','Battle resources')]:
            embed.add_field(name=label,value=metric(stats.get(key)),inline=True)
        embed.add_field(name='Recorded extra fields',value='\n'.join(f'`{safe(k,60)}`: {metric(v)}' for k,v in stats.items() if k not in {'total_damage','damage_taken','healing_done','kills','deaths','assists','damage_wall','damage_goose','baiye_token','camp','hostnum'} and isinstance(v,(int,float,bool)))[:1000] or 'None',inline=False)
        embed.add_field(name='Coverage',value=f'{len(matches)} matches containing this player / {data["scanned"]} reports checked.\n'+scope+ ('\n⚠️ Some reports were unavailable or outside the 100-report scan limit.' if data.get('partial') else ''),inline=False)
        embed.add_field(name='Reading these statistics',value='— = not recorded. Objective/resource units are unverified. No per-skill damage breakdown is supplied. Opponent names are current public metadata.',inline=False)
    embed.set_footer(text=f'Match {index+1}/{len(matches)} · {scope}' if matches else scope)
    return embed

class PlayerGuildBattleView(discord.ui.LayoutView):
    def __init__(self,owner_id,data,nickname,number,authorize,back=None):
        super().__init__(timeout=300)
        self.owner_id=owner_id;self.data=data;self.nickname=nickname;self.number=number;self.authorize=authorize
        self.back=back
        self.details=False
        self.roster_page=0
        self.participant=None
        self._match_id=None
        self.index=0;self.page=0;self.rebuild()

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message('Only the requesting user can browse these reports.',ephemeral=True);return False
        return await self.authorize(interaction)

    def embed(self): return battle_embed(self.data,self.nickname,self.number,self.index)

    async def prepare(self):
        if not self.data['matches']:return
        match=self.data['matches'][self.index]
        if self._match_id!=match['id']:
            self.roster_page=0;self.participant=None;self._match_id=match['id']
        from utility.guild_battles import load_participant_names
        try:await asyncio.wait_for(load_participant_names(match),timeout=20)
        except (Exception,asyncio.TimeoutError):pass
        if match.get('player_pid'):
            match.setdefault('player_names',{})[match['player_pid']]={'name':self.nickname,'number':self.number}
        self.rebuild()

    async def update(self,interaction):
        await interaction.response.defer()
        await self.prepare()
        if not await self.authorize(interaction):return
        await interaction.edit_original_response(view=self)

    def rebuild(self):
        self.clear_items();matches=self.data['matches']
        embed=self.embed()
        accent=0x26A69A
        items=[discord.ui.TextDisplay(f'## ⚔️ Guild battles\n**{safe(self.nickname)}** · Player `{safe(self.number)}`')]
        if matches:
            match=matches[self.index];stats=match['stats']
            wins=sum(m['outcome']=='Win' for m in matches)
            losses=sum(m['outcome']=='Loss' for m in matches)
            unknown=len(matches)-wins-losses
            record=f'**{len(matches)}** recorded matches · **{wins}W / {losses}L**'
            if unknown: record+=f' · {unknown} undecided'
            items.append(discord.ui.TextDisplay(record))
            items.append(discord.ui.Separator())
            outcome=match['outcome']
            accent=0x43B581 if outcome=='Win' else 0xE57373 if outcome=='Loss' else 0xC5A969
            icon='🟢' if outcome=='Win' else '🔴' if outcome=='Loss' else '⚪'
            opponent=self.data.get('names',{}).get(match.get('opponent_id')) or 'Guild name unavailable'
            when=f'<t:{int(match["timestamp"])}:f>' if match['timestamp'] else 'Date unavailable'
            items.append(discord.ui.TextDisplay(f'### {icon} {outcome} · vs {safe(opponent,100)}\n{when}\n-# Match {self.index+1} of {len(matches)} · {match["team"] or "Unknown"} team · Season {safe(match["season"])} / Round {safe(match["round"])}'))
            kda=' / '.join(metric(stats.get(k)) for k in ('kills','deaths','assists'))
            items.append(discord.ui.TextDisplay('### Player performance\n'
                f'**Damage dealt**  {metric(stats.get("total_damage"))}\n'
                f'**Damage taken**  {metric(stats.get("damage_taken"))}\n'
                f'**Healing**  {metric(stats.get("healing_done"))}\n'
                f'**Kills / Deaths / Assists**  {kda}'))
            objectives=[f'**{label}:** {metric(stats[key])}' for key,label in [('damage_wall','Wall damage'),('damage_goose','Objective damage'),('baiye_token','Battle resources')] if key in stats]
            if objectives: items.append(discord.ui.TextDisplay('### Objectives & resources\n'+'\n'.join(objectives)))
            if self.details:
                items.append(discord.ui.Separator())
                for field in embed.fields:
                    if field.name in ('Recorded extra fields','Coverage','Reading these statistics'):
                        items.append(discord.ui.TextDisplay('**'+field.name+'**\n'+field.value))
                items.append(discord.ui.TextDisplay(f'**Battle ID**\n`{safe(match["id"],100)}`'))
                if match.get('opponent_id'):
                    items.append(discord.ui.TextDisplay(f'**Opponent guild ID**\n`{safe(match["opponent_id"],100)}`'))
            items.append(discord.ui.TextDisplay('-# — = not recorded · Current guild history only'+ (' · ⚠️ Incomplete lookup' if self.data.get('partial') else '')))
        else:
            items.extend([discord.ui.Separator(),discord.ui.TextDisplay(embed.description)])
        self.add_item(discord.ui.Container(*items,accent_color=accent))
        utility_buttons=[]
        if self.back:
            button=discord.ui.Button(label='Back to player profile')
            button.callback=self.back
            utility_buttons.append(button)
        if matches:
            toggle=discord.ui.Button(label='Hide report details' if self.details else 'Report details',style=discord.ButtonStyle.secondary)
            async def toggle_details(interaction):
                self.details=not self.details;self.rebuild();await interaction.response.edit_message(view=self)
            toggle.callback=toggle_details;utility_buttons.append(toggle)
        if utility_buttons:self.add_item(discord.ui.ActionRow(*utility_buttons))
        if not matches:return
        start=self.page*25
        options=[]
        for i,m in enumerate(matches[start:start+25],start):
            from datetime import datetime,timezone
            date=datetime.fromtimestamp(m['timestamp'],timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if m['timestamp'] else 'Unknown date'
            opponent=self.data.get('names',{}).get(m.get('opponent_id')) or 'Guild name unavailable'
            options.append(discord.SelectOption(label=f'{i+1}. {m["outcome"]} · vs {opponent}'[:100],
                description=f'{date} · Season {m["season"]} / Round {m["round"]}'[:100],
                emoji='🟢' if m['outcome']=='Win' else '🔴' if m['outcome']=='Loss' else '⚪',value=str(i),default=i==self.index))
        menu=discord.ui.Select(placeholder=f'Matches {start+1}–{min(start+25,len(matches))} · Choose a battle',options=options)
        async def choose(interaction):
            self.index=int(menu.values[0]);await self.update(interaction)
        menu.callback=choose;self.add_item(discord.ui.ActionRow(menu))
        buttons=[]
        for label,delta in [('← Newer battle',-1),('Older battle →',1)]:
            button=discord.ui.Button(label=label,disabled=not 0<=self.index+delta<len(matches))
            async def move(interaction,step=delta):
                self.index+=step;self.page=self.index//25;await self.update(interaction)
            button.callback=move;buttons.append(button)
        for label,delta in [('≪ Newer 25',-1),('Older 25 ≫',1)]:
            button=discord.ui.Button(label=label,disabled=self.page+delta<0 or (self.page+delta)*25>=len(matches))
            async def change(interaction,step=delta):
                self.page+=step;self.index=self.page*25;await self.update(interaction)
            button.callback=change;buttons.append(button)
        self.add_item(discord.ui.ActionRow(*buttons))
        self.build_roster()

    def build_roster(self):
        match=self.data['matches'][self.index]
        roster=match.get('participants',{})
        if not roster:return
        names=match.get('player_names',{})
        def name(pid):return names.get(pid,{}).get('name') or 'ID '+pid
        def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
        entries=sorted(roster.items(),key=lambda entry:(entry[0]!=match.get('player_pid'),entry[1].get('camp') if entry[1].get('camp') in (1,2) else 3,-entry[1].get('total_damage',0) if finite(entry[1].get('total_damage')) else 1))
        items=[]
        for color,camp,emoji in [('Red',1,'🔴'),('Blue',2,'🔵')]:
            team=[s for _,s in entries if s.get('camp')==camp]
            totals=[]
            for key,label in [('total_damage','Damage'),('healing_done','Healing')]:
                values=[s[key] for s in team if finite(s.get(key))]
                totals.append(f'{label} **{metric(sum(values)) if values else "—"}** ({len(values)}/{len(team)})')
            guild=self.data.get('names',{}).get(match.get('guilds',{}).get(color)) or color+' guild'
            score=match.get('scores',{}).get(color,[None,None])
            items.append(discord.ui.TextDisplay(f'### {emoji} {safe(guild,45)} · {len(team)} players\n'+' · '.join(totals)+f'\n-# Score {metric(score[0])} → {metric(score[1])} · Totals cover returned fields only'))
        start=self.roster_page*6;visible=entries[start:start+6]
        lines=[]
        for pid,s in visible:
            icon='🔴' if s.get('camp')==1 else '🔵' if s.get('camp')==2 else '⚪'
            marker=' ★ Searched player' if pid==match.get('player_pid') else ''
            kda='/'.join(metric(s.get(k)) for k in ('kills','deaths','assists'))
            lines.append(f'{icon} **{safe(name(pid),30)}**{marker}\nDamage {metric(s.get("total_damage"))} · Taken {metric(s.get("damage_taken"))} · Heal {metric(s.get("healing_done"))} · K/D/A {kda}')
        items.append(discord.ui.TextDisplay(f'### Match roster · {start+1}–{min(start+6,len(entries))} / {len(entries)}\n'+'\n'.join(lines)))
        if self.participant in roster:
            s=roster[self.participant]
            fields=[('total_damage','Damage'),('damage_taken','Taken'),('healing_done','Healing'),('kills','Kills'),('deaths','Deaths'),('assists','Assists'),('damage_wall','Wall'),('damage_goose','Objective'),('baiye_token','Resources')]
            items.append(discord.ui.TextDisplay(f'### Inspect · {safe(name(self.participant),40)}\n'+' · '.join(f'{label} **{metric(s.get(k))}**' for k,label in fields)+f'\n-# Player {safe(names.get(self.participant,{}).get("number") or self.participant,60)}'))
        items.append(discord.ui.TextDisplay('-# Names are current public metadata. ★ marks the searched player. Unknown teams stay unassigned.'))
        self.add_item(discord.ui.Container(*items,accent_color=0x5865F2))
        menu=discord.ui.Select(placeholder='Inspect a participant on this roster page',options=[discord.SelectOption(label=name(pid)[:100],value=pid,description=('Searched player · ' if pid==match.get('player_pid') else '')+('Red team' if s.get('camp')==1 else 'Blue team' if s.get('camp')==2 else 'Team unavailable'),default=pid==self.participant) for pid,s in visible])
        async def inspect(interaction):
            self.participant=menu.values[0];self.rebuild();await interaction.response.edit_message(view=self)
        menu.callback=inspect;self.add_item(discord.ui.ActionRow(menu))
        buttons=[]
        for label,step in [('← Roster',-1),('Roster →',1)]:
            button=discord.ui.Button(label=label,disabled=not 0<=(self.roster_page+step)*6<len(entries))
            async def turn(interaction,delta=step):
                self.roster_page+=delta;self.participant=None;self.rebuild();await interaction.response.edit_message(view=self)
            button.callback=turn;buttons.append(button)
        self.add_item(discord.ui.ActionRow(*buttons))
