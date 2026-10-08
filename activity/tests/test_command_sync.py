import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
from utility.activity_commands import sync_with_activity

class CommandSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_entry_point(self):
        entry={'id':'4','name':'launch','description':'Launch','type':4,'handler':2,'application_id':'123','version':'1'}
        command=MagicMock();command.to_dict.return_value={'name':'ranking','type':1}
        tree=SimpleNamespace(_get_all_commands=MagicMock(return_value=[command]),translator=None,sync=AsyncMock())
        http=SimpleNamespace(get_global_commands=AsyncMock(return_value=[entry]),bulk_upsert_global_commands=AsyncMock())
        await sync_with_activity(SimpleNamespace(application_id=123,http=http,tree=tree))
        sent=http.bulk_upsert_global_commands.await_args.kwargs['payload']
        self.assertEqual(sent[1],{'id':'4','name':'launch','description':'Launch','type':4,'handler':2})
        tree.sync.assert_not_awaited()

    async def test_failed_read_never_overwrites_commands(self):
        http=SimpleNamespace(get_global_commands=AsyncMock(side_effect=RuntimeError('offline')),bulk_upsert_global_commands=AsyncMock())
        tree=SimpleNamespace(sync=AsyncMock())
        with self.assertRaises(RuntimeError):await sync_with_activity(SimpleNamespace(application_id=123,http=http,tree=tree))
        http.bulk_upsert_global_commands.assert_not_awaited();tree.sync.assert_not_awaited()
