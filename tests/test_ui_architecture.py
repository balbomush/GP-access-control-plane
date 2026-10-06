"""Focused A7/A8 browser-module lifetime tests on the pinned Node runtime."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gp_control_plane.web.ui import index_html


NODE = Path(r"B:\rkn killer\Codex-workspace\runtime\bgt-001\playwright-1.62.1-node-24.19.0-official\node\node.exe")
SCRIPTS = ROOT / "src" / "gp_control_plane" / "web" / "ui_assets" / "scripts"


class UiArchitectureModuleTests(unittest.TestCase):
    def test_transport_session_run_and_realtime_lifetimes(self) -> None:
        self.assertTrue(NODE.is_file(), f"pinned Node runtime is unavailable: {NODE}")
        harness = r'''
const fs = require('fs'); const vm = require('vm'); const base = process.argv[1];
function load(name, symbol) { vm.runInThisContext(fs.readFileSync(base + '/' + name, 'utf8') + `\nglobalThis.${symbol} = ${symbol};`, {filename:name}); }
load('api-client.js', 'ApiClient'); load('run-state.js', 'RunState'); load('realtime-controller.js', 'RealtimeController'); load('session-controller.js', 'SessionController');
const assert = (ok, message) => { if (!ok) throw new Error(message); };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
(async () => {
  let unauthorized = 0; let requests = [];
  const encoder = new TextEncoder(); const utf8 = encoder.encode('event: log\ndata: {"line":"ёж"}\n\n');
  const client = new ApiClient({getToken: () => 'token', onUnauthorized: () => { unauthorized += 1; }, fetch: async (url, init) => {
    requests.push({url, init});
    if (url === '/error') return new Response(JSON.stringify({error:{message:'bad', code:'bad_code', details:{x:1}}}), {status:400, statusText:'Bad', headers:{'Content-Type':'application/json'}});
    if (url === '/blob') return new Response('zip', {status:200, headers:{'Content-Disposition':'attachment; filename="a.zip"'}});
    if (url === '/sse') return new Response(new ReadableStream({start(c) { c.enqueue(utf8.slice(0, 29)); c.enqueue(utf8.slice(29)); c.close(); }}), {status:200});
    if (url === '/abort') throw Object.assign(new Error('aborted'), {name:'AbortError'});
    if (url === '/401') return new Response('', {status:401});
    return new Response(JSON.stringify({ok:true}), {status:200, headers:{'Content-Type':'application/json'}});
  }});
  assert((await client.getJson('/ok')).ok, 'JSON request failed');
  const multipart = new FormData(); multipart.append('file', new Blob(['zip']), 'backup.zip');
  await client.request('/multipart', {method:'POST', body:multipart});
  const multipartRequest = requests.at(-1);
  assert(multipartRequest.init.body === multipart && !Object.keys(multipartRequest.init.headers).some(key => key.toLowerCase() === 'content-type'), 'multipart body identity or boundary headers changed');
  try { await client.postJson('/error', {x:1}); assert(false, 'JSON error did not reject'); } catch (error) { assert(error.status === 400 && error.code === 'bad_code' && error.details.x === 1, 'JSON error shape changed'); }
  const binary = await client.blob('/blob'); assert((await binary.blob.text()) === 'zip', 'blob response changed');
  const events = []; await client.streamSse('/sse', {onEvent: (event, data) => events.push([event, data])});
  assert(events.length === 1 && events[0][1].includes('ёж'), 'split UTF-8 SSE frame was not decoded');
  try { await client.getJson('/abort', {signal:new AbortController().signal}); assert(false, 'abort did not propagate'); } catch (error) { assert(error.name === 'AbortError', 'abort semantics changed'); }
  await client.request('/401'); assert(unauthorized === 1, '401 did not notify session owner');
  try { await client.postJson('/401', {}); assert(false, '401 JSON error did not reject'); } catch (error) { assert(error.status === 401 && !error.staleSession, '401 JSON error contract changed after logout'); }
  assert(unauthorized === 2, '401 JSON request did not notify session owner');
  assert(requests.every(item => item.init.credentials === 'same-origin' && item.init.headers.Authorization === 'Bearer token'), 'auth/same-origin transport changed');
  let requestEpoch = 1; const sessionAbort = new AbortController();
  const guarded = new ApiClient({getToken: () => 'token', getEpoch: () => requestEpoch, isEpochCurrent: (epoch) => epoch === requestEpoch, getSignal: () => sessionAbort.signal, fetch: (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'})), {once:true}))});
  const staleRequest = guarded.getJson('/held'); sessionAbort.abort(); requestEpoch += 1;
  try { await staleRequest; assert(false, 'session cancellation did not abort an in-flight request'); } catch (error) { assert(error.name === 'AbortError', 'session cancellation changed request semantics'); }

  // Headers are not completion: composed caller/session signals stay attached
  // while JSON and blob bodies are pending, then detach their listeners.
  const caller = new AbortController(), lifetime = new AbortController();
  const listenerCounts = signal => { let added = 0, removed = 0; const add = signal.addEventListener.bind(signal), remove = signal.removeEventListener.bind(signal); signal.addEventListener = (...args) => { if (args[0] === 'abort') added += 1; return add(...args); }; signal.removeEventListener = (...args) => { if (args[0] === 'abort') removed += 1; return remove(...args); }; return () => ({added, removed}); };
  const callerCounts = listenerCounts(caller.signal), lifetimeCounts = listenerCounts(lifetime.signal);
  let bodySignal, bodyStarted;
  const heldBody = new ApiClient({getSignal: () => lifetime.signal, fetch: async (_url, init) => {
    bodySignal = init.signal;
    return {ok:true, status:200, json: () => new Promise((_resolve, reject) => { bodyStarted = true; init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted body'), {name:'AbortError'})), {once:true}); })};
  }});
  const heldJson = heldBody.getJson('/headers-arrived', {signal:caller.signal});
  while (!bodyStarted) await tick(); caller.abort();
  try { await heldJson; assert(false, 'caller abort after JSON headers resolved'); } catch (error) { assert(error.name === 'AbortError', 'JSON body abort was converted into success'); }
  assert(bodySignal.aborted, 'composed JSON signal was released before body completion');
  assert(callerCounts().added === callerCounts().removed && lifetimeCounts().added === lifetimeCounts().removed, 'JSON combined-signal listeners leaked');
  let blobStarted, blobSignal;
  const blobCaller = new AbortController(), blobLifetime = new AbortController();
  const heldBlob = new ApiClient({getSignal: () => blobLifetime.signal, fetch: async (_url, init) => ({ok:true, status:200, blob: () => new Promise((_resolve, reject) => { blobStarted = true; blobSignal = init.signal; init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted blob'), {name:'AbortError'})), {once:true}); })})});
  const pendingBlob = heldBlob.blob('/blob-headers', {signal:blobCaller.signal}); while (!blobStarted) await tick(); blobLifetime.abort();
  try { await pendingBlob; assert(false, 'session abort after blob headers resolved'); } catch (error) { assert(error.name === 'AbortError', 'blob body abort changed semantics'); }
  assert(blobSignal.aborted, 'composed blob signal was released before body completion');
  const malformed = new ApiClient({fetch: async () => ({ok:true, status:200, json: async () => { throw new Error('malformed'); }})});
  assert(JSON.stringify(await malformed.postJson('/malformed', {})) === '{}', 'successful malformed JSON no longer used the established empty fallback');

  // Password completion belongs to its source epoch; cancelling its response
  // body after a replacement session must not log the replacement out.
  let passwordToken = 'old', passwordController, passwordBodyStarted = false, passwordErrors = 0;
  const passwordApi = new ApiClient({getToken: () => passwordToken, getEpoch: () => passwordController.epoch(), isEpochCurrent: epoch => passwordController.isCurrent(epoch), getSignal: () => passwordController.signal(), fetch: async (_url, init) => ({ok:true, status:200, json: () => new Promise((_resolve, reject) => { passwordBodyStarted = true; init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted password'), {name:'AbortError'})), {once:true}); })})});
  passwordController = new SessionController({api:passwordApi, token:{get:() => passwordToken, clear:() => {passwordToken='';}, store:() => {}}, realtime:{dispose(){},start(){}}, ui:{boot(){},ready(){},login(){}}});
  const oldPassword = passwordController.changePassword({}, () => { passwordErrors += 1; }); while (!passwordBodyStarted) await tick();
  passwordController.logout(); passwordToken = 'new'; await passwordController.bootstrap(async () => ({}), () => {}); const replacementEpoch = passwordController.epoch();
  assert((await oldPassword) === false && passwordToken === 'new' && passwordController.epoch() === replacementEpoch && passwordErrors === 0, 'old password continuation changed the replacement session');

  const view = {finderRuns: [], status: null, finderLog: null}; const runs = new RunState(view);
  const oldGeneration = runs.acknowledge('old'); const newGeneration = runs.acknowledge('new');
  assert(!runs.acceptLog({run_id:'old'}, false, (_, value) => value), 'old run log overwrote accepted run');
  runs.mergeHistory({runs:[{run_id:'old', status:'stopped'}]}, true, rows => rows);
  assert(runs.isGenerationCurrent(newGeneration) && !runs.isGenerationCurrent(oldGeneration), 'old history cleared newer run');
  runs.mergeStatus({current_run:{run_id:'old', status:'running'}});
  assert(view.status.current_run.run_id === 'new', 'old status replaced accepted run');

  const logView = {finderRuns: [], status:null, finderLog:null}; const logRuns = new RunState(logView);
  logRuns.acknowledge('new'); logRuns.acceptLog({run_id:'new',stdout_tail:'base\n'}, false, (_old, next) => next);
  const firstLogRequest = logRuns.captureLogRequest(), secondLogRequest = logRuns.captureLogRequest(); let merged = 0;
  const append = (previous, next) => { merged += 1; return {...next, stdout_tail:(previous.stdout_tail || '') + next.stdout_append}; };
  assert(logRuns.acceptLog({run_id:'new',stdout_append:'delta\n'}, true, append, firstLogRequest), 'first incremental log was rejected');
  assert(!logRuns.acceptLog({run_id:'new',stdout_append:'delta\n'}, true, append, secondLogRequest) && merged === 1 && logView.finderLog.stdout_tail === 'base\ndelta\n', 'same-offset incremental log was appended twice');
  logRuns.mergeHistory({runs:[{run_id:'new',status:'stopped'}]}, true, rows => rows);
  assert(logRuns.acceptLog({run_id:'new',status:'stopped',stdout_tail:'base\nFINAL\n'}, false, (_old, next) => next), 'valid terminal log was rejected after matching history');
  assert(!logRuns.acceptLog({run_id:'old',stdout_tail:'old'}, false, (_old, next) => next), 'late old log resurrected after terminal history');
  logRuns.mergeStatus({current_run:{run_id:'old',status:'running'}});
  assert(!logRuns.busy() && !logRuns.current(), 'late old status resurrected after terminal history');
  logRuns.mergeHistory({runs:[{run_id:'external',status:'running'}]}, true, rows => rows);
  logRuns.mergeStatus({current_run:{run_id:'external',status:'running'}});
  assert(logRuns.busy() && logRuns.current().run_id === 'external', 'history-evidenced external run remained barred after terminal history');
  const extentView = {finderRuns: [],status:null,finderLog:null}; const extentRuns = new RunState(extentView);
  extentRuns.acknowledge('extent'); extentRuns.acceptLog({run_id:'extent',stdout_log:'extent.out',stdout_size:5,stdout_tail:'base\n'}, false, (_old, next) => next);
  const extentOne = extentRuns.captureLogRequest(), extentTwo = extentRuns.captureLogRequest();
  const extentMerge = (previous, next) => ({...next, stdout_tail:next.stdout_tail || (previous.stdout_tail || '') + (next.stdout_append || '')});
  assert(extentRuns.acceptLog({run_id:'extent',stdout_log:'extent.out',stdout_size:11,stdout_append:'first\n'}, true, extentMerge, extentOne), 'first extent was rejected');
  assert(extentRuns.acceptLog({run_id:'extent',stdout_log:'extent.out',stdout_size:18,stdout_append:'first\nsecond\n'}, true, extentMerge, extentTwo) && extentView.finderLog.stdout_size === 18 && extentView.finderLog.stdout_tail === 'base\nfirst\nsecond\n', 'larger same-offset extent lost newly arrived output');

  // A status emitted before its matching runs event is a normal SSE order.  It
  // must become current once history proves it, while a retired status arriving
  // later must leave that accepted external current intact.
  const orderedView = {finderRuns: [],status:null,finderLog:null}; const ordered = new RunState(orderedView);
  ordered.acknowledge('local'); ordered.mergeHistory({runs:[{run_id:'local',status:'stopped'}]}, true, rows => rows);
  assert(!ordered.mergeStatus({current_run:{run_id:'external',status:'running'}}), 'unproven external status was accepted too early');
  ordered.mergeHistory({runs:[{run_id:'external',status:'running'}]}, true, rows => rows);
  assert(ordered.busy() && ordered.current().run_id === 'external', 'status-before-runs did not recover external current');
  ordered.mergeStatus({current_run:{run_id:'local',status:'running'}});
  assert(ordered.busy() && ordered.current().run_id === 'external', 'retired status cleared recognized external current');

  // Same-offset replies are independently reconciled per stream.  The later
  // response may grow one stream while repeating an older extent of the other.
  const overlapView = {finderRuns: [],status:null,finderLog:null}; const overlap = new RunState(overlapView);
  const productionMerge = (previous, next) => {
    const join = (left, right) => !left || !right || left.endsWith('\n') || right.startsWith('\n') ? left + right : left + '\n' + right;
    return {...next, stdout_tail:next.stdout_tail || join(previous.stdout_tail || '', next.stdout_append || ''), stderr_tail:next.stderr_tail || join(previous.stderr_tail || '', next.stderr_append || '')};
  };
  overlap.acknowledge('overlap'); overlap.acceptLog({run_id:'overlap',stdout_log:'out',stdout_size:5,stdout_tail:'base\n',stderr_log:'err',stderr_size:5,stderr_tail:'base\n'}, false, productionMerge);
  const overlapOne=overlap.captureLogRequest(), overlapTwo=overlap.captureLogRequest();
  assert(overlap.acceptLog({run_id:'overlap',stdout_log:'out',stdout_size:5,stdout_append:'',stderr_log:'err',stderr_size:11,stderr_append:'err-1\n'},true,productionMerge,overlapOne),'first stderr extent rejected');
  assert(overlap.acceptLog({run_id:'overlap',stdout_log:'out',stdout_size:11,stdout_append:'out-1\n',stderr_log:'err',stderr_size:11,stderr_append:'err-1\n'},true,productionMerge,overlapTwo) && overlapView.finderLog.stdout_tail === 'base\nout-1\n' && overlapView.finderLog.stderr_tail === 'base\nerr-1\n' && overlapView.finderLog.stderr_size === 11,'per-stream overlap duplicated or regressed stderr');
  const utfView={finderRuns:[],status:null,finderLog:null}; const utf=new RunState(utfView); utf.acknowledge('utf'); utf.acceptLog({run_id:'utf',stdout_log:'out',stdout_size:0,stdout_tail:'',stderr_log:'err',stderr_size:0,stderr_tail:''},false,productionMerge);
  const utfOne=utf.captureLogRequest(), utfTwo=utf.captureLogRequest();
  assert(utf.acceptLog({run_id:'utf',stdout_log:'out',stdout_size:1,stdout_append:'�',stderr_log:'err',stderr_size:0},true,productionMerge,utfOne),'split UTF-8 prefix rejected');
  assert(utf.acceptLog({run_id:'utf',stdout_log:'out',stdout_size:3,stdout_append:'€',stderr_log:'err',stderr_size:0},true,productionMerge,utfTwo) && utfView.finderLog.stdout_tail === '€','overlap completed UTF-8 character was corrupted');

  const incompleteEvents = []; await new ApiClient({fetch: async () => new Response('event: status\ndata: {"current_run":')}).streamSse('/eof', {onEvent: (event, data) => incompleteEvents.push([event, data])});
  assert(incompleteEvents.length === 0, 'unterminated SSE EOF frame was dispatched');

  let streamCalls = 0, fallbackCalls = 0, eventCalls = 0; const streamHandlers = [], timeouts = [], intervals = [], cleared = [];
  const realtime = new RealtimeController({api:{streamSse: async (_url, options) => { streamCalls += 1; streamHandlers.push(options); options.onOpen(); throw new Error('closed'); }}, url: () => '/events', onEvent: () => { eventCalls += 1; }, fallback: () => { fallbackCalls += 1; }, isActive: () => true, setTimeout: (fn) => { const item={fn}; timeouts.push(item); return item; }, clearTimeout: (item) => cleared.push(item), setInterval: (fn) => { const item={fn}; intervals.push(item); return item; }, clearInterval: (item) => cleared.push(item)});
  realtime.start(); await tick(); assert(timeouts.length === 1 && intervals.length === 1, 'reconnect/fallback were not scheduled once');
  timeouts[0].fn(); await tick(); assert(streamCalls === 2 && timeouts.length === 2 && intervals.length === 1, 'reconnect duplicated a stream or fallback timer');
  streamHandlers[0].onEvent('status', '{}'); assert(eventCalls === 0, 'previous connection delivered an event after reconnect');
  intervals[0].fn(); assert(fallbackCalls === 1, 'fallback polling did not run while disconnected');
  realtime.dispose(); assert(cleared.length >= 2, 'dispose left reconnect or polling timer active');
  assert(eventCalls === 0, 'unexpected stale stream event');

  let resolveFirst, resolveSecond, applied = [], clears = 0, disposed = 0;
  const session = new SessionController({api:{postJson: async () => ({access_token:'new'})}, token:{get:() => 'token', store:() => {}, clear:() => {clears += 1;}}, realtime:{dispose:() => {disposed += 1;}, start:() => {}}, ui:{boot:() => {}, ready:() => {}, login:() => {}}});
  const first = session.bootstrap(() => new Promise(resolve => {resolveFirst = resolve;}), value => applied.push(value));
  const second = session.bootstrap(() => new Promise(resolve => {resolveSecond = resolve;}), value => applied.push(value));
  resolveFirst('old'); await tick(); resolveSecond('new'); await second; await first;
  assert(applied.length === 1 && applied[0] === 'new', 'stale bootstrap applied after a newer session');
  session.logout(); assert(clears === 1 && disposed >= 3, 'logout did not cancel the session lifetime');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
'''
        completed = subprocess.run(
            [str(NODE), "-e", harness, str(SCRIPTS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)

    def test_native_transport_error_and_cancellation_contracts(self) -> None:
        """Use native fetch so response-body signal semantics cannot be faked."""
        harness = r'''
const fs=require('fs'),vm=require('vm'),http=require('http'),base=process.argv[1];
vm.runInThisContext(fs.readFileSync(base+'/api-client.js','utf8')+'\nglobalThis.ApiClient=ApiClient;');
const assert=(ok,message)=>{if(!ok)throw new Error(message)}; let held500, held500Ready, held401, held401Ready;
const server=http.createServer((request,response)=>{
  if(request.url==='/401'){response.writeHead(401,{'Content-Type':'application/json'});response.end(JSON.stringify({error:{message:'session expired detail',code:'unauthorized',details:{test:1}}}));return;}
  if(request.url==='/hold401'){response.writeHead(401,{'Content-Type':'application/json'});response.write('{"error":');held401=response;held401Ready();return;}
  if(request.url==='/malformed'){response.writeHead(200,{'Content-Type':'application/json'});response.end('not json');return;}
  if(request.url==='/hold500'){response.writeHead(500,{'Content-Type':'application/json'});response.write('{"error":');held500=response;held500Ready();return;}
  response.writeHead(200,{'Content-Type':'application/json'});response.end('{"ok":true}');
});
(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin='http://127.0.0.1:'+server.address().port;
try {
  for(const method of ['getJson','postJson']) { let token='old',session=new AbortController(); const api=new ApiClient({getToken:()=>token,getSignal:()=>session.signal,onUnauthorized:()=>{token='';session.abort();}}); let error; try {await (method==='getJson'?api.getJson(origin+'/401'):api.postJson(origin+'/401',{}));} catch(value){error=value;} assert(token==='',method+' did not log out after body consumption'); if(method==='getJson') assert(error.message.includes('session expired detail'), 'GET 401 text was lost'); else assert(error.status===401&&error.code==='unauthorized'&&error.details.test===1&&error.data.error.message==='session expired detail','POST 401 details were lost'); }
  assert(JSON.stringify(await new ApiClient().postJson(origin+'/malformed',{}))==='{}','malformed successful POST did not retain fallback');
  const timeout=new DOMException('Caller deadline reached','TimeoutError'),pre=new AbortController(),live=new AbortController();pre.abort(timeout); let preError; try {await new ApiClient({getSignal:()=>live.signal}).getJson(origin+'/ok',{signal:pre.signal});} catch(error){preError=error;} assert(preError===timeout&&preError.name==='TimeoutError','pre-aborted caller reason was not preserved');
  const caller=new AbortController(),session=new AbortController();const heldGate=new Promise(resolve=>held500Ready=resolve);const pending=new ApiClient({getSignal:()=>session.signal}).postJson(origin+'/hold500',{}, {signal:caller.signal});await heldGate;const reason=new DOMException('deadline','TimeoutError');caller.abort(reason);held500.end('true}');let cancel;try{await pending;}catch(error){cancel=error;}assert(cancel===reason&&cancel.name==='TimeoutError','HTTP-500 body cancellation became a synthetic HTTP error');
  // A 401 is still a pending response body.  Replacing the owning session while
  // it is held must reject it as stale, without its deferred cleanup logging
  // out the replacement token.
  for (const method of ['getJson','postJson']) { let token='old',epoch=1,current=new AbortController(),logouts=[]; const heldGate=new Promise(resolve=>held401Ready=resolve); const api=new ApiClient({getToken:()=>token,getEpoch:()=>epoch,isEpochCurrent:value=>value===epoch,getSignal:()=>current.signal,onUnauthorized:()=>{logouts.push(token);token='';current.abort();}}); const old=method==='getJson'?api.getJson(origin+'/hold401'):api.postJson(origin+'/hold401',{}); await heldGate; current.abort(new DOMException('old session','AbortError')); epoch+=1; token='replacement'; current=new AbortController(); held401.end('true}}'); let stale;try{await old;}catch(error){stale=error;} assert(stale&&stale.staleSession&&token==='replacement'&&logouts.length===0,method+' old 401 body logged out replacement session'); }
  const held401Caller=new AbortController(), held401Gate=new Promise(resolve=>held401Ready=resolve);const pending401=new ApiClient().postJson(origin+'/hold401',{}, {signal:held401Caller.signal});await held401Gate;held401Caller.abort(new DOMException('deadline','TimeoutError'));held401.end('true}}');let cancel401;try{await pending401;}catch(error){cancel401=error;}assert(cancel401&&cancel401.name==='TimeoutError'&&cancel401.status===undefined,'HTTP-401 body cancellation became synthetic Unauthorized');
  console.log('native 401 replacement/malformed/reason/500 cancellation: PASS');
} finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
'''
        completed = subprocess.run(
            [str(NODE), "-e", harness, str(SCRIPTS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)

    def test_rendered_browser_401_replacement_and_body_abort_phases(self) -> None:
        """Exercise the public rendered adapters with real Chromium response bodies."""
        runtime = NODE.parents[1]
        harness = r'''
const fs=require('fs'),http=require('http'),path=require('path');const {chromium}=require(path.join(process.argv[2],'playwright-project/node_modules/playwright'));const html=fs.readFileSync(process.argv[1]);
const assert=(ok,message)=>{if(!ok)throw new Error(message)};let hold401,hold401Ready,hold200,hold200Ready;
const server=http.createServer((request,response)=>{const url=new URL(request.url,'http://localhost').pathname;
  if(url==='/'){response.writeHead(200,{'Content-Type':'text/html'});response.end(html);return;}
  if(url==='/hold401'){response.writeHead(401,{'Content-Type':'application/json'});response.flushHeaders();response.write('{"error":');hold401=response;hold401Ready();return;}
  if(url==='/hold200'){response.writeHead(200,{'Content-Type':'application/json'});response.flushHeaders();response.write('{"ok":');hold200=response;hold200Ready();return;}
  if(url.includes('/events/stream')){response.writeHead(200,{'Content-Type':'text/event-stream'});response.end();return;}
  response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify(url.includes('/status')?{state:'idle',current_run:null,zapret2:{ready:true}}:{}));
});
(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin='http://127.0.0.1:'+server.address().port;const browser=await chromium.launch({headless:true});const page=await browser.newPage();
try {await page.goto(origin+'/');await page.evaluate(async()=>{localStorage.setItem(AUTH_TOKEN_KEY,'old');await startAuthenticatedUi();});
  const got401=new Promise(resolve=>hold401Ready=resolve);await page.evaluate(()=>{window.__old401=getJson('/hold401').then(()=>null,error=>({name:error.name,stale:Boolean(error.staleSession)}));});await got401;
  await page.evaluate(async()=>{logout();localStorage.setItem(AUTH_TOKEN_KEY,'replacement');await startAuthenticatedUi();});hold401.end('true}}');
  const replacement=await page.evaluate(async()=>{const login=document.getElementById('login-screen'),shell=document.getElementById('app-shell');return {old:await window.__old401,token:authToken(),epoch:currentSessionEpoch(),loginExists:Boolean(login),loginHidden:Boolean(login&&login.hidden),shellExists:Boolean(shell),shellVisible:Boolean(shell&&!shell.hidden)};});
  assert(replacement.old.stale&&replacement.token==='replacement'&&replacement.loginExists&&replacement.loginHidden&&replacement.shellExists&&replacement.shellVisible,'old rendered 401 logged out replacement session or left an invalid shell/login view');
  const got200=new Promise(resolve=>hold200Ready=resolve);await page.evaluate(()=>{const Client=eval('ApiClient');const controller=new AbortController();window.__bodyAbort=(new Client()).getJson('/hold200',{signal:controller.signal}).then(()=>null,error=>({name:error.name,status:error.status}));window.__abortBody=()=>controller.abort(new DOMException('deadline','TimeoutError'));});await got200;await page.evaluate(()=>window.__abortBody());hold200.end('true}');
  const chromiumAbort=await page.evaluate(()=>window.__bodyAbort);assert(chromiumAbort.name==='AbortError'&&chromiumAbort.status===undefined,'Chromium post-header body AbortError was substituted');
  const gotPost401=new Promise(resolve=>hold401Ready=resolve);await page.evaluate(()=>{const Client=eval('ApiClient');const controller=new AbortController();window.__post401=(new Client()).postJson('/hold401',{}, {signal:controller.signal}).then(()=>null,error=>({name:error.name,status:error.status}));window.__abort401=()=>controller.abort(new DOMException('deadline','TimeoutError'));});await gotPost401;await page.evaluate(()=>window.__abort401());hold401.end('true}}');
  const postAbort=await page.evaluate(()=>window.__post401);assert(postAbort.name==='AbortError'&&postAbort.status===undefined,'held 401 POST body became synthetic Unauthorized');
} finally {await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as temporary_directory:
            html_path = Path(temporary_directory) / "ui.html"
            html_path.write_text(index_html(), encoding="utf-8")
            completed = subprocess.run(
                [str(NODE), "-e", harness, str(html_path), str(runtime)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=os.environ | {"PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")},
                timeout=45,
            )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)

    def test_legacy_actions_ignore_replaced_session_completions(self) -> None:
        """Exercise the real adapters, not just the extracted owner classes."""
        self.assertTrue(NODE.is_file(), f"pinned Node runtime is unavailable: {NODE}")
        runtime = NODE.parents[1]
        harness = r'''
const fs = require('fs'), path = require('path');
const {chromium} = require(path.join(process.argv[2], 'playwright-project/node_modules/playwright'));
const assert = (ok, message) => { if (!ok) throw new Error(message); };
(async () => {
  const browser = await chromium.launch({headless:true}); const page = await browser.newPage();
  try {
    const html = fs.readFileSync(process.argv[1], 'utf8');
    await page.route('http://race.local/**', route => route.fulfill({status:200, contentType:'text/html', body:html}));
    await page.addInitScript(() => {
      window.fetch = async (url, init={}) => {
        const value = String(url);
        const reply = data => new Response(JSON.stringify(data), {headers:{'Content-Type':'application/json'}});
        if (value.includes('/events/stream')) return new Response(new ReadableStream({start(controller) { init.signal?.addEventListener('abort', () => controller.close(), {once:true}); }}));
        if (value.includes('/api/web/status')) return reply({state:'idle', current_run:null, zapret2:{ready:true}});
        if (value.includes('history-page')) return reply({runs:[], total:0});
        if (value.includes('/runs/latest-log')) return reply({});
        return reply({});
      };
    });
    await page.goto('http://race.local/');
    const details = await page.evaluate(async () => {
      localStorage.setItem(AUTH_TOKEN_KEY, 'old'); await startAuthenticatedUi(); const normal = window.fetch;
      const settingsUrl = apiEndpoint('core', 'saveRunSettings');
      const replace = async token => { logout(); localStorage.setItem(AUTH_TOKEN_KEY, token); await startAuthenticatedUi(); };
      let rejectOld, entered; const enteredOld = new Promise(resolve => entered = resolve);
      window.fetch = (url, init) => String(url) === settingsUrl ? new Promise((_resolve, reject) => { rejectOld = reject; entered(); }) : normal(url, init);
      const save = saveSettings(); await enteredOld; await replace('new'); rejectOld(new DOMException('old abort', 'AbortError')); await save;
      const staleCatchMessage = document.getElementById('message').textContent;

      let releaseBody, bodyEntered; const bodyGate = new Promise(resolve => bodyEntered = resolve);
      window.fetch = (url, init) => String(url) === settingsUrl ? Promise.resolve({ok:true, status:200, json:() => new Promise(resolve => { releaseBody = resolve; bodyEntered(); })}) : normal(url, init);
      const heldSave = saveSettings(); await bodyGate; await replace('third'); releaseBody({curl_max_time:99, old_session_sentinel:true}); await heldSave;
      const lateSettings = {settings:state.settings, message:document.getElementById('message').textContent};

      let releaseBlob, blobEntered; const blobGate = new Promise(resolve => blobEntered = resolve);
      window.fetch = (url, init) => String(url) === '/held-download' ? Promise.resolve({ok:true, status:200, headers:new Headers(), blob:() => new Promise(resolve => { releaseBlob = resolve; blobEntered(); })}) : normal(url, init);
      const oldDownload = downloadBackup('/held-download', 'review'); await blobGate; await replace('download-replacement'); releaseBlob(new Blob(['old archive'])); await oldDownload;
      const staleDownloadMessage = document.getElementById('message').textContent;

      runState.acknowledge('log-run'); runState.acceptLog({run_id:'log-run',stdout_log:'same.out',stdout_size:5,stdout_tail:'base\n'}, false, (_old, next) => next);
      const logReplies = [], logUrls = [];
      window.fetch = (url, init) => String(url).includes('/runs/latest-log') ? new Promise(resolve => { logUrls.push(String(url)); logReplies.push(resolve); }) : normal(url, init);
      const first = refreshLog(true), second = refreshLog(true); await new Promise(resolve => setTimeout(resolve, 0));
      const delta = () => new Response(JSON.stringify({run_id:'log-run',stdout_log:'same.out',stdout_size:11,stdout_append:'delta\n'}), {headers:{'Content-Type':'application/json'}});
      logReplies[0](delta()); await first; logReplies[1](delta()); await second;
      const logTail = state.finderLog.stdout_tail;

      acknowledgeRun('completed'); mergeStatusPayload({current_run:{run_id:'completed',status:'running'},zapret2:{ready:true}});
      runState.acceptLog({run_id:'completed',status:'running',stdout_tail:'base\n'},false,mergeLogPayload);
      mergeRunPage({runs:[{run_id:'completed',status:'stopped'}],total:1,offset:0,has_more:false},true);
      const terminalLog = runState.acceptLog({run_id:'completed',status:'stopped',stdout_tail:'base\nFINAL\n'},false,mergeLogPayload);
      mergeRunPage({runs:[{run_id:'external',status:'running'}],total:2,offset:0,has_more:false},true);
      mergeStatusPayload({current_run:{run_id:'external',status:'running'},zapret2:{ready:true}});
      const externalLog = runState.acceptLog({run_id:'external',status:'running',stdout_tail:'EXTERNAL\n'},false,mergeLogPayload);
      const terminalFlow = {terminalLog, externalLog, busy:isBusy(), current:currentRun()?.run_id, displayed:state.finderLog.stdout_tail};

      // The connected SSE path can publish status before runs and never repeat
      // it.  History must promote that pending external status; a late retired
      // local status must then leave it visible and busy.
      acknowledgeRun('sse-local'); mergeRunPage({runs:[{run_id:'sse-local',status:'stopped'}],total:1,offset:0,has_more:false},true);
      handleStatusEvent({current_run:{run_id:'sse-external',status:'running'},zapret2:{ready:true}});
      mergeRunPage({runs:[{run_id:'sse-external',status:'running'}],total:1,offset:0,has_more:false},true);
      const statusBeforeRuns = {current:currentRun()?.run_id,busy:isBusy()};
      handleStatusEvent({current_run:{run_id:'sse-local',status:'running'},zapret2:{ready:true}});
      const retiredStatus = {current:currentRun()?.run_id,busy:isBusy()};

      // Rendered terminal regression: the real status->runs SSE path promotes
      // external current through refreshRuns(), then a log GET is deliberately
      // held.  The visible live panel and its Stop action must already agree.
      document.getElementById('tab-terminal').click();
      acknowledgeRun('panel-local'); mergeStatusPayload({current_run:{run_id:'panel-local',status:'running'},zapret2:{ready:true}});
      mergeRunPage({runs:[{run_id:'panel-local',status:'stopped'}],total:1,offset:0,has_more:false},true);
      mergeStatusPayload({current_run:null,zapret2:{ready:true}});
      handleRealtimeEvent('status', JSON.stringify({current_run:{run_id:'panel-external',status:'running'},zapret2:{ready:true}}));
      let releasePanelLog, panelLogEntered; const panelLogGate=new Promise(resolve=>panelLogEntered=resolve), panelFetch=window.fetch;
      window.fetch=(url,init)=>{
        const value=String(url), reply=data=>Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
        if(value.includes('history-page')) return reply({runs:[{run_id:'panel-local',status:'stopped'},{run_id:'panel-external',status:'running'}],total:2,offset:0,has_more:false});
        if(value.includes('/runs/latest-log')) return new Promise(resolve=>{releasePanelLog=resolve;panelLogEntered();});
        return normal(url,init);
      };
      handleRealtimeEvent('runs','{}'); await new Promise(resolve=>setTimeout(resolve,0)); await new Promise(resolve=>setTimeout(resolve,0));
      handleRealtimeEvent('log','{}'); await panelLogGate;
      const panelWhileLogHeld={tab:state.activeTab,current:currentRun()?.run_id,busy:isBusy(),label:liveRunStatusText(),text:document.getElementById('live-run-panel').textContent,stopDisabled:document.querySelector('#live-run-panel [data-action=stop-current]').disabled};
      releasePanelLog(new Response(JSON.stringify({run_id:'panel-external',status:'running',stdout_tail:'EXTERNAL LIVE\n'}),{headers:{'Content-Type':'application/json'}})); await new Promise(resolve=>setTimeout(resolve,0));
      window.fetch=panelFetch;

      // This is the post-Start refresh caller: history terminal convergence and
      // the matching final log arrive in one response batch.  A delayed running
      // full-log request captured beforehand must not overwrite that final log.
      const originalRefreshFetch = window.fetch;
      const fastGeneration = acknowledgeRun('fast-terminal'); mergeStatusPayload({current_run:{run_id:'fast-terminal',status:'running'},zapret2:{ready:true}});
      runState.acceptLog({run_id:'fast-terminal',status:'running',stdout_log:'fast.out',stdout_size:5,stdout_tail:'base\n'},false,mergeLogPayload);
      const lateFastRequest = runState.captureLogRequest();
      window.fetch = (url, init) => {
        const value=String(url); const reply=data=>Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
        if(value.includes('history-page')) return reply({runs:[{run_id:'fast-terminal',status:'stopped'}],total:1,offset:0,has_more:false});
        if(value.includes('/runs/latest-log')) return reply({run_id:'fast-terminal',status:'stopped',stdout_log:'fast.out',stdout_size:11,stdout_tail:'base\nFINAL\n'});
        if(value.includes('/status')) return reply({current_run:null,zapret2:{ready:true}});
        return originalRefreshFetch(url,init);
      };
      await refreshAcknowledgedRun(fastGeneration);
      const finalAfterRefresh=state.finderLog.stdout_tail;
      const delayedRunningAccepted=runState.acceptLog({run_id:'fast-terminal',status:'running',stdout_log:'fast.out',stdout_size:5,stdout_tail:'base\n',progress:{percent:10}},false,mergeLogPayload,lateFastRequest);
      window.fetch = originalRefreshFetch;

      // Production mergeLogPayload receives independently overlapping streams.
      const overlapGeneration=acknowledgeRun('browser-overlap'); runState.acceptLog({run_id:'browser-overlap',stdout_log:'out',stdout_size:5,stdout_tail:'base\n',stderr_log:'err',stderr_size:5,stderr_tail:'base\n'},false,mergeLogPayload);
      const overlapA=runState.captureLogRequest(),overlapB=runState.captureLogRequest();
      runState.acceptLog({run_id:'browser-overlap',stdout_log:'out',stdout_size:5,stdout_append:'',stderr_log:'err',stderr_size:11,stderr_append:'err-1\n'},true,mergeLogPayload,overlapA);
      runState.acceptLog({run_id:'browser-overlap',stdout_log:'out',stdout_size:11,stdout_append:'out-1\n',stderr_log:'err',stderr_size:11,stderr_append:'err-1\n'},true,mergeLogPayload,overlapB);
      const overlapLog={stdout:state.finderLog.stdout_tail,stderr:state.finderLog.stderr_tail,stderrSize:state.finderLog.stderr_size,generation:overlapGeneration};
      acknowledgeRun('browser-utf'); runState.acceptLog({run_id:'browser-utf',stdout_log:'out',stdout_size:0,stdout_tail:'',stderr_log:'err',stderr_size:0,stderr_tail:''},false,mergeLogPayload);
      const utfA=runState.captureLogRequest(),utfB=runState.captureLogRequest();
      runState.acceptLog({run_id:'browser-utf',stdout_log:'out',stdout_size:1,stdout_append:'�',stderr_log:'err',stderr_size:0},true,mergeLogPayload,utfA);
      runState.acceptLog({run_id:'browser-utf',stdout_log:'out',stdout_size:3,stdout_append:'€',stderr_log:'err',stderr_size:0},true,mergeLogPayload,utfB);
      const utfTail=state.finderLog.stdout_tail;

      // Actual refreshLog transport/merge/render path: larger same-offset
      // snapshots must retain the established last 200 lines for both streams,
      // not rebuild an unbounded tail after the first trimmed reply.
      const byteSize=value=>new TextEncoder().encode(value).length;
      const lineCount=value=>String(value).split('\n').length;
      const baseTail=Array.from({length:200},(_unused,index)=>`base-${index}`).join('\n');
      acknowledgeRun('bounded-tail'); runState.acceptLog({run_id:'bounded-tail',status:'running',stdout_log:'out',stderr_log:'err',stdout_size:byteSize(baseTail),stderr_size:byteSize(baseTail),stdout_tail:baseTail,stderr_tail:baseTail},false,mergeLogPayload);
      const boundedReplies=[],boundedUrls=[],boundedFetch=window.fetch;
      window.fetch=(url,init)=>String(url).includes('/runs/latest-log')?new Promise(resolve=>{boundedUrls.push(String(url));boundedReplies.push(resolve);}):normal(url,init);
      const boundedRounds=[];
      for(let round=0;round<100;round+=1){
        const stdoutSize=state.finderLog.stdout_size,stderrSize=state.finderLog.stderr_size;
        const smaller='\n'+Array.from({length:5},(_unused,index)=>`round-${round}-${index}`).join('\n');
        const larger='\n'+Array.from({length:10},(_unused,index)=>`round-${round}-${index}`).join('\n');
        const firstBounded=refreshLog(true),secondBounded=refreshLog(true); await new Promise(resolve=>setTimeout(resolve,0));
        const reply=append=>new Response(JSON.stringify({run_id:'bounded-tail',status:'running',stdout_log:'out',stderr_log:'err',stdout_size:stdoutSize+byteSize(append),stderr_size:stderrSize+byteSize(append),stdout_append:append,stderr_append:append}),{headers:{'Content-Type':'application/json'}});
        boundedReplies.shift()(reply(smaller)); await firstBounded;
        const firstLines={stdout:lineCount(state.finderLog.stdout_tail),stderr:lineCount(state.finderLog.stderr_tail)};
        boundedReplies.shift()(reply(larger)); await secondBounded;
        boundedRounds.push({sameOffset:boundedUrls.at(-2)===boundedUrls.at(-1),firstLines,secondLines:{stdout:lineCount(state.finderLog.stdout_tail),stderr:lineCount(state.finderLog.stderr_tail)}});
      }
      const boundedTail={first:boundedRounds[0],last:boundedRounds.at(-1),allSameOffset:boundedRounds.every(row=>row.sameOffset),allFirstBounded:boundedRounds.every(row=>row.firstLines.stdout===200&&row.firstLines.stderr===200),stdoutLines:lineCount(state.finderLog.stdout_tail),stderrLines:lineCount(state.finderLog.stderr_tail),stdoutSize:state.finderLog.stdout_size,stderrSize:state.finderLog.stderr_size,firstLine:state.finderLog.stdout_tail.split('\n')[0],lastLine:state.finderLog.stdout_tail.split('\n').at(-1),displayed:document.getElementById('finder-log').textContent};
      window.fetch=boundedFetch;

      runState.rejectAccepted(); runState.mergeStatus({current_run:null,zapret2:{ready:true}});
      document.getElementById('finder-domains').value = 'old-click.example';
      let rejectStart, startEntered; const startGate = new Promise(resolve => startEntered = resolve), starts = [];
      window.fetch = (url, init) => {
        if (String(url) === settingsUrl) return new Promise((_resolve, reject) => { rejectStart = reject; startEntered(); });
        if (String(url) === apiEndpoint('core','startStrategyDiscoveryRun')) { starts.push({token:init.headers.Authorization, body:JSON.parse(init.body)}); return Promise.resolve(new Response(JSON.stringify({run_id:'wrong'}), {headers:{'Content-Type':'application/json'}})); }
        return normal(url, init);
      };
      const oldStart = startSelectedDiscovery(); await startGate; await replace('fourth'); rejectStart(new DOMException('old start abort', 'AbortError')); await oldStart;
      return {staleCatchMessage, lateSettings, staleDownloadMessage, logUrls, logTail, terminalFlow, statusBeforeRuns, retiredStatus, panelWhileLogHeld, finalAfterRefresh, delayedRunningAccepted, overlapLog, utfTail, boundedTail, starts, token:authToken(), accepted:runState.accepted()};
    });
    assert(!details.staleCatchMessage.includes('Ошибка сохранения'), 'old settings catch wrote the replacement session message');
    assert(!details.lateSettings.settings.old_session_sentinel && details.lateSettings.settings.curl_max_time !== 99, 'old settings body wrote replacement settings');
    assert(!details.staleDownloadMessage.includes('Archive download failed'), 'old download catch wrote the replacement session message');
    assert(details.logUrls.length === 2 && details.logUrls[0] === details.logUrls[1] && details.logTail === 'base\ndelta\n', 'same-offset log requests did not converge once');
    assert(details.terminalFlow.terminalLog && details.terminalFlow.externalLog && details.terminalFlow.busy && details.terminalFlow.current === 'external' && details.terminalFlow.displayed === 'EXTERNAL\n', 'terminal reconciliation blocked final logs or a history-evidenced external run');
    assert(details.statusBeforeRuns.busy && details.statusBeforeRuns.current === 'sse-external' && details.retiredStatus.busy && details.retiredStatus.current === 'sse-external', 'SSE status-before-runs or retired-status reconciliation lost external current');
    assert(details.panelWhileLogHeld.tab === 'terminal' && details.panelWhileLogHeld.busy && details.panelWhileLogHeld.current === 'panel-external' && details.panelWhileLogHeld.label === 'Идет подбор' && details.panelWhileLogHeld.text.includes('Идет подбор') && !details.panelWhileLogHeld.stopDisabled, 'history-promoted current did not render the live terminal panel before held next-log completion');
    assert(details.finalAfterRefresh === 'base\nFINAL\n' && !details.delayedRunningAccepted, 'acknowledged terminal refresh lost final log or accepted delayed running output');
    assert(details.overlapLog.stdout === 'base\nout-1\n' && details.overlapLog.stderr === 'base\nerr-1\n' && details.overlapLog.stderrSize === 11 && details.utfTail === '€', 'independent overlapping streams duplicated/regressed or corrupted UTF-8');
    assert(details.boundedTail.first.secondLines.stdout === 200 && details.boundedTail.first.secondLines.stderr === 200 && details.boundedTail.last.secondLines.stdout === 200 && details.boundedTail.last.secondLines.stderr === 200 && details.boundedTail.allSameOffset && details.boundedTail.allFirstBounded && details.boundedTail.firstLine !== 'base-0' && details.boundedTail.lastLine === 'round-99-9' && !details.boundedTail.displayed.includes('base-0\n') && details.boundedTail.stdoutSize > 1200 && details.boundedTail.stderrSize > 1200, 'production overlapping snapshots bypassed the per-stream 200-line tail limit');
    assert(details.starts.length === 0 && details.token === 'fourth' && !details.accepted, 'old Start used the replacement token/session');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
'''
        with tempfile.TemporaryDirectory() as temporary_directory:
            html_path = Path(temporary_directory) / "ui.html"
            html_path.write_text(index_html(), encoding="utf-8")
            completed = subprocess.run(
                [str(NODE), "-e", harness, str(html_path), str(runtime)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=os.environ | {"PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")},
                timeout=45,
            )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
