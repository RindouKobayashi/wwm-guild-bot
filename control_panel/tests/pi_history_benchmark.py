"""Benchmark a read-only backup; never migrate/prune the source database."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import sys
from control_panel.storage import Store
from control_panel.history import History,HistoryBusy

async def benchmark(source):
    result={'source_bytes':source.stat().st_size,'windows':[]}
    with tempfile.TemporaryDirectory(prefix='wwm-history-check-') as temp:
        snapshot=Path(temp)/'history.db'
        assert source.resolve()!=snapshot.resolve()
        original=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        destination=sqlite3.connect(snapshot)
        started=time.monotonic();deadline=started+30
        def progress(*_):
            if time.monotonic()>deadline:raise TimeoutError('Read-only backup could not complete within 30 seconds')
        try:
            original.backup(destination,pages=512,progress=progress,sleep=.05)
            result['source_connection_changes']=original.total_changes
        finally:
            destination.close();original.close()
        result['backup_seconds']=round(time.monotonic()-started,3)
        print(json.dumps({'phase':'backup','seconds':result['backup_seconds'],'source_bytes':result['source_bytes']}),file=sys.stderr,flush=True)
        store=Store(snapshot)
        try:
            # This disposable copy needs no power-loss durability. Production
            # retains its configured synchronous mode; measured queries are reads.
            store.db.execute('PRAGMA synchronous=NORMAL')
            result['event_counts']=dict(store.db.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
            print(json.dumps({'phase':'indexing','event_counts':result['event_counts']}),file=sys.stderr,flush=True)
            started=time.monotonic()
            while not store.backfill(1000)['complete']:
                if time.monotonic()-started>300:raise TimeoutError('Isolated indexing exceeded five minutes')
            result['index_seconds']=round(time.monotonic()-started,3)
            history=History(store)
            try:
                for window in (900,3600,86400,259200):
                    started=time.monotonic()
                    try:
                        data=await history.read('analytics',range_seconds=window)
                        result['windows'].append({'range_seconds':window,'seconds':round(time.monotonic()-started,4),
                            'requests':data['summary']['total'],'response_bytes':len(json.dumps(data).encode()),
                            'median_ms':data['summary']['median_ms'],'p95_ms':data['summary']['p95_ms']})
                        started=time.monotonic();await history.read('analytics',range_seconds=window)
                        result['windows'][-1]['cached_seconds']=round(time.monotonic()-started,6)
                    except HistoryBusy:
                        result['windows'].append({'range_seconds':window,'seconds':round(time.monotonic()-started,4),
                                                  'result':'bounded history_busy'})
                result['performance']=history.performance()
                started=time.monotonic()
                page=await history.read('query','request',summary=True,limit=100,since=time.time()-86400)
                result['request_page']={'seconds':round(time.monotonic()-started,4),'rows':len(page['items']),
                                        'response_bytes':len(json.dumps(page).encode())}
            finally:await history.close()
        finally:store.close()
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    args=parser.parse_args()
    try:print(json.dumps(asyncio.run(benchmark(args.source))))
    except (TimeoutError,sqlite3.Error) as error:raise SystemExit(type(error).__name__+': '+str(error))
