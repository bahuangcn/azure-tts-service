const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const calls=[];let generation=0,hold=false,release;
const dom=new JSDOM(fs.readFileSync(path.join(__dirname,'../webui/index.html'),'utf8'),{url:'https://tools.exnihilo.site/tts/',runScripts:'dangerously',beforeParse(w){
 w.HTMLMediaElement.prototype.pause=()=>{};w.HTMLMediaElement.prototype.play=async()=>{};w.URL.revokeObjectURL=()=>{};w.URL.createObjectURL=()=> 'blob:planned';
 w.fetch=async(url,options={})=>{calls.push({url,options});let data=[];
 if(url.includes('/preferences'))data={languages:['zh'],presets:[],preset:null};
 if(url.includes('/voices?'))data={voices:[{id:'voice',provider:'azure',name:'故事声音',primary_languages:['zh-CN'],facets:{language:['zh-CN'],style:['gentle']},official:{}}]};
 if(url.endsWith('/reading-plans')){const input=JSON.parse(options.body);if(hold)await new Promise(r=>release=r);data={id:'plan-'+(++generation),summary:'温柔舒缓',capability_note:'按平台执行',warnings:[],provider:'azure',blocks:[{text:input.text,speed:0.85,pitch:0,emotion:'gentle',explanation:'保持自然',pauses:[],emphasis:[]}]};}
 if(url.endsWith('/preview'))data={task_id:'preview',status:'completed'};
 if(url.endsWith('/tts/preview'))data={task_id:'preview',status:'completed',audio_url:'/azure_api/tts/audio/preview'};
 return {ok:true,json:async()=>data,blob:async()=>new w.Blob(['audio'])};
 };
}});
const w=dom.window,d=w.document,$=id=>d.getElementById(id),settle=()=>new Promise(r=>setImmediate(r));
(async()=>{await settle();$('text').value='很久以前，小兔子看着月亮。';await $('generatePlan').onclick();
 assert.equal($('planResult').hidden,false);assert.equal($('applyPlan').checked,true);assert.match($('planDetails').textContent,/0.85×/);
 assert.equal($('text').value,'很久以前，小兔子看着月亮。');
 await $('submit').onclick();assert.equal(JSON.parse(calls.filter(c=>c.url.endsWith('/tts')&&c.options.method==='POST').at(-1).options.body).arrangement_id,'plan-1');
 await $('previewPlan').onclick();assert.equal($('planAudio').src,'blob:planned');$('closePlanPlayer').click();assert.equal($('planPlayer').hidden,true);
 $('applyPlan').checked=false;await $('submit').onclick();assert.equal(JSON.parse(calls.filter(c=>c.url.endsWith('/tts')&&c.options.method==='POST').at(-1).options.body).arrangement_id,null);
 $('speed').value='0.8';$('speed').dispatchEvent(new w.Event('input'));assert.equal($('planResult').hidden,true);assert.equal($('applyPlan').checked,false);
 hold=true;const generating=$('generatePlan').onclick();await settle();$('text').value='更新后的故事。';$('text').dispatchEvent(new w.Event('input'));release();await generating;
 assert.equal($('planResult').hidden,true);assert.match($('planStatus').textContent,/输入已变化/);
 console.log('AI arrangement UI: generation, original text, explicit application, preview, stale-input protection.');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>w.close());
