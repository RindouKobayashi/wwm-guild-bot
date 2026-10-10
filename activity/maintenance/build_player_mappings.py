"""Export existing extracted names for Git-pull deployments; no player data."""
import ast
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT.parent
sys.path.insert(0, str(BOT))
from utility.xlsx_reader import iter_xlsx_rows

tree = ast.parse((BOT / 'cogs/wwm_cog.py').read_text(encoding='utf8'))
keys = {'GRADE_NAMES', 'SMALL_GRADE_SUFFIXES', 'SLOT_NAMES', 'COLLECTION_CATEGORY_SCOPES', 'MAX_ENERGY', 'ENERGY_REGEN_SECONDS'}
constants = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in keys:
                constants[target.id] = ast.literal_eval(node.value)
equipment = {str(row[0]).strip(): str(row[1]).strip() for row in iter_xlsx_rows(BOT/'data/All_Equipment_Item_Names.xlsx')
             if len(row)>1 and str(row[0]).strip().isdigit() and row[1]}
items = {}
for row in iter_xlsx_rows(BOT/'data/All_Item_Names.xlsx'):
    if len(row)<3 or not str(row[0]).strip().isdigit() or not row[2]:continue
    bucket=items.setdefault(str(row[0]).strip(),{})
    names=bucket.setdefault(str(row[1]).strip(),[])
    if row[2] not in names:names.append(row[2])
affixes={}
with (BOT/'data/all_affix_id_english_names.csv').open(encoding='utf-8-sig',newline='') as f:
    for row in csv.DictReader(f):
        if row.get('english_name'):
            affixes[row['affix_id']]={k:row.get(v) for k,v in [('name','english_name'),('format','value_show_format'),('minimum','minimum'),('maximum','maximum')]}
target=ROOT/'player_mappings.json'
extra_path=BOT/'data/equipment_metadata.json'
extra=json.loads(extra_path.read_text(encoding='utf-8')) if extra_path.exists() else {k:v for k,v in json.loads(target.read_text(encoding='utf-8')).items() if k.startswith('equipment_') or k=='legacy_limits'} if target.exists() else {}
target.write_text(json.dumps({**extra,'sources':extra.get('sources',[])+['data/All_Equipment_Item_Names.xlsx','data/All_Item_Names.xlsx','data/all_affix_id_english_names.csv','cogs/wwm_cog.py'],
    'constants':constants,'equipment':equipment,'items':items,'affixes':affixes},ensure_ascii=False,separators=(',',':')),encoding='utf8')
print(f'Exported {len(equipment)} equipment names, {len(items)} scoped item IDs, {len(affixes)} affixes: {target}')
