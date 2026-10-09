// CodeApp return on #717 (2026-10-09), adapted from its independent fixture: a Control link's open read that
// never answers shows the bounded notice without re-reading by itself; an explicit Try again opens the newer
// Card with nothing left busy and the superseded read's late answer never replaces it; a late answer without a
// retry still opens the Card; leaving the Card leaves nothing busy; a late refusal shows its own reason.
// The real panel, store and loadControlCard thunk in Chromium; the Hub is a synthetic private bridge.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { createServer, loadConfigFromFile } from 'vite'
import { launchBrowser } from './browser-fixture.mjs'
import { safeViteServer } from './safe-port.mjs'

const root = fileURLToPath(new URL('..', import.meta.url)).replace(/\/$/, '')
const base = `/@fs${root}`

test('a Control link open read: bounded, retry retires the old read, late answers and refusals behave', async (t) => {
const browser=await launchBrowser();
if(!browser){t.skip('no Chromium available; set PLAYWRIGHT_CHROMIUM_EXECUTABLE');return}
const fixture=`
import React from 'react';
import {createRoot} from 'react-dom/client';
import {Provider} from 'react-redux';
import {setConnectionsCallOperation} from '${base}/src/api/client.ts';
import {store} from '${base}/src/app/store.ts';
import {DelegatedAccessPanel} from '${base}/src/features/delegatedAccess/DelegatedAccessPanel.tsx';
import {loadDelegatedAccess} from '${base}/src/features/delegatedAccess/delegatedAccessSlice.ts';
import {loadDelegatedToKdcube} from '${base}/src/features/delegatedToKdcube/delegatedToKdcubeSlice.ts';
import {CONTROL_OPEN_READ_SECONDS} from '${base}/src/features/delegatedAccess/cardFreshness.ts';
const project='work:project:synthetic-pr717';
const requests=[],calls=[];
const makeCard=(revision,label)=>({access_id:'person-control-review',source:'control',issuer_kind:'project-person',issuer_ref:project,state:'active',label,card_revision:revision,client_id:'',grantor_subject:'synthetic-target',created_at:1759700000,expires_at:0,catalog_version:'synthetic-catalog',composition_mode:'and',resource_grants:{},resource_operations:{},account_scope:{},properties:{'connection_hub.project_person_control':{schema:'connection_hub.project_person_control.v1',project_ref:project,target_subject:'synthetic-target'}}});
setConnectionsCallOperation(async(_method,operation,data)=>{
 calls.push({operation,data});
 if(operation==='delegated_access_list')return {ok:true,platform_user_id:'synthetic-viewer',items:[],grant_options:[],resources:[]};
 if(operation==='delegated_to_kdcube_catalog')return {ok:true,enabled:true,providers:{},accounts:[]};
 if(operation==='project_person_control_get')return await new Promise(resolve=>requests.push({resolve,data}));
 return {ok:true,items:[],applications:[],resources:[]};
});
await store.dispatch(loadDelegatedAccess());
await store.dispatch(loadDelegatedToKdcube());
const root=createRoot(document.getElementById('root'));
const params={control_card_id:'person-control-review',project_ref:project,target_subject:'synthetic-target',target_label:'Synthetic Target'};
window.__mount=()=>root.render(React.createElement(Provider,{store},React.createElement(DelegatedAccessPanel,{openParams:params})));
window.__resolve=(index,revision,canEdit)=>requests[index].resolve({ok:true,access:makeCard(revision,'Synthetic revision '+revision),viewer:{can_edit:canEdit}});
window.__refuse=(index)=>requests[index].resolve({ok:false,error:'project_person_control_decided_by_admin',message:'Synthetic current read is refused'});
window.__leave=()=>root.unmount();
window.__state=()=>{const s=store.getState().delegatedAccess;return {busy:s.busy,controlReads:s.controlReads,revision:s.focusedCard?.card_revision,can_edit:s.focusedViewer?.can_edit,error:s.error,requestCount:requests.length}};
window.__calls=calls;
window.__bound=CONTROL_OPEN_READ_SECONDS;
window.__ready=true;
`;
const config=await loadConfigFromFile({command:'serve',mode:'test'},`${root}/vite.config.ts`);
const server=await createServer({root,configFile:false,logLevel:'silent',resolve:{alias:config.config.resolve.alias},optimizeDeps:{include:['react','react-dom/client','react-redux']},plugins:[{name:'private-pr717',enforce:'pre',resolveId(id){if(id==='virtual:pr717-review')return '\0pr717-review'},load(id){if(id==='\0pr717-review')return fixture}},react()],server:await safeViteServer()});
await server.listen();
const origin=server.resolvedUrls.local[0];
const failures=[];
const head='';
const pages=[];
async function pageForCase(){
 const page=await browser.newPage({viewport:{width:1280,height:1000}});pages.push(page);page.setDefaultTimeout(8000);
 await page.route('**/*',async route=>{const url=route.request().url();if(url===origin+'pr717-review.html')return route.fulfill({contentType:'text/html',body:await server.transformIndexHtml('/pr717-review.html','<div id="root"></div><script type="module" src="/@id/__x00__pr717-review"></script>')});if(url.endsWith('/csrf'))return route.fulfill({contentType:'application/json',body:'{"csrf_required":false}'});return url.startsWith(origin)?route.continue():route.abort()});
 await page.goto(origin+'pr717-review.html');
 await page.waitForFunction(()=>window.__ready===true);
 await page.clock.install();
 await page.evaluate(()=>window.__mount());
 await page.getByText('Opening the requested Card...',{exact:true}).waitFor();
 return page;
}
async function timeout(page){const bound=await page.evaluate(()=>window.__bound);await page.clock.fastForward(bound*1000+1);await page.getByRole('alert').filter({hasText:'Connection Hub did not answer in time. Try again.'}).waitFor();assert.equal((await page.evaluate(()=>window.__state())).requestCount,1,'timeout must not auto-retry');void bound}
try{
 const retry=await pageForCase();await timeout(retry);
 await retry.getByRole('button',{name:'Try again',exact:true}).click();
 await retry.waitForFunction(()=>window.__state().requestCount===2);
 await retry.evaluate(()=>window.__resolve(1,9,true));
 await retry.locator('.card-link-view').waitFor();
 const stateAfterRetry=await retry.evaluate(()=>window.__state());
 const editEnabled=await retry.locator('.card-link-view').getByRole('button',{name:'Edit',exact:true}).isEnabled();
 console.log('RETRY OBSERVATION',JSON.stringify({stateAfterRetry,editEnabled}));
 if(!editEnabled||stateAfterRetry.busy)failures.push('successful explicit retry leaves Edit disabled/busy behind the original never-answering read');
 await retry.evaluate(()=>window.__resolve(0,4,false));
 await retry.waitForFunction(()=>window.__state().controlReads===0);
 const stateAfterOld=await retry.evaluate(()=>window.__state());
 console.log('OLDER ANSWER OBSERVATION',JSON.stringify(stateAfterOld));
 if(stateAfterOld.revision!==9||stateAfterOld.can_edit!==true)failures.push('superseded original response replaces newer retry Card/viewer');
 const late=await pageForCase();await timeout(late);await late.evaluate(()=>window.__resolve(0,7,true));await late.locator('.card-link-view').waitFor();
 assert.equal(await late.locator('.card-link-view').getByRole('button',{name:'Edit',exact:true}).isEnabled(),true);console.log('PASS late answer without retry opens an editable Card');
 const leave=await pageForCase();await leave.evaluate(()=>window.__leave());await leave.clock.fastForward(60000);
 const afterLeave=await leave.evaluate(()=>window.__state());console.log('LEAVE OBSERVATION',JSON.stringify(afterLeave));
 if(afterLeave.busy||afterLeave.controlReads!==0)failures.push('leaving cancels only the timer; pending read still holds shared busy state');
 const refusal=await pageForCase();await timeout(refusal);await refusal.evaluate(()=>window.__refuse(0));await refusal.waitForFunction(()=>window.__state().controlReads===0);
 const refusalText=await refusal.getByRole('alert').allTextContents();console.log('LATE REFUSAL OBSERVATION',JSON.stringify(refusalText));
 if(!refusalText.join(' ').includes('Synthetic current read is refused'))failures.push('late server refusal remains hidden by timeout prose');
 assert.deepEqual(failures,[],JSON.stringify(failures));
 console.log('PASS full pending/retry/late/leave/refusal lifecycle; synthetic private bridge only');
}finally{await Promise.all(pages.map(p=>p.close()));await server.close();await browser.close()}
})
