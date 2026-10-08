"""Bot-local extracted Abyss lookup and same-message leaderboard UI."""
import json,math
from pathlib import Path
from datetime import datetime,timezone
import discord
from utility.api_constants import KONGFU_WEAPON_MAP

class AbyssAssets:
    def __init__(self,root):
        self.root=Path(root)
    def data(self):
        try:return json.loads((self.root/'catalogue.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):return {'trials':[],'names':{}}
    def resolve(self,text):
        text=(text or '').strip()
        if text.lower().startswith('abyss:'):text=text.split(':',1)[1].strip()
        return next((r for r in self.data()['trials'] if str(r['id'])==text or r['name'].casefold()==text.casefold()),None)
    def image(self,entry):
        path=(self.root/entry.get('image','missing')).resolve()
        return path if path.is_relative_to(self.root.resolve()) and path.is_file() else None

def clean(value,limit=80):return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:limit]
def result_text(score,mode):
    if isinstance(score,list):score=score[0] if score else None
    if not isinstance(score,(float,int)) or not math.isfinite(score) or score==0:return '—'
    if mode=='no_hit':
        try:return datetime.fromtimestamp(abs(score),timezone.utc).strftime('%Y-%m-%d')
        except (ValueError,OverflowError,OSError):return '—'
    seconds=abs(score);return f'{int(seconds//60)}:{seconds%60:06.3f}'

class AbyssRankingView(discord.ui.LayoutView):
    def __init__(self,cog,owner,assets,entry,mode='overall',target=None):
        super().__init__(timeout=300)
        self.cog=cog;self.owner=owner;self.assets=assets;self.entry=entry;self.mode=mode;self.target=target
        self.page=1;self.rows=[];self.total=0;self.selected=0;self.target_found=None;self.error=False
        self.names=assets.data()['names']
        self.rank_name=entry['no_hit_rank_name'] if mode=='no_hit' else entry['overall_rank_name']
        if mode=='path':
            paths=entry.get('paths') or []
            if entry.get('special_no_path_ui') or len(paths)!=1 or not paths[0].get('name') or not paths[0].get('rank_name'):
                raise ValueError('No unambiguous extracted Path Trial for this boss')
            self.rank_name=paths[0]['rank_name']
    async def interaction_check(self,interaction):
        if interaction.user.id==self.owner:return True
        await interaction.response.send_message('Only the command requester can browse this leaderboard.',ephemeral=True);return False
    async def load(self,page=1,jump=False):
        from utility.wwm import get_rank_list
        if jump and self.target:
            probe=await get_rank_list(self.rank_name,page=1,pid=self.target)
            rank=((probe or {}).get('result') or {}).get('my_rank',-1)
            self.target_found=(isinstance(rank,int) and rank>=0) if (probe or {}).get('code')==0 else None
            if self.target_found:page=rank//20+1
        response=await get_rank_list(self.rank_name,page=max(1,page),pid=self.target)
        self.error=not response or response.get('code')!=0
        data=(response or {}).get('result') or {}
        try:self.total=max(0,int(data.get('rank_total_len') or 0))
        except (TypeError,ValueError):self.total=0
        maxpage=max(1,math.ceil(self.total/20))
        if not self.error and page>maxpage:
            response=await get_rank_list(self.rank_name,page=maxpage,pid=self.target)
            self.error=not response or response.get('code')!=0;data=(response or {}).get('result') or {};page=maxpage
        self.rows=[r for r in (data.get('rank_list') or []) if isinstance(r,dict)][:20] if not self.error else [];self.page=max(1,page);self.selected=0
        if self.target:self.selected=next((i for i,r in enumerate(self.rows) if r.get('pid')==self.target),0)
        self.rebuild()
    def attachments(self):
        path=self.assets.image(self.entry)
        return [discord.File(path,filename='abyss.png')] if path else []
    def mapped(self,kind,values):
        names = self.names.get(kind, {})
        if kind == 'martial':
            # Reuse the same martial-art labels as player profiles. Extracted
            # names take precedence; unknown encounter-specific IDs stay visible.
            names = {**{str(k):v['name'] for k,v in KONGFU_WEAPON_MAP.items() if v.get('name')}, **names}
        return ', '.join('Empty' if v==0 else clean(names.get(str(v),f'ID {v}'),38) for v in values) or 'Not recorded'
    def rebuild(self):
        self.clear_items()
        mode={'overall':'Fastest clears','no_hit':'No-hit completion dates','path':'Path Trial · '+clean((self.entry.get('paths') or [{}])[0].get('name',''))}[self.mode]
        items=[]
        if self.assets.image(self.entry):
            gallery=discord.ui.MediaGallery();gallery.add_item(media='attachment://abyss.png');items.append(gallery)
        items.append(discord.ui.TextDisplay(f'## ⚔️ Abyss Trial · {clean(self.entry["name"])}\n**{mode}**'+f'\n-# Trial {self.entry["id"]} · Boss {self.entry["boss_recall_id"]} · {self.total} ranked entries'))
        if self.mode=='path':items.append(discord.ui.TextDisplay('Boss-specific path leaderboard · ranked by clear time. Recorded skills below reflect the actual run, including encounter-specific variations.'))
        if self.target_found is False:items.append(discord.ui.TextDisplay('The requested player was not returned on this board. This does not prove they never completed it.'))
        if self.error:items.append(discord.ui.TextDisplay('⚠️ The leaderboard API is unavailable. Try again later.'))
        elif not self.rows:items.append(discord.ui.TextDisplay('No ranked entries returned. This board may be empty, unavailable in this region, or not yet released.'))
        else:
            lines=[]
            for i,r in enumerate(self.rows):
                base=(r.get('player_info') or {}).get('base') or {}
                marker=' ★' if r.get('pid')==self.target else ''
                lines.append(f'**{(self.page-1)*20+i+1}.** {clean(base.get("nickname") or r.get("pid"),32)}{marker} · **{result_text(r.get("score"),self.mode)}**')
            items.append(discord.ui.TextDisplay('\n'.join(lines)))
            r=self.rows[self.selected];ud=r.get('ud') or {};skills=ud.get('battle_skills')
            base=(r.get('player_info') or {}).get('base') or {}
            detail=f'### Recorded build · {clean(base.get("nickname") or r.get("pid"),50)}\nPlayer `{clean(base.get("number_id") or r.get("pid"))}` · Result **{result_text(r.get("score"),self.mode)}**\n'
            if isinstance(skills,list):
                detail+=f'**Martial arts:** {self.mapped("martial",skills[:2])}\n**Inner Ways:** {self.mapped("inner",skills[3:7])}\n**Mystic Skills:** {self.mapped("mystic",skills[7:])}\n'
            else:detail+='Build was not recorded.\n'
            if ud.get('device_name'):detail+=f'**Platform:** {clean(ud["device_name"],30)}'
            items.append(discord.ui.Separator());items.append(discord.ui.TextDisplay(detail))
        items.append(discord.ui.TextDisplay('-# Saved ranked-run loadouts, not current gear or damage statistics. Unresolved skill IDs remain visible. No-hit dates use UTC; other results are clear times.'))
        self.add_item(discord.ui.Container(*items,accent_color=0x26A69A))
        if self.rows:
            menu=discord.ui.Select(placeholder='Inspect a ranked player’s recorded build',options=[discord.SelectOption(label=f'{(self.page-1)*20+i+1}. '+str(((r.get('player_info') or {}).get('base') or {}).get('nickname') or r.get('pid'))[:85],value=str(i),default=i==self.selected) for i,r in enumerate(self.rows)])
            async def inspect(interaction):
                self.selected=int(menu.values[0]);self.rebuild();await interaction.response.edit_message(view=self)
            menu.callback=inspect;self.add_item(discord.ui.ActionRow(menu))
        buttons=[]
        for label,step in [('← Previous',-1),('Next →',1)]:
            button=discord.ui.Button(label=label,disabled=self.error or not 1<=self.page+step<=max(1,math.ceil(self.total/20)))
            async def turn(interaction,delta=step):
                await interaction.response.defer();await self.load(self.page+delta)
                await interaction.edit_original_response(view=self,attachments=self.attachments())
            button.callback=turn;buttons.append(button)
        self.add_item(discord.ui.ActionRow(*buttons))
