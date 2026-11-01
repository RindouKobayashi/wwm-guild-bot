import assert from 'node:assert/strict';
import {test} from 'node:test';
import {build} from 'esbuild';
import {mkdir,writeFile} from 'node:fs/promises';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=new URL('../',import.meta.url);
const output=new URL('data/tests/refresh-test.mjs',root);
await mkdir(new URL('data/tests/',root),{recursive:true});
const built=await build({entryPoints:[fileURLToPath(new URL('src/refresh.ts',root))],bundle:true,
 external:['react'],format:'esm',write:false});
await writeFile(output,built.outputFiles[0].text);
const {createRefresh,createHistoryGate}=await import(pathToFileURL(fileURLToPath(output)).href);
const flush=async()=>{for(let n=0;n<8;n++)await Promise.resolve()};

function fixture(initial={data:null,loading:false,error:'',loadedKey:'',updatedAt:0}){
 let now=0,id=0,state=initial;const timers=new Map(),requests=[];
 const clock={now:()=>now,set:(callback,delay)=>{timers.set(++id,{at:now+delay,callback});return id},clear:key=>timers.delete(key)};
 const advance=async ms=>{
  const end=now+ms;
  for(let attempts=0;attempts<1000;attempts++){
   const due=[...timers].filter(([,value])=>value.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
   if(!due)break;
   now=due[1].at;timers.delete(due[0]);due[1].callback();await flush();
  }
  now=end;await flush();
 };
 const fetcher=(url,{signal})=>new Promise((resolve,reject)=>{
  requests.push({url,signal,respond:(body,ok=true)=>resolve({ok,json:async()=>body})});
  signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
 });
 return {clock,fetcher,requests,advance,get state(){return state},publish:update=>{state=update(state)}};
}

test('live invalidations and polling never overlap or exceed analytics cadence',async()=>{
 const f=fixture(),c=createRefresh('24h',()=>'/analytics?range_seconds=86400',10000,f.publish,f);
 c.refresh();for(let n=0;n<100;n++)c.refresh();
 assert.equal(f.requests.length,1);
 f.requests[0].respond({total:10});await flush();
 await f.advance(9999);assert.equal(f.requests.length,1);
 await f.advance(1);assert.equal(f.requests.length,2);
 for(let n=0;n<100;n++)c.refresh();assert.equal(f.requests.length,2);
 c.dispose();assert.equal(f.requests[1].signal.aborted,true);
 await f.advance(100000);assert.equal(f.requests.length,2);
});

test('window replacement cancels the old request and cannot publish stale results',async()=>{
 const f=fixture();const old=createRefresh('1h',()=>'/one',10000,f.publish,f);
 old.refresh();old.dispose();
 const next=createRefresh('72h',()=>'/two',10000,f.publish,f);next.refresh();
 assert.equal(f.requests[0].signal.aborted,true);
 f.requests[1].respond({window:'72h'});await flush();
 assert.equal(f.state.loadedKey,'72h');assert.equal(f.state.data.window,'72h');
 f.requests[0].respond({window:'1h'});await flush();
 assert.equal(f.state.data.window,'72h');next.dispose();
});

test('slow history preserves previous window labels without changing health state',async()=>{
 const f=fixture({data:{window:'1h'},loadedKey:'1h',error:'',loading:false,updatedAt:1});
 const c=createRefresh('24h',()=>'/history',10000,f.publish,f);c.refresh();
 assert.equal(f.state.loading,true);assert.equal(f.state.loadedKey,'1h');
 f.requests[0].respond({error:'History busy',code:'history_busy'},false);await flush();
 assert.equal(f.state.error,'History busy');assert.equal(f.state.data.window,'1h');
 assert.equal(f.state.loading,false);await f.advance(10000);assert.equal(f.requests.length,2);
 f.requests[1].respond({window:'24h'});await flush();assert.equal(f.state.loadedKey,'24h');c.dispose();
});

test('health timeouts abort and remain errors until a verified recovery',async()=>{
 const f=fixture({data:{connected:true},loadedKey:'status',error:'',loading:false,updatedAt:0});
 const c=createRefresh('status',()=>'/status',5000,f.publish,f);c.refresh();
 await f.advance(5000);assert.equal(f.requests[0].signal.aborted,true);
 assert.match(f.state.error,/timed out/);c.refresh();
 assert.match(f.state.error,/timed out/);
 f.requests[1].respond({connected:true});await flush();assert.equal(f.state.error,'');c.dispose();
});

test('visible history resources share two network slots instead of overloading the server',async()=>{
 const f=fixture(),gate=createHistoryGate(f.fetcher,2);
 const one=gate('/one',{}),two=gate('/two',{}),three=gate('/three',{});
 await flush();assert.equal(f.requests.length,2);
 f.requests[0].respond({});await one;await flush();assert.equal(f.requests.length,3);
 f.requests[1].respond({});f.requests[2].respond({});await Promise.all([two,three]);
});

test('an obsolete queued window is removed before it reaches the server',async()=>{
 const f=fixture(),gate=createHistoryGate(f.fetcher,2),controller=new AbortController();
 const one=gate('/one',{}),two=gate('/two',{}),obsolete=gate('/obsolete',{signal:controller.signal});
 const rejection=assert.rejects(obsolete,{name:'AbortError'});controller.abort();await rejection;
 await flush();assert.equal(f.requests.length,2);
 f.requests[0].respond({});f.requests[1].respond({});await Promise.all([one,two]);await flush();
 assert.equal(f.requests.length,2);
});
