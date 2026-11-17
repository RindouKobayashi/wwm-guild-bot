"""Extract lunjian/lunjian3v3_prop labels; not a general PvP mode mapping."""
import argparse
import copy
import json
from pathlib import Path
import struct
import sys

BOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BOT))
from utility.pvp_ranks import validate


def mode_evidence(toolkit,game):
    from original_lua import read_original
    from lua_symbols import inspect
    def walk(tree):
        yield tree
        for child in tree['children']:yield from walk(child)
    result=[]
    sources=('hexm/common/consts/lunjian_consts.lua',
             'hexm/client/ui/windows/pvp/pvp3v3_fight_info_window.lua')
    for source in sources:
        code,evidence=read_original(toolkit,game,source);tree=inspect(code)
        if tree['source']!='@'+source:raise ValueError('PvP Lua source identity mismatch')
        nodes=list(walk(tree))
        hits=[p for p in nodes if 'lunjian_grade_info' in p['constants'] and 'name' in p['constants']]
        if not hits:raise ValueError('PvP mode no longer references lunjian_grade_info names')
        if source==sources[0] and not any({'lunjian_grade_info','lunjian','grade','small_grade'}<=set(p['constants']) for p in nodes):
            raise ValueError('Normal 1v1 combined-grade source changed')
        if source==sources[1] and not any(10 in p['constants'] for p in hits):raise ValueError('3v3 combined-grade source changed')
        evidence['prototypes']=[dict(first_line=p['first_line'],last_line=p['last_line'],
                                     constants=[c for c in p['constants'] if c in ('lunjian_grade_info','name','grade','small_grade','lunjian')]) for p in hits]
        result.append(evidence)
    source='hexm/client/data_oversea/generated_data/manifest_overlay.lua'
    code,evidence=read_original(toolkit,game,source);tree=inspect(code)
    if any('lunjian_grade_info' in p['constants'] for p in walk(tree)):raise ValueError('Overseas rank-table override requires review')
    result.append(dict(evidence,check='No direct lunjian_grade_info override in current overseas manifest'))
    return result


def converted(rows):
    ranks={};grade_labels={}
    for key,row in rows.items():
        grade,division=row.get('rank_1'),row.get('rank_2')
        name,grade_name=row.get('name'),row.get('rank_1_name')
        if type(grade) is not int or type(division) is not int or key!=str(grade*10+division):raise ValueError('PvP row identity disagrees with rank fields')
        if not isinstance(name,str) or not name.strip() or not isinstance(grade_name,str) or not grade_name.strip():raise ValueError('Missing official English PvP name')
        grade_labels.setdefault(str(grade),set()).add(grade_name.strip())
        ranks[key]=dict(grade=grade,division=division,name=name)
    grades={g:next(iter(names)) for g,names in grade_labels.items() if len(names)==1}
    ambiguous={g:sorted(names) for g,names in grade_labels.items() if len(names)>1}
    result=dict(format=1,modes={'1v1':dict(ranks=ranks,grades=grades),'3v3':dict(ranks=copy.deepcopy(ranks),grades=dict(grades))},
                limitations={'inconsistent_grade_only_labels':ambiguous,'policy':'Full row name is authoritative. Conflicting grade-only labels are omitted rather than guessed.'})
    validate(result);return result


def extract(toolkit,root):
    sys.path.insert(0,str(toolkit/'app'))
    import update_trial_dungeons as D
    from table_discovery import catalogue
    from named_mtable import installed_binding
    from utility.game_assets import digest
    reference=json.loads((Path(__file__).with_name('pvp_rank_reference.json')).read_text(encoding='utf-8'))
    cat=catalogue(root,toolkit/'cache/table_schema_catalogue.json')
    named=installed_binding(root,'lunjian_grade_info')
    matches=[t for t in cat['tables'] if (t['archive'],t['frame'])==(named['archive'],named['frame'])]
    if len(matches)!=1 or not set(reference['fields'])<=set(matches[0]['fields']):
        raise ValueError('Named lunjian_grade_info schema missing or changed')
    table=matches[0]
    frames=dict(D.U.frames(root/table['archive']));schema=D.decode_name_list(frames[table['frame']])
    records=D.U.shard_records(frames,table['frame']);chunks=D.U.load_frames(root,[loc for _,loc,*_ in records]);locale=D.all_english_strings(root);rows={}
    for key,locator,*_ in records:
        a,f,o,z=struct.unpack('<HHHH',locator);raw=chunks[(a,f)][o*8:(o+z)*8];reader=D.U.Reader(raw)
        row={schema[i]:v for i,v in D.U.read_row(reader,schema).items()}
        if row.get('id')!=key or str(key) in rows or any(raw[reader.pos:]):raise ValueError('Duplicate, invalid or unbounded PvP row')
        for field in ('name','rank_1_name'):
            value=row.get(field);row[field]=locale.get(value) if type(value) is int else value
        rows[str(key)]=row
    if len(rows)!=table['records'] or len(rows)!=named['records']:raise ValueError('PvP extraction coverage mismatch')
    result=converted(rows)
    result['display_policy']='legacy_until_client_verified'
    result['display_verification_note']='Stored English strings say Skilled; owner reports Silver in the running client. Keep established display labels until final client localization is verified.'
    proof=mode_evidence(toolkit,root.parents[2])
    result['sources']=dict(archive=table['archive'],frame=table['frame'],fields=schema,records=len(rows),index_sha256=digest(root/table['archive']),
        named_binding=named,
        current_lua_evidence=proof,
        api_fields=reference['api_fields'],
        mode_binding='Only API lunjian and lunjian3v3_prop grade/division labels. Both client displays use lunjian_grade_info. Group-mode and special-mode ranks are not mapped by this export.',
        evidence=reference['evidence'])
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--toolkit',type=Path,default=BOT.parent/'WWM-Toolkit');p.add_argument('--game-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    data=extract(args.toolkit,args.game_root);args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print('Extracted',len(data['modes']['1v1']['ranks']),'normal arena rank labels for 1v1/3v3')
