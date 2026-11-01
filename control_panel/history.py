"""Two bounded read workers, SQLite interruption, and shared analytics caching."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import sqlite3
import threading
import time

class HistoryBusy(Exception):
    pass

@dataclass
class Flight:
    stop: threading.Event = field(default_factory=threading.Event)
    waiters: int = 0
    task: object = None

class History:
    def __init__(self, store, budget_seconds=3):
        self.store=store;self.budget=budget_seconds
        self.executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='panel-history')
        self.active={};self.cache=OrderedDict();self.closing=False
        self.stats={'cache_hits':0,'shared_queries':0,'cancelled_queries':0,'timed_out_queries':0,
                    'busy_rejections':0,'completed_queries':0,'last_query_ms':0,'last_queue_wait_ms':0}

    def _run(self,method,args,kwargs,flight,admitted):
        started=time.monotonic();deadline=started+self.budget
        self.stats['last_queue_wait_ms']=round((started-admitted)*1000,2)
        db=sqlite3.connect(self.store.path.as_uri()+'?mode=ro',uri=True,timeout=.05)
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(flight.stop.is_set() or time.monotonic()>=deadline),1000)
        try:
            db.execute('BEGIN')
            result=getattr(self.store.reader(db),method)(*args,**kwargs)
            if flight.stop.is_set() or time.monotonic()>=deadline:
                raise sqlite3.OperationalError('interrupted')
            return result
        except sqlite3.OperationalError as error:
            if time.monotonic()>=deadline:
                self.stats['timed_out_queries']+=1
            raise HistoryBusy('History is busy. Retry shortly; live health is still available.') from error
        finally:
            db.close()
            self.stats['last_query_ms']=round((time.monotonic()-started)*1000,2)

    async def _execute(self,key,method,args,kwargs,flight,cacheable):
        admitted=time.monotonic()
        try:
            result=await asyncio.get_running_loop().run_in_executor(self.executor,
                lambda:self._run(method,args,kwargs,flight,admitted))
            self.stats['completed_queries']+=1
            if cacheable and not flight.stop.is_set() and result.get('indexing',{}).get('complete',True):
                encoded=len(json.dumps(result).encode())
                if encoded<=2*1024*1024:
                    self.cache[key]=(time.monotonic()+10,result,encoded)
                    while len(self.cache)>16 or sum(item[2] for item in self.cache.values())>2*1024*1024:
                        self.cache.popitem(last=False)
            return result
        finally:
            self.active.pop(key,None)

    async def read(self,method,*args,**kwargs):
        if self.closing:raise HistoryBusy('History reader is shutting down.')
        cacheable=method in ('analytics','command_summary')
        cache_kwargs=dict(kwargs)
        if method=='analytics' and 'range_seconds' in kwargs:
            seconds=int(kwargs.pop('range_seconds'))
            if seconds not in (900,3600,86400,259200):raise ValueError('Invalid analytics range')
            if 'since' in kwargs or 'until' in kwargs:raise ValueError('Choose range_seconds or explicit dates')
            cache_kwargs=dict(kwargs,range_seconds=seconds)
            end=int(time.time()//10)*10
            kwargs.update(since=end-seconds,until=end)
        key=(method,json.dumps([args,cache_kwargs],sort_keys=True,allow_nan=False))
        cached=self.cache.get(key)
        if cached and cached[0]>time.monotonic():
            self.stats['cache_hits']+=1;self.cache.move_to_end(key);return cached[1]
        self.cache.pop(key,None)
        flight=self.active.get(key)
        if flight and flight.stop.is_set():
            self.stats['busy_rejections']+=1;raise HistoryBusy('Previous history query is stopping; retry shortly.')
        if flight:
            self.stats['shared_queries']+=1
        else:
            if len(self.active)>=2:
                self.stats['busy_rejections']+=1;raise HistoryBusy('History is busy. Retry shortly; live health is still available.')
            flight=Flight();self.active[key]=flight
            flight.task=asyncio.create_task(self._execute(key,method,args,kwargs,flight,cacheable))
            # A disconnected final waiter must not leave an unobserved exception.
            flight.task.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)
        flight.waiters+=1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters-=1
            if not flight.waiters and not flight.task.done():
                flight.stop.set();self.stats['cancelled_queries']+=1

    def performance(self):
        return dict(self.stats,active_reads=len(self.active),cache_entries=len(self.cache))

    async def close(self):
        self.closing=True
        for flight in list(self.active.values()):flight.stop.set()
        await asyncio.gather(*(flight.task for flight in list(self.active.values())),return_exceptions=True)
        self.executor.shutdown(wait=True)
