import ast
import copy
import json
from pathlib import Path
import sys
import tempfile
import struct
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'maintenance/assets'))
from extract_pvp_ranks import converted
from utility.pvp_ranks import format_rank,validate


def fixture():
    return converted({'31':dict(rank_1=3,rank_2=1,name='Skilled I',rank_1_name='Skilled'),
                      '101':dict(rank_1=10,rank_2=1,name='Mythic',rank_1_name='Mythic')})


class RankTests(unittest.TestCase):
    def test_unverified_client_labels_preserve_silver(self):
        data=fixture();data['display_policy']='legacy_until_client_verified'
        for mode in ('1v1','3v3'):
            for division,suffix in ((1,'I'),(2,'II'),(3,'III')):
                self.assertEqual(format_rank(3,division,mode,data=data,
                    legacy_names={'3':'Silver'},legacy_suffixes={'1':'I','2':'II','3':'III'}),'Silver '+suffix)
        self.assertIn('unknown mode',format_rank(3,1,'huiwu',data=data))

    def test_named_manifest_keeps_identical_schema_modes_separate(self):
        from named_mtable import binding
        names=['fight_shoulder_grade_info','lunjian_grade_info']
        raw=bytearray(struct.pack('<6I',0x10001,8,2,2,0,0))
        for name in names:
            value=name.encode();raw+=struct.pack('<H',len(value))+value
            raw+=b'\0'*((-len(raw))%8)
        for archive,frame in ((1,15057),(2,1871)):
            raw+=struct.pack('<8I',28,1,archive+(frame<<16),0xffffffff,0,0,0,1024)
        one=binding(bytes(raw),'lunjian_grade_info');group=binding(bytes(raw),'fight_shoulder_grade_info')
        self.assertEqual((one['archive'],one['frame']),('index2',1871))
        self.assertEqual((group['archive'],group['frame']),('index1',15057))
        with self.assertRaises(ValueError):binding(bytes(raw),'zzq_grade')
        with self.assertRaises(ValueError):binding(bytes(raw[:30]),'lunjian_grade_info')

    def test_other_pvp_modes_not_given_normal_arena_labels(self):
        for mode in ('fight_shoulder','huiwu','zzq','wuwo','5v5'):
            self.assertIn('unknown mode',format_rank(3,1,mode=mode,data=fixture()))

    def test_bundle_rejects_discord_activity_mapping_drift(self):
        from test_game_asset_pipeline import fixture as bundle_fixture
        from build_game_bundle import finish
        from utility.game_assets import validate_release
        with tempfile.TemporaryDirectory() as d:
            stage=Path(d);tables=bundle_fixture(stage);data=fixture()
            tables['pvp_ranks']=data;tables['player_mappings']['pvp_ranks']=copy.deepcopy(data)
            finish(stage,tables,{'mode':'fixture'},{});validate_release(stage)
            tables['player_mappings']['pvp_ranks']['modes']['1v1']['ranks']['31']['name']='Wrong label'
            with self.assertRaises(ValueError):finish(stage,tables,{'mode':'fixture'},{})

    def test_exact_localized_labels_for_both_modes(self):
        for mode in ('1v1','3v3'):
            self.assertEqual(format_rank(3,1,mode,data=fixture()),'Skilled I')
            self.assertEqual(format_rank('10','1',mode,data=fixture()),'Mythic')

    def test_invalid_and_future_ids(self):
        data=fixture()
        self.assertIsNone(format_rank(None,None,data=data))
        self.assertIn('invalid grade',format_rank(True,1,data=data))
        self.assertIn('invalid division',format_rank(3,{},data=data))
        self.assertIn('Unknown rank',format_rank(3,4,data=data))
        self.assertIn('Unknown rank',format_rank(99,1,data=data))
        self.assertEqual(format_rank(3,None,data=data),'Skilled')
        self.assertEqual(format_rank(3,0,data=data),'Skilled')
        self.assertIn('unknown mode',format_rank(3,1,'event',data=data))

    def test_legacy_only_when_extracted_table_absent(self):
        kwargs=dict(legacy_names={'3':'Silver'},legacy_suffixes={'1':'I'})
        self.assertEqual(format_rank(3,1,data={},**kwargs),'Silver I')
        self.assertEqual(format_rank(3,1,data=fixture(),**kwargs),'Skilled I')
        self.assertIn('Unknown rank',format_rank(3,4,data=fixture(),**kwargs))

    def test_bad_join_and_unresolved_names_rejected(self):
        with self.assertRaises(ValueError):converted({'32':dict(rank_1=3,rank_2=1,name='Skilled I',rank_1_name='Skilled')})
        with self.assertRaises(ValueError):converted({'31':dict(rank_1=3,rank_2=1,name=None,rank_1_name='Skilled')})
        data=fixture();data['modes']['3v3']['ranks']['31']['division']=2
        with self.assertRaises(ValueError):validate(data)

    def test_inconsistent_optional_grade_label_not_guessed(self):
        data=converted({'81':dict(rank_1=8,rank_2=1,name='Grandmaster I',rank_1_name='Hidden Edge'),
                        '82':dict(rank_1=8,rank_2=2,name='Grandmaster II',rank_1_name='Grandmaster')})
        self.assertEqual(format_rank(8,1,data=data),'Grandmaster I')
        self.assertIn('without division',format_rank(8,0,data=data))
        self.assertIn('8',data['limitations']['inconsistent_grade_only_labels'])

    def test_discord_and_activity_use_shared_formatter(self):
        from activity.player_profiles import detail_groups
        from utility.game_assets import current
        from unittest.mock import patch
        mappings=copy.deepcopy(current().section('player_mappings'));mappings['pvp_ranks']=fixture()
        details=detail_groups({'lunjian':{'grade':3,'small_grade':1},'lunjian3v3_prop':{'grade':10,'small_grade':1}}, {},mappings)
        values={r['label']:r['value'] for r in details['Arena summaries']}
        self.assertEqual(values['1v1 rank'],'Skilled I');self.assertEqual(values['3v3 rank'],'Mythic')
        tree=ast.parse((ROOT/'cogs/wwm_cog.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PlayerProfileView')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_format_rank')
        method.decorator_list=[];scope={};exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'rank-test','exec'),scope)
        scope['PlayerProfileView']=type('Profile',(),dict(GRADE_NAMES={},SMALL_GRADE_SUFFIXES={}))
        with patch('utility.game_assets.current') as assets:
            assets.return_value.section.return_value=fixture()
            self.assertEqual(scope['_format_rank'](3,1),'Skilled I')
            self.assertEqual(scope['_format_rank'](10,1,'3v3'),'Mythic')


if __name__=='__main__':unittest.main()
