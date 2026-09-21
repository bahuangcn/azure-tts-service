// Run with NODE_PATH pointing to a jsdom install; no browser or provider charges.
const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const voice = (id, language, gender, source='system') => ({id,name:id,facets:{language:[language],gender:[gender],role_type:[gender==='Female'?'青年女声':'青年男声'],source:[source],style:['温柔']},official:{voice_id:id}});
const azure = [voice('zh-CN-XiaochenNeural','zh-CN','Female'),voice('中文男声','zh-CN','Male'),voice('English','en-US','Female'),voice('Japanese','ja-JP','Female')];
const minimax = [voice('设计女声','zh-CN','Female','designed'),voice('官方男声','zh-CN','Male')];
azure[0].facets.style=['friendly','angry','sad','cheerful'];
azure[1].facets.style=[];
azure.forEach(v=>v.provider='azure');
minimax.forEach(v=>v.provider='minimax');
azure.push({...voice('British','en-GB','Female'),provider:'azure'}, {...voice('Australian','en-AU','Female'),provider:'azure'});
// Even an incorrectly mixed response must not leak another platform's options.
azure.push({...voice('MiniMax-only','en-CA','Male'),provider:'minimax'});
const calls = [];
let preferences={preset:null,presets:[],default_preset_id:null};
let nextPreset=0;
let pendingPreview=null,holdPreview=false;
const languages={azure:['zh','en'],minimax:['zh']};
azure.push({...voice('German multilingual','de-DE','Female'),primary_languages:['de-DE'],facets:{language:['de-DE','en-US','zh-CN'],role_type:['老年男声']}});
minimax.forEach(v=>v.facets.role_type=['青年']);
const dom = new JSDOM(readFileSync(path.join(__dirname,'../webui/index.html'),'utf8'),{
 url:'https://tools.exnihilo.site/tts/',runScripts:'dangerously',
 beforeParse(window){
  window.fetch=async(url,options)=>{
   calls.push({url,options});
   let body=[];
   if(url.includes('/preferences')){if(options?.method==='PUT') preferences={...preferences,...JSON.parse(options.body)};body={...preferences,languages:languages[new URL(url,'https://test').searchParams.get('provider')||'azure']};}
   if(url.includes('/presets')) {
    const id=url.split('/')[3];
    if(options.method==='POST' && !id) {
      const preset={id:String(++nextPreset),...JSON.parse(options.body)};
      preferences.presets.push(preset);preferences.preset=preset;preferences.default_preset_id=preset.id;
    } else if(options.method==='PUT') {
      const preset=preferences.presets.find(p=>p.id===id);Object.assign(preset,JSON.parse(options.body));preferences.preset=preset;preferences.default_preset_id=id;
    } else if(options.method==='DELETE') {
      preferences.presets=preferences.presets.filter(p=>p.id!==id);
    } else if(url.endsWith('/load')) {
      preferences.preset=preferences.presets.find(p=>p.id===id);preferences.default_preset_id=id;
    }
    body={...preferences,languages:languages[preferences.preset.provider]};
   }
   if(url.includes('/voices?')){
    const voices=url.includes('minimax')?minimax:azure,facets={};
    voices.forEach(v=>Object.entries(v.facets).forEach(([k,values])=>facets[k]=[...new Set([...(facets[k]||[]),...values])]));
    body={voices,facets};
   }
   if(url.endsWith('/voices/preview')){if(holdPreview)await new Promise(resolve=>pendingPreview=resolve);body={task_id:'sample',status:'completed'};}
   if(url.endsWith('/tts/sample'))body={task_id:'sample',status:'completed',audio_url:'/azure_api/tts/audio/sample'};
   return {ok:true,json:async()=>body,blob:async()=>new window.Blob(['audio'])};
  };
  window.URL.revokeObjectURL=()=>{};
  window.URL.createObjectURL=()=> 'blob:sample';
  Object.defineProperty(window.HTMLMediaElement.prototype,'paused',{get(){return this._paused!==false;}});
  window.HTMLMediaElement.prototype.pause=function(){this._paused=true;this.dispatchEvent(new window.Event('pause'));};
  window.HTMLMediaElement.prototype.play=async function(){this._paused=false;this.dispatchEvent(new window.Event('play'));};
  window.HTMLElement.prototype.scrollIntoView=()=>{};
 }
});
const w=dom.window,d=w.document,$=id=>d.getElementById(id);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await settle();
 assert.equal($('filter-gender'),null);assert.equal($('filter-age'),null);assert.equal($('filter-role'),null);
 assert.equal($('token'),null,'No key prompt');
 const firstCard=d.querySelector('.voice-option');
 assert.deepEqual([...firstCard.querySelectorAll('.style-tag')].map(t=>t.textContent),['友好','生气','悲伤','欢快']);
 assert.match(d.querySelectorAll('.voice-option')[1].textContent,/风格：官方未标注/);
 for (const style of ['angry','sad']) {
  $('filter-style').value=style;$('filter-style').dispatchEvent(new w.Event('change'));
  assert.equal(d.querySelectorAll('.voice-option').length,1);
  assert.equal(d.querySelectorAll('.voice-option .style-tag').length,4,'Show all styles even when filtered');
  assert.equal(d.querySelector('.style-tag.matched').textContent,style==='angry'?'生气':'悲伤');
 }
 $('clearFilters').click();
 assert.match($('connection').textContent,/Azure Speech · 已启用 2 种语言/);
 assert.equal(d.querySelectorAll('select[data-key]').length,4);
 assert.equal(d.querySelectorAll('.voice-option').length,5);
 assert.deepEqual([...$('filter-language').options].map(o=>o.value),['','zh-CN','en-US','en-GB','en-AU']);
 for (const [locale,id] of [['en-US','English'],['en-GB','British'],['en-AU','Australian']]) {
  $('filter-language').value=locale;$('filter-language').dispatchEvent(new w.Event('change'));
  assert.equal(d.querySelectorAll('.voice-option').length,1);assert.match(d.querySelector('.voice-option').textContent,new RegExp(id));
 }
 $('filter-language').value='zh-CN';$('filter-language').dispatchEvent(new w.Event('change'));
 $('filter-role_type').value='青年女声';$('filter-role_type').dispatchEvent(new w.Event('change'));
 assert.equal(d.querySelectorAll('.voice-option').length,1,'AND filters intersect');
 assert.match($('count').textContent,/1 \/ 5/);
 $('clearFilters').click();assert.equal(d.querySelectorAll('.voice-option').length,5);
 assert.equal($('speed').type,'range');assert.equal($('pitch').type,'range');
 $('speed').value='1.4';$('speed').dispatchEvent(new w.Event('input'));assert.match($('speedValue').textContent,/1.4×/);
 d.querySelector('[data-pitch="high"]').click();assert.equal($('pitch').value,'20');
 $('text').value='你好。';$('submit').click();await settle();
 let payload=JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body);
 assert.equal(payload.rate,'40%');assert.equal(payload.pitch,'20Hz');assert.equal(payload.minimax_pitch,0);
 $('provider').value='minimax';$('provider').dispatchEvent(new w.Event('change'));await settle();
 assert.match($('connection').textContent,/MiniMax · 已启用 1 种语言/);
 assert.deepEqual([...$('filter-role_type').options].map(o=>o.value),['','青年']);
 assert.deepEqual([...$('filter-language').options].map(o=>o.value),['','zh-CN']);
 assert.equal($('languageSettings').getAttribute('href'),'languages.html?provider=minimax');
 assert.equal($('pitch').max,'12');assert.equal($('pitch').value,'0');
 $('filter-source').value='designed';$('filter-source').dispatchEvent(new w.Event('change'));
 assert.equal(d.querySelectorAll('.voice-option').length,1);
 d.querySelector('[data-pitch="high"]').click();assert.equal($('pitch').value,'3');
 $('submit').click();await settle();
 payload=JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body);
 assert.equal(payload.minimax_pitch,3);assert.equal(payload.voice,'设计女声');
 $('presetName').value='中文故事';$('savePreset').click();await settle();assert.equal(preferences.preset.voice,'设计女声');
 const firstPreset=preferences.default_preset_id;
 $('presetName').value='慢速故事';$('speed').value='0.8';await $('savePreset').onclick();
 assert.equal(preferences.presets.length,2);assert.equal($('presetSelect').options.length,3);
 $('presetSelect').value=firstPreset;$('presetSelect').dispatchEvent(new w.Event('change'));
 await $('restorePreset').onclick();assert.equal($('speed').value,'1.4');
 $('presetName').value='中文睡前故事';await $('updatePreset').onclick();assert.equal(preferences.presets[0].name,'中文睡前故事');
 d.querySelector('.preview-button').click();await settle();
 assert.equal($('previewAudio').src,'blob:sample');assert.equal($('previewAudio').hidden,false);
 const preview=JSON.parse(calls.find(c=>c.url.endsWith('/voices/preview')).options.body);
 assert.equal(preview.provider,'minimax');assert.equal(preview.voice,'设计女声');assert.equal(preview.pitch,3);assert.equal(preview.speed,1.4);
 const playingAudio=$('previewAudio'), playingPanel=$('previewPanel');
 assert.equal(playingPanel.closest('.voice-row'),d.querySelector('.preview-button').closest('.voice-row'));
 assert.equal(playingAudio.paused,false);
 d.querySelector('.preview-button').click();await settle();assert.equal(playingAudio.paused,true);assert.equal($('previewPanel'),playingPanel);assert.match($('previewStatus').textContent,/已暂停/);
 const generated=calls.filter(c=>c.url.endsWith('/voices/preview')).length;
 d.querySelector('.preview-button').click();await settle();assert.equal(playingAudio.paused,false);
 assert.equal(calls.filter(c=>c.url.endsWith('/voices/preview')).length,generated,'Resume does not regenerate audio');
 d.querySelector('.voice-option').click();assert.equal($('previewAudio'),playingAudio,'Selecting voice preserves player node');
 $('showMore').click();assert.equal($('previewAudio'),playingAudio,'Expanding list preserves player node');
 $('stopPreview').click();assert.equal($('previewPanel'),null);assert.equal(playingAudio.paused,true);
 assert.equal(d.querySelector('.preview-button').getAttribute('aria-expanded'),'false');
 holdPreview=true;d.querySelector('.preview-button').click();await settle();assert($('previewPanel'));
 $('stopPreview').click();pendingPreview();await settle();assert.equal($('previewPanel'),null,'Late response cannot reopen closed player');holdPreview=false;
 $('clearFilters').click();const previewButtons=d.querySelectorAll('.preview-button');
 previewButtons[0].click();await settle();const firstAudio=$('previewAudio');
 previewButtons[1].click();await settle();assert.equal(d.querySelectorAll('.preview-panel').length,1);assert.equal(firstAudio.paused,true);
 assert.equal($('previewPanel').closest('.voice-row'),previewButtons[1].closest('.voice-row'));
 $('filter-source').value='designed';$('filter-source').dispatchEvent(new w.Event('change'));assert.equal($('previewPanel'),null,'Hidden voice cannot keep playing');


$('speed').value='0.5';await $('restorePreset').onclick();assert.equal($('speed').value,'1.4');assert.equal($('voice').value,'设计女声');
 assert(calls.every(c=>c.url.startsWith('/workbench_api/')));
 assert(calls.every(c=>!c.options.headers.Authorization),'No browser token');
 const reopened=new JSDOM(readFileSync(path.join(__dirname,'../webui/index.html'),'utf8'),{url:'https://tools.exnihilo.site/tts/',runScripts:'dangerously',beforeParse(window){window.fetch=w.fetch;window.URL.revokeObjectURL=()=>{};window.HTMLMediaElement.prototype.pause=()=>{}}});
 await settle();
 assert.equal(reopened.window.document.getElementById('provider').value,'minimax');
 assert.equal(reopened.window.document.getElementById('voice').value,'设计女声');
 assert.equal(reopened.window.document.getElementById('speed').value,'1.4');
 assert.equal(reopened.window.document.getElementById('pitch').value,'3');
 reopened.window.close();
 console.log('Workbench checks passed: automatic loading, intersecting filters, reset, sliders, presets, provider parameters, no browser secrets.');
 dom.window.close();
})().catch(e=>{dom.window.close();console.error(e);process.exitCode=1});
