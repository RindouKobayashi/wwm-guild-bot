import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('player_profiles', Path(__file__).resolve().parents[1] / 'player_profiles.py')
profiles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profiles)


class PlayerProfileTests(unittest.TestCase):
    def mappings(self):
        import json
        return json.loads((Path(__file__).resolve().parents[1]/'player_mappings.json').read_text(encoding='utf8'))

    def test_energy_online_offline_missing_and_visible_birthday(self):
        mappings=self.mappings()
        online={'base':{'is_online':1,'logout_time':100},'gameplay_resources':{'50':{'value':0}},'birthday':{'visible':1,'month':4,'day':5}}
        result=profiles.detail_groups(online,{},mappings,now=1060)
        self.assertEqual(result['Energy & activity'][0]['value'],0)
        self.assertIsNone(result['Fashion & social'][1]['value'])
        online['base']['is_online']=0
        self.assertEqual(profiles.detail_groups(online,{},mappings,now=1060)['Energy & activity'][0]['value'],2)
        online['gameplay_resources']={}
        result=profiles.detail_groups(online,{},mappings,now=1060)
        self.assertEqual(result['Energy & activity'][2]['value'],'Regeneration floor only; base energy unknown')
        online['base']['logout_time']=2000
        self.assertEqual(profiles.detail_groups(online,{},mappings,now=1060)['Energy & activity'][0]['value'],0)
        self.assertEqual(profiles.detail_groups({}, {},mappings,now=1060)['Arena summaries'][0]['value'],None)

    def test_equipment_affix_formatting_and_unknown_ids(self):
        mappings={'constants':{'SLOT_NAMES':{'1':'Primary Weapon'}},'equipment':{'5':'Extracted Sword'},
                  'affixes':{'10':{'name':'Critical rate','format':'{0:.1%}','minimum':'0','maximum':'1'}}}
        data=profiles.equipment({'wear_equips':{'1':{'No':5,'ex':{'base_affixes':[[10,0.2],[999,0]],'durability':0}}}},mappings)
        self.assertEqual(data[0]['name'],'Extracted Sword')
        self.assertEqual(data[0]['affixes'][0]['display'],'20.0%')
        self.assertEqual(data[0]['affixes'][1]['name'],'Unmapped affix (ID 999)')
        self.assertEqual(data[0]['durability'],0)
        self.assertEqual(data[0]['affixes'][0]['roll_percent'],20)
        self.assertIsNone(data[0]['affixes'][1]['roll_percent'])
        for value, lo, hi, status in [(2,'0','1','Outside reference range'),(1,'1','1','Fixed reference'),(1,'invalid','2','Range unavailable')]:
            mappings['affixes']['10'].update(minimum=lo,maximum=hi)
            item=profiles.equipment({'wear_equips':{'1':{'No':5,'ex':{'base_affixes':[[10,value]],'retoned':0}}}},mappings)[0]
            self.assertIsNone(item['affixes'][0]['roll_percent'])
            self.assertEqual(item['affixes'][0]['range_status'],status)
            self.assertEqual(item['retuned_count'],0)

    def test_percent_of_maximum_precision_relay_and_group_order(self):
        mappings=self.mappings()
        rows={'8':{'No':1101663,'ex':{'suffix':2,'base_affixes':[[9793108,77.80000000000001]]}},
              '1':{'No':1101673,'ex':{'suffix':1,'legacy_origin_no':1101673,'base_affixes':[[9713002,73.13199999999999]]}},
              '21':{'No':1101671,'ex':{'suffix':45,'base_affixes':[]}}}
        items=profiles.equipment({'wear_equips':rows},mappings)
        self.assertEqual([i['slot_id'] for i in items],['1','8','21'])
        self.assertEqual(items[0]['affixes'][0]['roll_percent'],94)
        self.assertEqual(items[1]['affixes'][0]['roll_percent'],100)
        self.assertEqual(items[0]['tier'],96)
        self.assertEqual(items[0]['quality'],'Legendary')
        self.assertEqual(items[0]['set'],'Jadeware')
        self.assertEqual(items[1]['set'],'Formbend')
        self.assertEqual(items[2]['set'],'Startling String')
        self.assertTrue(items[0]['relayed'])
        self.assertEqual(items[0]['relay_limit_percent'],94)
        self.assertFalse(items[1]['relayed'])
        self.assertEqual(items[2]['slot'],'Bow')
        self.assertEqual(items[1]['group'],'Defensive')

    def test_unknown_metadata_and_percentage_units(self):
        mappings=self.mappings()
        item=profiles.equipment({'wear_equips':{'99':{'No':-1,'ex':{'suffix':-1,'base_affixes':[[9793013,0.0846]]}}}},mappings)[0]
        self.assertIsNone(item['tier'])
        self.assertIsNone(item['quality'])
        self.assertIsNone(item['set'])
        self.assertEqual(item['group'],'Other')
        self.assertIn('%',item['affixes'][0]['maximum_display'])
        self.assertEqual(item['affixes'][0]['roll_percent'],94)

    def test_collection_names_are_scoped_and_ambiguous_not_guessed(self):
        mappings={'constants':{'COLLECTION_CATEGORY_SCOPES':{'titles':['title'],'guise':['outfit']}},
                  'items':{'1':{'resource':['Wrong currency'],'title':['Correct title'],'outfit':['A','B']}}}
        data=profiles.collections({'title_prop':{'titles':{'1':{'level':0}}},'guise':{'owned_views':{'1':2}}},mappings)
        self.assertEqual(data['titles'][0]['name'],'Correct title')
        self.assertEqual(data['guise'][0]['name'],'Unmapped item (ID 1)')

    def test_missing_values_are_not_zero_or_offline(self):
        data = profiles.profile({'base': {}}, {})
        self.assertEqual(data['status'], 'Not reported')
        self.assertIsNone(data['online_hours'])
        self.assertIsNone(data['level'])
        self.assertEqual(data['guild']['status'], 'not_reported')
        self.assertTrue(all(v is None for v in data['achievements'].values()))

    def test_mapping_zero_values_invisible_and_allowlist(self):
        data = profiles.profile({'base': {'nickname': '<b>Player</b>', 'number_id': '0012345678',
            'school': '4', 'is_online': 1, 'invisible': True, 'online_time': 0, 'private_field': 'secret'},
            'achievement': {'quantity': {'1': 0, '2': 12, '3': 3}}, 'club': {}}, {4: 'Silver Needle'})
        self.assertEqual(data['number'], '0012345678')
        self.assertEqual(data['sect'], 'Silver Needle')
        self.assertEqual(data['status'], 'Invisible')
        self.assertEqual(data['online_hours'], 0)
        self.assertEqual(data['achievements']['Normal'], 0)
        self.assertEqual(data['guild']['status'], 'none')
        self.assertNotIn('private_field', str(data))

    def test_unknown_sect_is_explicit_and_nonfinite_stats_missing(self):
        data = profiles.profile({'base': {'school': 999, 'online_time': float('nan')},
                                 'club': {'club_id': 'internal-id'}}, {})
        self.assertEqual(data['sect'], 'Unmapped sect (ID 999)')
        self.assertIsNone(data['online_hours'])
        self.assertEqual(data['guild']['status'], 'joined')
        self.assertNotIn('internal-id', str(data))
