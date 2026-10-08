"""Offline energy state-machine checks; no bot imports or network."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

SOURCE=Path(__file__).resolve().parents[3]/'cogs/guild_verification_cog.py'
TREE=ast.parse(SOURCE.read_text(encoding='utf-8'))
NAMES={'should_alert','extrapolate_energy','_check_energy_alerts','save_energy_alert'}
NODES=[n for n in ast.walk(TREE) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in NAMES]
for n in NODES:
    n.returns=None
    for arg in n.args.args:arg.annotation=None
NS={'MAX_ENERGY':600,'ENERGY_REGEN_SECONDS':480,'REARM_MARGIN':30,'ALERT_COOLDOWN':3600,'CACHE_MAX_AGE':604800,
    'time':SimpleNamespace(time=lambda:10000),'logger':logging.getLogger('energy-tests'),
    'settings':SimpleNamespace(DISCORD_SERVER_ID=1),'DB_PATH':'unused'}
exec(compile(ast.Module(body=NODES,type_ignores=[]),str(SOURCE),'exec'),NS)

class EnergyTests(unittest.IsolatedAsyncioTestCase):
    def test_threshold_and_rearm(self):
        f=NS['should_alert']
        for e in (420,421,449):self.assertEqual(f(e,450,False,None,10000),(False,False))
        self.assertEqual(f(450,450,False,None,10000),(True,True))
        self.assertEqual(f(571,600,False,None,10000),(False,False))
        self.assertEqual(f(600,600,False,None,10000),(True,True))
        self.assertEqual(f(0,1,False,None,10000),(False,False))
        self.assertEqual(f(440,450,True,None,10000),(False,True))
        self.assertEqual(f(420,450,True,None,10000),(False,False))
        self.assertEqual(f(450,450,False,9999,10000),(False,False))
    def test_zero_cache(self):
        self.assertEqual(NS['extrapolate_energy'](0,9520,10000),(1,True))
        self.assertEqual(NS['extrapolate_energy'](100,0,10000),(None,False))
    async def check(self,online,energy,delivered=True,was_above=0,has_data=True):
        conn=MagicMock();cursor=SimpleNamespace(fetchall=AsyncMock(return_value=[(123,450,100,9520,was_above,None)]))
        conn.execute=AsyncMock(return_value=cursor);conn.executemany=AsyncMock();conn.commit=AsyncMock()
        context=MagicMock();context.__aenter__=AsyncMock(return_value=conn);context.__aexit__=AsyncMock(return_value=False)
        NS['aiosqlite']=SimpleNamespace(connect=lambda _:context)
        NS['_compute_energy']=lambda *args:(energy,has_data,0)
        cog=SimpleNamespace(bot=SimpleNamespace(get_guild=lambda _:SimpleNamespace(get_member=lambda _:object())),_send_energy_alert=AsyncMock(return_value=delivered))
        await NS['_check_energy_alerts'](cog,{'p':{'base':{'is_online':online}}},{'p':123})
        update=conn.executemany.call_args.args[1][0] if conn.executemany.called else None
        return cog,update
    async def test_online_alerts_require_fresh_data_and_rearm(self):
        cog,row=await self.check(1,500);cog._send_energy_alert.assert_awaited_once();self.assertEqual(row[2:4],(1,10000))
        cog,row=await self.check(1,400,was_above=1);self.assertEqual(row[2],0)
        cog,row=await self.check(1,500,has_data=False);self.assertEqual(row[:2],(0,0));cog._send_energy_alert.assert_not_called()
    async def test_unknown_presence_skipped(self):
        cog,row=await self.check(None,500);cog._send_energy_alert.assert_not_called();self.assertIsNone(row)
    async def test_delivery_retry(self):
        cog,row=await self.check(0,450,False);self.assertEqual(row[2:4],(0,None))
        cog,row=await self.check(0,450,True);self.assertEqual(row[2:4],(1,10000))
    def test_save_preserves_cache_and_cooldown(self):
        import sqlite3
        node=next(n for n in NODES if n.name=='save_energy_alert')
        sql=next(n.value for n in ast.walk(node) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'INSERT INTO' in n.value)
        conn=sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE energy_alerts(user_id INTEGER PRIMARY KEY, threshold, is_enabled, last_energy, last_seen_ts, was_above, last_alert_ts, updated_at)')
        conn.execute('INSERT INTO energy_alerts VALUES (123,450,1,100,9520,1,9000,0)')
        conn.execute(sql,(123,500,1,10000))
        row=conn.execute('SELECT threshold,last_energy,last_seen_ts,was_above,last_alert_ts FROM energy_alerts').fetchone()
        self.assertEqual(row,(500,100,9520,0,9000));conn.close()
