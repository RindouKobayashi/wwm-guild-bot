import {useCallback,useEffect,useRef,useState} from 'react';

export type ResourceState<T>={data:T|null;loading:boolean;error:string;loadedKey:string;updatedAt:number};
export type Resource<T>=ResourceState<T>&{refresh:()=>void};
type Clock={now:()=>number;set:(callback:()=>void,delay:number)=>any;clear:(id:any)=>void};
const nativeClock:Clock={now:()=>Date.now(),set:(callback,delay)=>setTimeout(callback,delay),clear:id=>clearTimeout(id)};

/** Share the server's two-read budget across visible resources in this tab. */
export function createHistoryGate(fetcher:typeof fetch=fetch,limit=2):typeof fetch{
 let active=0;const queue:any[]=[];
 const drain=()=>{
  while(active<limit&&queue.length){
   const item=queue.shift();item.signal?.removeEventListener('abort',item.abort);
   if(item.signal?.aborted){item.reject(new DOMException('Aborted','AbortError'));continue}
   active++;
   Promise.resolve().then(()=>fetcher(item.input,item.init)).then(item.resolve,item.reject).finally(()=>{active--;drain()});
  }
 };
 return (input,init)=>new Promise((resolve,reject)=>{
  const signal=init?.signal;
  const item:any={input,init,signal,resolve,reject};
  item.abort=()=>{const index=queue.indexOf(item);if(index>=0){queue.splice(index,1);reject(new DOMException('Aborted','AbortError'))}};
  if(signal?.aborted){reject(new DOMException('Aborted','AbortError'));return}
  signal?.addEventListener('abort',item.abort,{once:true});queue.push(item);drain();
 });
}
const gatedHistory=createHistoryGate();
export const resourceFetch:typeof fetch=(input,init)=>{
 const path=typeof input==='string'?input:input instanceof URL?input.href:input.url;
 return (init?.method&&init.method!=='GET')||/\/api\/(?:status(?:\?|$)|operations\/)/.test(path)
  ?fetch(input,init):gatedHistory(input,init);
};

/** Pure controller shared by the hook and deterministic concurrency tests. */
export function createRefresh<T>(key:string,getUrl:()=>string,interval:number,
 publish:(update:(old:ResourceState<T>)=>ResourceState<T>)=>void,
 options:{clock?:Clock;fetcher?:typeof fetch}={}){
 const clock=options.clock||nativeClock,fetcher=options.fetcher||resourceFetch;
 let alive=true,pending=false,dirty=false,lastStart=-Infinity;
 let controller:AbortController|null=null,timer:any=null;
 const schedule=()=>{
  if(!alive)return;
  if(pending){dirty=true;return}
  if(timer!==null)return;
  const wait=Math.max(0,lastStart+interval-clock.now());
  if(wait)timer=clock.set(()=>{timer=null;void run()},wait);else void run();
 };
 const run=async()=>{
  if(!alive||pending)return;
  pending=true;dirty=false;lastStart=clock.now();controller=new AbortController();
  let timedOut=false,retry=false;
  const deadline=clock.set(()=>{timedOut=true;controller?.abort()},Math.max(4000,Math.min(8000,interval)));
  publish(old=>({...old,loading:true,error:old.loadedKey===key?old.error:''}));
  try{
   const response=await fetcher(getUrl(),{signal:controller.signal});
   const data=await response.json();
   if(!response.ok){retry=data.code==='history_busy';throw new Error(data.error||'Request failed')}
   if(alive)publish(()=>({data,loading:false,error:'',loadedKey:key,updatedAt:clock.now()}));
  }catch(error){
   if(alive&&((error as Error).name!=='AbortError'||timedOut))publish(old=>({...old,loading:false,error:timedOut?'Request timed out; retrying.':(error as Error).message}));
  }finally{
   pending=false;clock.clear(deadline);
   if(alive){
    if(retry)timer=clock.set(()=>{timer=null;void run()},Math.max(2000,interval));
    else if(dirty)schedule();
   }
  }
 };
 return {refresh:schedule,dispose:()=>{alive=false;controller?.abort();if(timer!==null)clock.clear(timer)}};
}

/** One flight per resource; key changes abort, live invalidations coalesce. */
export function useResource<T>(key:string,getUrl:()=>string,interval:number,enabled=true):Resource<T>{
 const [state,setState]=useState<ResourceState<T>>({data:null,loading:false,error:'',loadedKey:'',updatedAt:0});
 const trigger=useRef<()=>void>(()=>{});
 const refresh=useCallback(()=>trigger.current(),[]);
 useEffect(()=>{
  if(!enabled){trigger.current=()=>{};setState(old=>({...old,loading:false,error:''}));return}
  const controller=createRefresh<T>(key,getUrl,interval,setState);
  trigger.current=controller.refresh;controller.refresh();
  const poll=setInterval(controller.refresh,interval);
  return()=>{trigger.current=()=>{};controller.dispose();clearInterval(poll)};
 },[key,getUrl,interval,enabled]);
 return {...state,refresh};
}
