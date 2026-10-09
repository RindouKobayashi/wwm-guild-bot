// Offline UI verification: all identity/game responses are fixtures.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
const publicDir=path.resolve(__dirname,'../public');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.WWM_TEST_BROWSER?{executablePath:process.env.WWM_TEST_BROWSER}:{})});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));
 const fixture={name:'<img src=x onerror=alert(1)> Player',number:'0012345678',level:70,status:'Invisible',sect:'Silver Needle',region:'Asia',created:1700000000,online_hours:123.4,signature:'<script>alert(1)</script>',guild:{status:'joined',name:'Test Guild'},achievements:{Normal:0,Hard:12,Expert:3},fetched_at:1700000000};
 fixture.details={'Energy & activity':[{label:'Energy',value:0},{label:'Reading type',value:'Game reading'}],'Masteries':[{label:'Martial',value:1234.5},{label:'Exploration',value:null}]};
 await page.route('https://activity.test/**',async route=>{
  const url=new URL(route.request().url()),p=url.pathname.replace(/^\/\.proxy/,'');requests.push(p+url.search);
  const json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  if(p==='/api/config')return json({mode:'discord',client_id:'123456'});
  if(p==='/api/auth')return json({session:'offline-session',access_token:'offline-token',user:{name:'Viewer',bound:true,character_number:'0012345678'}});
  if(p==='/api/player'){
   const section=url.searchParams.get('section');
   if(section==='equipment')return json({section,items:[{slot:'Primary Weapon',name:'Mapped Sword',durability:0,attributes:[{label:'Power',value:10}],affixes:[{name:'Critical rate',display:'20.0%',minimum:'0',maximum:'1',roll_percent:20,range_status:'Comparable'}]}]});
   if(section==='collections')return json({section,buckets:{titles:Array.from({length:45},(_,i)=>({name:'Mapped title '+i,value:i}))}});
   if(section==='social')return json({section,likes:0,partial:true,partners:[{name:'Partner',number:'1234567890',favor:0}]});
   if(section==='homestead'&&fixture.guild.status==='none')return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'Homestead temporarily unavailable'})});
   if(section==='homestead')return json({section,rows:[],message:'No homestead reference was returned.'});
   return json(fixture);
  }
  if(p==='/api/guild')return json({status:'no_guild',matches:[],scanned:0});
  if(p==='/api/rank')return json({rank_list:[]});
  if(p==='/discord-sdk.js')return route.fulfill({contentType:'text/javascript',body:'export class DiscordSDK {constructor(){this.commands={authorize:async()=>({code:"offline"}),authenticate:async()=>({})};} async ready(){}}'});
  const file=path.join(publicDir,p==='/'?'index.html':p.slice(1));
  if(!fs.existsSync(file))return route.fulfill({status:404,body:'Missing fixture file'});
  const ext=path.extname(file),types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json','.png':'image/png'};
  return route.fulfill({contentType:types[ext]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 await page.goto('https://activity.test/?frame_id=offline');
 await page.waitForFunction(()=>document.getElementById('connection').textContent==='Discord connected');
 assert.equal(await page.inputValue('#player-number'),'0012345678');
 await page.waitForFunction(()=>document.getElementById('guild-state').textContent.includes('not currently in a guild'));
 await page.click('[data-tab="players"]');
 assert.equal(await page.inputValue('#profile-query'),'0012345678');
 await page.click('#profile-own');await page.waitForSelector('#profile-battles');
 assert((await page.innerText('#profile-content')).includes('Silver Needle'));
 assert((await page.innerText('#profile-content')).includes('<script>alert(1)</script>'));
 assert.equal(await page.locator('#profile-content script, #profile-content img').count(),0);
 assert.equal(await page.isEnabled('#profile-battles'),true);
 assert((await page.locator('.profile-groups').innerText()).includes('1,234.5'));
 await page.waitForFunction(()=>document.getElementById('profile-equipment-body').textContent.includes('20.0%'));
 await page.waitForSelector('#collection-filter');
 assert.equal(await page.locator('#collection-list tbody tr').count(),45);
 assert.equal(await page.locator('#profile-content details, [data-profile-section]').count(),0);
 assert.equal(await page.locator('.roll-track').count(),1);
 await page.fill('#collection-filter','Mapped title 44');assert.equal(await page.locator('#collection-list tbody tr').count(),1);
 await page.waitForFunction(()=>document.getElementById('profile-social-body').textContent.includes('Partner'));
 await page.waitForFunction(()=>document.getElementById('profile-homestead-body').textContent.includes('No homestead reference'));
 await page.screenshot({path:path.join(__dirname,'player-desktop.png'),fullPage:true});
 await page.click('#profile-battles');assert.equal(await page.isVisible('#guild'),true);
 await page.click('[data-tab="players"]');fixture.guild={status:'none',name:null};
 await page.fill('#profile-query','Exact nickname');await page.click('#profile-search');
 await page.waitForFunction(()=>document.getElementById('profile-content').textContent.includes('No current guild'));
 assert.equal(await page.isEnabled('#profile-battles'),false);
 await page.waitForFunction(()=>document.getElementById('profile-homestead-state').className==='error');
 await page.waitForFunction(()=>document.getElementById('profile-equipment-body').textContent.includes('Mapped Sword'));
 assert(await page.isVisible('#profile-social'));
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:path.join(__dirname,'player-mobile.png'),fullPage:true});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);
 assert(requests.includes('/api/player?identifier=Exact%20nickname'));
 console.log('Player UI passed: bound prefill, profile escaping, exact-name search, no guild, profile-to-battles, automatic visible equipment/social/homestead, collection filter and mobile layout.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
