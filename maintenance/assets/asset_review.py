"""Offline, field-level review of public game exports."""
import html
import json
from pathlib import Path

from utility.game_assets import digest, inside


def fields(before, after, prefix=''):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            name = prefix + '.' + key if prefix else key
            if key not in before or key not in after:
                result.append({'field': name, 'before': before.get(key), 'after': after.get(key),
                               'before_present': key in before, 'after_present': key in after})
            else:
                result.extend(fields(before[key], after[key], name))
        return result
    return [] if before == after else [{'field': prefix or 'value', 'before': before, 'after': after,
                                       'before_present': True, 'after_present': True}]


def sections(tables, directory=None):
    result = dict(tables.get('player_mappings', {}))
    for key in ('item_index', 'martial_names'):
        if key in tables:
            result[key] = tables[key]
    for key, value in tables.get('api_constants', {}).items():
        if isinstance(value, dict) and key != 'sources':
            result['api_constants.' + key] = value
    result = {key: value for key, value in result.items() if isinstance(value, dict)}
    if directory:
        directory = Path(directory)
        catalogue = directory / 'activity/catalogue.json'
        if catalogue.exists():
            boards = json.loads(catalogue.read_text(encoding='utf-8'))['boards']
            result['ranking_boards'] = {}
            for board in boards:
                row = dict(board)
                # Compare image content, not just its stable URL or filename.
                if row.get('image'):
                    relative = 'activity/' + row['image'].lstrip('/')
                    row['artwork_sha256'] = digest(inside(directory, relative))
                result['ranking_boards'][row['key']] = row
        catalogue = directory / 'abyss/catalogue.json'
        if catalogue.exists():
            result['abyss_names'] = json.loads(catalogue.read_text(encoding='utf-8')).get('names', {})
    return result


def label(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ('name', 'title', 'english_name'):
            if value.get(key):
                return str(value[key])
        return ' · '.join(str(item) for item in value.values() if isinstance(item, str))[:180]
    return ''


def review(old, new, old_directory=None, new_directory=None):
    before, after = sections(old, old_directory), sections(new, new_directory)
    records, summary = [], {}
    for section in sorted(before.keys() | after.keys()):
        left, right = before.get(section, {}), after.get(section, {})
        counts = {'added': 0, 'changed': 0, 'removed': 0, 'unchanged': 0}
        for identifier in sorted(left.keys() | right.keys()):
            a, b = left.get(identifier), right.get(identifier)
            state = 'added' if identifier not in left else 'removed' if identifier not in right else 'unchanged' if a == b else 'changed'
            counts[state] += 1
            if state == 'unchanged':
                continue
            records.append({'section': section, 'id': identifier, 'status': state,
                            'name': label(b) or label(a), 'before': a, 'after': b,
                            'fields': fields(a, b)})
        summary[section] = counts
    return {'baseline_available': bool(old), 'summary': summary, 'records': records}


def render(report):
    payload = json.dumps(report, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>WWM asset review</title><style>
body{font:15px system-ui;background:#101b20;color:#e2e9e7;margin:0;padding:30px;max-width:1400px;margin:auto}h1{color:#e8cd8e}p{line-height:1.6;color:#b6c8c8}select,input,button{padding:10px;background:#1c3037;color:inherit;border:1px solid #45616a;border-radius:5px}nav{display:flex;gap:12px;flex-wrap:wrap;position:sticky;top:0;background:#101b20;padding:15px 0}table{width:100%;border-collapse:collapse;margin:20px 0}td,th{text-align:left;padding:9px;border-bottom:1px solid #30434b}article{border:1px solid #38515a;border-radius:7px;padding:16px;margin:14px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px ui-monospace,monospace;background:#17282e;padding:12px}small{color:#b6c8c8}.added{color:#78d6af}.removed{color:#f49a99}.changed{color:#e8cd8e}.pair{display:grid;grid-template-columns:1fr 1fr;gap:15px}@media(max-width:650px){.pair{grid-template-columns:1fr}body{padding:14px}}summary{cursor:pointer}</style>
<h1>WWM asset review</h1><p>Comparison against the currently published bundle. “Removed” means absent from this export, not proof that the game deleted it. Names and category-scoped item identities are preserved. Artwork changes compare SHA-256 content hashes. No baseline means additions are an inventory, not confirmed patch additions.</p>
<div id="context"></div><div id="totals"></div><nav><input id="search" placeholder="Search ID, name or changed field"><select id="section"><option value="">All sections</option></select><select id="status"><option value="">All changes</option><option>added</option><option>changed</option><option>removed</option></select></nav><p id="count"></p><div id="records"></div><button id="more">Show next 100</button><details><summary>Source provenance and remaining limitations</summary><pre id="provenance"></pre></details>
<script type="application/json" id="data">''' + payload + '''</script><script>
const data=JSON.parse(document.getElementById('data').textContent),r=data.review;let limit=100;
const el=id=>document.getElementById(id),escape=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
el('context').textContent=r.baseline_available?'Baseline: previous published assets. Candidate has not necessarily been published.':'No previous bundle available.';
el('totals').innerHTML='<table><tr><th>Section</th><th>New</th><th>Changed</th><th>Removed</th><th>Unchanged</th></tr>'+Object.entries(r.summary).map(([k,v])=>'<tr><td>'+escape(k)+'</td>'+['added','changed','removed','unchanged'].map(s=>'<td>'+v[s]+'</td>').join('')+'</tr>').join('')+'</table>';
for(const s of Object.keys(r.summary)){const o=document.createElement('option');o.value=s;o.textContent=s;el('section').append(o)}
el('provenance').textContent=JSON.stringify({provenance:data.provenance,manual_policy:data.manual_policy_still_used,unresolved_artwork:data.unresolved_artwork},null,2);
function draw(){const q=el('search').value.toLowerCase();const rows=r.records.filter(x=>(!el('section').value||x.section===el('section').value)&&(!el('status').value||x.status===el('status').value)&&(!q||JSON.stringify(x).toLowerCase().includes(q)));el('count').textContent=rows.length+' matching changes · '+Math.min(limit,rows.length)+' shown';el('records').innerHTML=rows.slice(0,limit).map(x=>'<article><strong class="'+x.status+'">'+escape(x.status.toUpperCase())+'</strong> · '+escape(x.section)+' · '+escape(x.id)+'<h3>'+escape(x.name||'Unnamed record')+'</h3>'+(x.status==='changed'?'<table><tr><th>Field</th><th>Before</th><th>After</th></tr>'+x.fields.map(f=>'<tr><td>'+escape(f.field)+'</td><td><pre>'+escape(f.before_present?JSON.stringify(f.before,null,2):'(absent)')+'</pre></td><td><pre>'+escape(f.after_present?JSON.stringify(f.after,null,2):'(absent)')+'</pre></td></tr>').join('')+'</table>':'')+'<details'+(x.status==='changed'?'':' open')+'><summary>Full record</summary><div class="pair"><div><small>Before</small><pre>'+escape(JSON.stringify(x.before,null,2))+'</pre></div><div><small>After</small><pre>'+escape(JSON.stringify(x.after,null,2))+'</pre></div></div></details></article>').join('');el('more').hidden=rows.length<=limit}
for(const id of ['search','section','status'])el(id).addEventListener('input',()=>{limit=100;draw()});el('more').onclick=()=>{limit+=100;draw()};draw();</script></html>'''
