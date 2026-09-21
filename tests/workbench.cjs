// Run with NODE_PATH pointing to a jsdom install; no browser or provider charges.
const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const voice = (id, language, gender, source='system') => ({id,name:id,facets:{language:[language],gender:[gender],role_type:[gender==='Female'?'青年女声':'青年男声'],source:[source],style:['温柔']},official:{voice_id:id}});
const azure = [voice('zh-CN-XiaochenNeural','zh-CN','Female'),voice('中文男声','zh-CN','Male'),voice('English','en-US','Female'),voice('Japanese','ja-JP','Female')];
const minimax = [voice('设计女声','zh-CN','Female','designed'),voice('官方男声','zh-CN','Male')];
const calls = [];
let preferences={preset:null};
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
   if(url.includes('/voices')){
    const voices=url.includes('minimax')?minimax:azure,facets={};
    voices.forEach(v=>Object.entries(v.facets).forEach(([k,values])=>facets[k]=[...new Set([...(facets[k]||[]),...values])]));
    body={voices,facets};
   }
   return {ok:true,json:async()=>body};
  };
  window.URL.revokeObjectURL=()=>{};
 }
});
const w=dom.window,d=w.document,$=id=>d.getElementById(id);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await settle();
 assert.equal($('filter-gender'),null);assert.equal($('filter-age'),null);assert.equal($('filter-role'),null);
 assert.equal($('token'),null,'No key prompt');
 assert.match($('connection').textContent,/Azure Speech · 已启用 2 种语言/);
 assert.equal(d.querySelectorAll('select[data-key]').length,4);
 assert.equal(d.querySelectorAll('.voice-option').length,3);
 $('filter-language').value='zh';$('filter-language').dispatchEvent(new w.Event('change'));
 $('filter-role_type').value='青年女声';$('filter-role_type').dispatchEvent(new w.Event('change'));
 assert.equal(d.querySelectorAll('.voice-option').length,1,'AND filters intersect');
 assert.match($('count').textContent,/1 \/ 3/);
 $('clearFilters').click();assert.equal(d.querySelectorAll('.voice-option').length,3);
 assert.equal($('speed').type,'range');assert.equal($('pitch').type,'range');
 $('speed').value='1.4';$('speed').dispatchEvent(new w.Event('input'));assert.match($('speedValue').textContent,/1.4×/);
 d.querySelector('[data-pitch="high"]').click();assert.equal($('pitch').value,'20');
 $('text').value='你好。';$('submit').click();await settle();
 let payload=JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body);
 assert.equal(payload.rate,'40%');assert.equal(payload.pitch,'20Hz');assert.equal(payload.minimax_pitch,0);
 $('provider').value='minimax';$('provider').dispatchEvent(new w.Event('change'));await settle();
 assert.match($('connection').textContent,/MiniMax · 已启用 1 种语言/);
 assert.deepEqual([...$('filter-role_type').options].map(o=>o.value),['','青年']);
 assert.deepEqual([...$('filter-language').options].map(o=>o.value),['','zh']);
 assert.equal($('languageSettings').getAttribute('href'),'languages.html?provider=minimax');
 assert.equal($('pitch').max,'12');assert.equal($('pitch').value,'0');
 $('filter-source').value='designed';$('filter-source').dispatchEvent(new w.Event('change'));
 assert.equal(d.querySelectorAll('.voice-option').length,1);
 d.querySelector('[data-pitch="high"]').click();assert.equal($('pitch').value,'3');
 $('submit').click();await settle();
 payload=JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body);
 assert.equal(payload.minimax_pitch,3);assert.equal(payload.voice,'设计女声');
 $('savePreset').click();await settle();assert.equal(preferences.preset.voice,'设计女声');$('speed').value='0.5';await $('restorePreset').onclick();assert.equal($('speed').value,'1.4');assert.equal($('voice').value,'设计女声');
 assert(calls.every(c=>c.url.startsWith('/workbench_api/')));
 assert(calls.every(c=>!c.options.headers.Authorization),'No browser token');
 const reopened=new JSDOM(readFileSync(path.join(__dirname,'../webui/index.html'),'utf8'),{url:'https://tools.exnihilo.site/tts/',runScripts:'dangerously',beforeParse(window){window.fetch=w.fetch;window.URL.revokeObjectURL=()=>{}}});
 await settle();
 assert.equal(reopened.window.document.getElementById('provider').value,'minimax');
 assert.equal(reopened.window.document.getElementById('voice').value,'设计女声');
 assert.equal(reopened.window.document.getElementById('speed').value,'1.4');
 assert.equal(reopened.window.document.getElementById('pitch').value,'3');
 reopened.window.close();
 console.log('Workbench checks passed: automatic loading, intersecting filters, reset, sliders, presets, provider parameters, no browser secrets.');
 dom.window.close();
})().catch(e=>{dom.window.close();console.error(e);process.exitCode=1});
