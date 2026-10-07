// Optional browser regression. Start the web server first; API responses are isolated fixtures.
const assert=require('node:assert/strict');
const {chromium,devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const step=(id,deps=[],state='succeeded')=>({id,label:id,scheduled:true,dependencies:deps,state,purpose:'目的',inputs:[],progress:'记录',result:'结果',attempts:[]});
(async()=>{const browser=await chromium.launch({executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
try{for(const mobile of [false,true]){
const page=await browser.newPage(mobile?{...devices['iPhone 13']}:{viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
let revision=0;
const makeRun=()=>({id:'fixture',mod_id:'并行流程测试',run_id:'fixture',source_version:'1',target_version:'2',state:'running',active_steps:['branch-2'],observed_at:1,groups:[{id:'g',steps:[step('root'),...Array.from({length:5},(_,i)=>step(`branch-${i}`,['root'],i===2?'running':'succeeded')),step('join',Array.from({length:5},(_,i)=>`branch-${i}`),'planned'),{...step('<img src=x onerror=alert(1)>',[],'pending'),scheduled:false},step('orphan',['absent'])].map(s=>({...s,result:`结果 ${revision}`}))}]});
await page.route('**/api/**',route=>{
 const path=new URL(route.request().url()).pathname;
 let data=path==='/api/session'?{authenticated:true}:makeRun();
 if(path.includes('/steps/')) data=makeRun().groups[0].steps.find(s=>s.id===decodeURIComponent(path.split('/').pop()));
 return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
});
await page.goto((process.env.MODPORT_WEB_TEST_URL || 'http://127.0.0.1:8877') + '/#run=fixture');await page.locator('.graph-node').first().waitFor();
if(mobile){
 assert(await page.locator('.graph-viewport').evaluate(n=>n.scrollWidth<=n.clientWidth+1));
 assert.equal(await page.locator('.graph-viewport').evaluate(n=>n.scrollTop),0);
 await page.setViewportSize({width:375,height:812});
 await page.waitForTimeout(100);
 assert(await page.locator('.graph-viewport').evaluate(n=>n.scrollWidth<=n.clientWidth+1));
}
if(!mobile){
 await page.setViewportSize({width:844,height:390});await page.waitForTimeout(100);
 await page.setViewportSize({width:375,height:812});await page.waitForTimeout(100);
 assert(await page.locator('.graph-viewport').evaluate(n=>n.scrollWidth<=n.clientWidth+1));
 await page.setViewportSize({width:1440,height:1000});await page.waitForTimeout(100);
}
assert.equal(await page.locator('.graph-node').count(),7);assert.equal(await page.locator('.graph-edge').count(),10);
assert.equal(await page.locator('.graph-shelf-node').count(),2);assert.equal(await page.locator('#stages img').count(),0);
const positions=await page.locator('.graph-node').evaluateAll(ns=>ns.filter(n=>n.dataset.step.startsWith('branch')).map(n=>n.offsetTop));assert.equal(new Set(positions).size,1);
await page.getByRole('button',{name:'适应宽度',exact:true}).click();
assert(await page.locator('.graph-viewport').evaluate(n=>n.scrollWidth<=n.clientWidth+1));
// Restore readable zoom and test scroll persistence under changing data.
for(let i=0;i<4;i++)await page.getByRole('button',{name:'放大',exact:true}).click();
const viewport=page.locator('.graph-viewport');
if(mobile){
 const manualZoom=await page.locator('.graph-zoom').textContent();
 await page.setViewportSize({width:844,height:390});await page.waitForTimeout(100);
 assert.equal(await page.locator('.graph-zoom').textContent(),manualZoom);
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(100);
 assert.equal(await page.locator('.graph-zoom').textContent(),manualZoom);
}
await viewport.evaluate(n=>{n.scrollLeft=150;n.scrollTop=180;n.focus();});
await page.waitForTimeout(100);
const before=await viewport.evaluate(n=>({x:n.scrollLeft,y:n.scrollTop}));
revision++;
await page.evaluate(()=>refresh());
const after=await viewport.evaluate(n=>({x:n.scrollLeft,y:n.scrollTop}));
assert.deepEqual(after,before);assert.equal(await page.evaluate(()=>document.activeElement.dataset.focus),'graph:viewport');
await page.getByRole('button',{name:/定位当前/}).click();
await page.locator('.graph-node[data-step="branch-2"]').click();
await page.locator('#step-panel h2').filter({hasText:'branch-2'}).waitFor();
await page.locator('.graph-node[data-step="branch-0"]').click();
await page.locator('#step-panel h2').filter({hasText:'branch-0'}).waitFor();
assert.equal(await page.locator('.graph-node.selected').getAttribute('data-step'),'branch-0');
if(!mobile){
 for(let i=0;i<5;i++)await page.getByRole('button',{name:'放大',exact:true}).click();
 await viewport.scrollIntoViewIfNeeded();const box=await viewport.boundingBox();
 await viewport.evaluate(n=>{n.scrollLeft=150;n.scrollTop=180;});
 const start=await viewport.evaluate(n=>n.scrollTop);
 await page.mouse.move(box.x+5,box.y+150);await page.mouse.down();await page.mouse.move(box.x+5,box.y+100,{steps:5});await page.mouse.up();
 assert(await viewport.evaluate(n=>n.scrollTop)>start);
}
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
assert.deepEqual(errors,[]);
await viewport.scrollIntoViewIfNeeded();await page.getByRole('button',{name:'适应宽度',exact:true}).click();
await page.screenshot({path:mobile?'/tmp/modport-graph-mobile-fixture.png':'/tmp/modport-graph-desktop-fixture.png'});
console.log(`PASS ${mobile?'mobile':'desktop'}: fork/join, fit, selection, refresh position/focus, safe text, no page overflow`);
// A continuation with inherited results must not become a huge horizontal row.
const inheritedSteps=[...Array.from({length:14},(_,i)=>({...step('历史结果'+i),inherited:true})),step('本轮任务',['历史结果0'],'running')];
await page.route('**/api/**',route=>{
 const path=new URL(route.request().url()).pathname;
 const data=path==='/api/runs'?{runs:[]}:path.includes('/steps/')?inheritedSteps.find(s=>s.id===decodeURIComponent(path.split('/').pop())):{id:'continuation',run_id:'continuation',state:'running',groups:[{steps:inheritedSteps}]};
 return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
});
await page.evaluate(()=>{location.hash='#run=continuation';});
await page.locator('.graph-node').filter({hasText:'本轮任务'}).waitFor();
assert.equal(await page.locator('.graph-node').count(),1);
assert.equal(await page.locator('.graph-edge').count(),0);
assert.equal(await page.locator('.graph-shelf-node').count(),14);
assert.equal(await page.locator('.graph-node .graph-input-note').textContent(),'沿用输入 1 项');
if(mobile){
 assert(await viewport.evaluate(n=>n.scrollWidth<=n.clientWidth+1));
 assert(await viewport.evaluate(n=>n.clientHeight<350));
 const rowTops=await page.locator('.graph-shelf-node').evaluateAll(ns=>ns.map(n=>n.getBoundingClientRect().top));
 assert.equal(new Set(rowTops).size,14);
}
await page.evaluate(()=>{location.hash='';});
await page.waitForFunction(()=>document.getElementById('stages').workflowResize===null);
await page.locator('#run-list .empty').waitFor();
assert.deepEqual(errors,[]);
console.log(`PASS ${mobile?'mobile':'desktop'}: continuation inherited results wrap outside the graph; input note and observer cleanup`);
await page.close();
}}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
