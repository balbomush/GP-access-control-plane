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


class R2UiTests(unittest.TestCase):
    def test_subject_lifetimes_settings_draft_and_latest_preset_in_rendered_browser(self):
        runtime = NODE.parents[1]
        harness = r'''
const fs=require('fs'),path=require('path');const {chromium}=require(path.join(process.argv[2],'playwright-project/node_modules/playwright'));
const assert=(condition,message)=>{if(!condition)throw Error(message)};
(async()=>{const browser=await chromium.launch({headless:true});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
try{
 await page.route('http://r2.local/**',r=>r.fulfill({status:200,contentType:'text/html',body:fs.readFileSync(process.argv[1],'utf8')}));
 await page.addInitScript(()=>{window.fetch=async(url,init={})=>{
  if(String(url).includes('/events/stream'))return new Response(new ReadableStream({start(c){init.signal?.addEventListener('abort',()=>c.close(),{once:true})}}));
  const data=String(url).includes('/status')?{current_run:null,zapret2:{ready:true},settings:{curl_parallelism_max:10}}:{};
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
 }});await page.goto('http://r2.local/');
 await page.evaluate(async()=>{
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  localStorage.setItem(AUTH_TOKEN_KEY,'r2');await startAuthenticatedUi();
  const input=el('settings-curl-max');input.value='17';input.dispatchEvent(new Event('input',{bubbles:true}));
  mergeStatusPayload({current_run:null,zapret2:{ready:true},settings:{curl_parallelism_max:23}});
  check(input.value==='17','background status overwrote unsaved settings draft');
  const normal=window.fetch;window.fetch=async(url,init)=>String(url)===apiEndpoint('core','saveRunSettings')?new Response(JSON.stringify({curl_parallelism_max:17}),{headers:{'Content-Type':'application/json'}}):normal(url,init);
  check(await saveSettings(),'draft save failed');check(state.settings.curl_parallelism_max===17,'draft payload was not saved');
  mergeStatusPayload({current_run:null,zapret2:{ready:true},settings:{curl_parallelism_max:24}});check(input.value==='24','saved draft prevented future snapshot refresh');
  const select=el('finder-preset-select');select.innerHTML='<option value="custom:old">old</option><option value="custom:new">new</option>';
  state.customPresets={finder:{},common:{}};state.customPresetMeta={finder:{old:{total_count:1,enabled_count:1},new:{total_count:1,enabled_count:1}},common:{}};
  const held=[];window.fetch=(url,init)=>String(url).includes('/presets/domains')?new Promise(resolve=>held.push({url:String(url),resolve})):normal(url,init);
  select.value='custom:old';const old=usePreset('finder');await Promise.race([(async()=>{while(held.length<1)await new Promise(r=>setTimeout(r,0))})(),new Promise((_,reject)=>setTimeout(()=>reject(Error('first preset request absent')),3000))]);
  select.value='custom:new';const current=usePreset('finder');await Promise.race([(async()=>{while(held.length<2)await new Promise(r=>setTimeout(r,0))})(),new Promise((_,reject)=>setTimeout(()=>reject(Error('second preset request absent')),3000))]);
  const reply=domain=>new Response(JSON.stringify({domains:[{domain}],has_more:false}),{headers:{'Content-Type':'application/json'}});
  held[1].resolve(reply('discord.com'));await current;held[0].resolve(reply('youtube.com'));await old;
  check(el('finder-domains').value==='discord.com','late prior preset overwrote current selection');
  const life=new UiLifetime();const signal=life.requestOptions().signal;let ticks=0,cleanups=0;
  life.setTimeout(()=>ticks++,0);life.requestAnimationFrame(()=>ticks++);const release=life.ownCleanup(()=>cleanups++);
  const token=life.capture('choice');life.capture('choice');try{await life.result(Promise.resolve('old'),token);check(false,'stale choice was accepted')}catch(error){check(error.staleView,'wrong stale choice error')}
  life.dispose();release();await new Promise(r=>setTimeout(r,25));check(signal.aborted&&ticks===0&&cleanups===1,'view dispose leaked a callback, request or cleanup');
  const rows=[{id:'one',protocol:'tls',args:'--dpi-desync=split2',seen:[{domain:'youtube.com'}],common_seen:[{domains:['youtube.com','discord.com']}]}];const before=JSON.stringify(rows);
  const model=createCandidateSelectionModel({uniqueDomains:values=>[...new Set(values)]});
  for(const mode of ['coverage','minimal','balance']){const targets={required:['youtube.com'],desired:['discord.com']};const a=model.buildCandidateResult(mode,targets,rows),b=model.buildCandidateResult(mode,targets,rows);check(JSON.stringify(a)===JSON.stringify(b),'pure candidate selection is nondeterministic');check(a.strategy_set.length===1,'candidate selection lost the loaded strategy')}
  check(JSON.stringify(rows)===before,'pure candidate selection mutated query rows');
  disposeApplication();
 });assert(errors.length===0,'rendered UI errors: '+errors.join(';'));
}finally{await browser.close()}})().catch(e=>{console.error(e.stack);process.exitCode=1});
'''
        with tempfile.TemporaryDirectory() as raw:
            html = Path(raw) / "ui.html"
            html.write_text(index_html(), encoding="utf-8")
            result = subprocess.run([str(NODE), "-e", harness, str(html), str(runtime)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45, env=os.environ | {"PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")})
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_confirmed_draft_disposal_raw_response_and_real_click_regressions(self):
        runtime = NODE.parents[1]
        with tempfile.TemporaryDirectory() as raw:
            html = Path(raw) / "ui.html"
            html.write_text(index_html(), encoding="utf-8")
            result = subprocess.run([str(NODE), str(ROOT / "tests/browser/r2_lifetime.cjs"), str(html), str(runtime)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45, env=os.environ | {"PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")})
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)
            import json
            receipt = json.loads(result.stdout)
            self.assertTrue(receipt["pass"])
            self.assertEqual(10, len(receipt["scenarios"]))
