import os
import tempfile
from pathlib import Path
os.environ.update(TTS_AUDIO_DIR=tempfile.mkdtemp(), TTS_DB_PATH=tempfile.mktemp(),
 TTS_API_KEYS='{"alice":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","bob":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}', AZURE_SPEECH_KEY='test')
from fastapi import FastAPI
from fastapi.testclient import TestClient
from database import init_db, get_db
from routes import router
from synthesis import sentences, subtitles
from providers import normalize
init_db()
app=FastAPI();app.include_router(router,prefix='/azure_api')
client=TestClient(app)
a={'Authorization':'Bearer '+'a'*32};b={'Authorization':'Bearer '+'b'*32}

def test_protection_and_ownership():
    assert client.post('/azure_api/tts',json={'text':'你好。'}).status_code==401
    result=client.post('/azure_api/tts',headers=a,json={'text':'你好。'})
    assert result.status_code==202
    tid=result.json()['task_id']
    for path in [f'/tts/{tid}',f'/tts/audio/{tid}',f'/tts/{tid}/timing',f'/tts/{tid}/subtitles.srt']:
        assert client.get('/azure_api'+path,headers=b).status_code==404
        assert client.get('/azure_api'+path).status_code==401
    assert client.delete('/azure_api/tts/'+tid,headers=b).status_code==404
    assert client.delete('/azure_api/tts/'+tid,headers=a).status_code==409
    assert client.get('/azure_api/tts',headers=b).json()==[]

def test_validation():
    for body in [{'text':' '},{'text':'x'*3001},{'text':'Hi','sentences':['different']},{'text':'Hi','speed':3},{'text':'Hi','rate':'\"/><audio src=\"evil'}]:
        assert client.post('/azure_api/tts',headers=a,json=body).status_code==422

def test_catalog_filters(monkeypatch):
    import routes
    voices=[normalize('azure',{'ShortName':'a','Locale':'en-US','Gender':'Female','StyleList':['chat']}),normalize('azure',{'ShortName':'b','Locale':'en-US','Gender':'Male'})]
    monkeypatch.setattr(routes,'voice_catalog',lambda provider:voices)
    d=client.get('/azure_api/voices?language=en-US&gender=Female&style=chat',headers=a).json()
    assert [v['id'] for v in d['voices']]==['a']
    assert client.get('/azure_api/voices?unknown=yes',headers=a).status_code==422
    raw={'voice_id':'custom','description':['温柔','女','中文'],'tags':{'age':['young']}}
    n=normalize('minimax',raw,'designed')
    assert n['official']==raw and n['facets']['age']==['young']

def test_subtitles():
    assert sentences('你好！世界。Hello. Bye!')==['你好！','世界。','Hello.','Bye!']
    t=[{'text':'你好','start_ms':1234,'end_ms':2345}]
    assert '00:00:01,234 --> 00:00:02,345' in subtitles(t,'srt')
    assert subtitles(t,'vtt').startswith('WEBVTT\n\n')

def test_measured_synthesis(monkeypatch):
    import synthesis, wave
    def fake(text,voice,rate,path,pitch):
        with wave.open(path,'wb') as f:
            f.setparams((1,2,32000,0,'NONE','not compressed'))
            f.writeframes(b'\x00\x00'*3200)
        return [{'text':text,'start_ms':0,'end_ms':100}],100
    monkeypatch.setattr('worker._synth_one',fake)
    result=client.post('/azure_api/tts',headers=a,json={'text':'你好。世界。'})
    tid=result.json()['task_id']
    with get_db() as conn: task=dict(conn.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
    synthesis.synthesize_story(task)
    d=client.get('/azure_api/tts/'+tid,headers=a).json()
    assert d['total_ms']==200
    assert [s['duration_ms'] for s in d['sentence_timings']]==[100,100]
    assert d['word_timings'][1]['start_ms']==100
    assert client.get(d['audio_url'],headers=a).status_code==200
    assert client.get(d['subtitles']['vtt'],headers=a).text.startswith('WEBVTT')

def test_minimax_error_is_safe(monkeypatch):
    import providers
    monkeypatch.setattr(providers,'MINIMAX_API_KEY','private-provider-key')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'base_resp':{'status_code':2049,'status_msg':'private-provider-key'}}
    monkeypatch.setattr(providers.requests,'post',lambda *a,**k:Response())
    import pytest
    with pytest.raises(RuntimeError,match='2049') as exc:
        providers.minimax_post('/v1/t2a_v2',{})
    assert 'private-provider-key' not in str(exc.value)

def test_rate_limit(monkeypatch):
    import security
    from fastapi import HTTPException
    import pytest
    monkeypatch.setattr(security,'RATE_LIMIT',1)
    security.limit_submission('rate-test-client')
    with pytest.raises(HTTPException) as exc: security.limit_submission('rate-test-client')
    assert exc.value.status_code==429

def test_minimax_measured_audio(monkeypatch):
    import synthesis,io,wave
    buf=io.BytesIO()
    with wave.open(buf,'wb') as f:
        f.setparams((1,2,32000,0,'NONE','not compressed'))
        f.writeframes(b'\x00\x00'*6400)
    seen=[]
    def fake(path,payload):
        seen.append(payload)
        return {'data':{'audio':buf.getvalue().hex()},'trace_id':'test-trace','extra_info':{'usage_characters':2}}
    monkeypatch.setattr(synthesis,'minimax_post',fake)
    result=client.post('/azure_api/tts',headers=a,json={'text':'你好。世界。'})
    tid=result.json()['task_id']
    with get_db() as conn:
        conn.execute("UPDATE tasks SET provider='minimax' WHERE task_id=?",(tid,));conn.commit()
        task=dict(conn.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
    synthesis.synthesize_story(task)
    d=client.get('/azure_api/tts/'+tid,headers=a).json()
    assert d['total_ms']==400 and len(seen)==2
    assert d['word_timings']==[]
    assert d['metadata']['provider_segments'][0]['trace_id']=='test-trace'

def test_unified_role_categories():
    azure=normalize('azure',{'ShortName':'a','Gender':'Female','RolePlayList':['YoungAdultFemale','SeniorFemale']})
    mini=normalize('minimax',{'voice_id':'b','gender':'Female','description':['一位青年女性声音，标准普通话。']})
    assert '青年女声' in azure['facets']['role_type']
    assert '老年女声' in azure['facets']['role_type']
    assert mini['facets']['role_type']==['青年']
    assert normalize('minimax',{'voice_id':'unknown'})['facets']['role_type']==['未标注']

def test_preferences_persistence_and_isolation():
    assert client.get('/azure_api/preferences').status_code==401
    assert client.get('/azure_api/preferences',headers=a).json()['languages']==['zh','en']
    preset={'provider':'minimax','voice':'designed','speed':0.8,'pitch':-3,'filters':{'source':'designed'}}
    assert client.put('/azure_api/preferences',headers=a,json={'preset':preset}).status_code==200
    assert client.put('/azure_api/preferences',headers=a,json={'languages':['zh','en','ja']}).json()['preset']==preset
    assert client.get('/azure_api/preferences',headers=b).json()['preset'] is None
    assert client.get('/azure_api/preferences',headers=a).json()['preset']==preset
    assert client.put('/azure_api/preferences',headers=a,json={'preset':{**preset,'pitch':20}}).status_code==422
    assert client.put('/azure_api/preferences',headers=a,json={'languages':[]}).json()['languages']==[]
    assert client.put('/azure_api/preferences',headers=a,json={'languages':['bad-invalid']}).status_code==422

def test_platform_languages_and_role_sources():
    for provider, languages in [('azure',['en']),('minimax',['zh'])]:
        response=client.put('/azure_api/preferences?provider='+provider,headers=a,json={'languages':languages})
        assert response.status_code==200
    assert client.get('/azure_api/preferences?provider=azure',headers=a).json()['languages']==['en']
    mini=client.get('/azure_api/preferences?provider=minimax',headers=a).json()
    assert mini['languages']==['zh'] and mini['preset']['voice']=='designed'
    azure=normalize('azure',{'ShortName':'a','Locale':'de-DE','SecondaryLocaleList':['zh-CN','en-US'],'Gender':'Female'})
    assert azure['primary_languages']==['de-DE']
    assert azure['facets']['role_type']==['未标注']
    assert azure['role_type_source']=='RolePlayList'
    assert normalize('minimax',{'voice_id':'b','age':'青年','role':'Narrator'})['facets']['role_type']==['青年']


def test_named_presets_crud_and_owner_isolation():
    body={'name':'睡前故事','provider':'azure','voice':'zh-CN-XiaochenNeural','speed':0.8,'pitch':-4,'filters':{'language':'zh-CN'}}
    first=client.post('/azure_api/presets',headers=a,json=body)
    assert first.status_code==201
    first_id=first.json()['default_preset_id']
    second=client.post('/azure_api/presets',headers=a,json={**body,'name':'English','voice':'en-US-JennyNeural','speed':1.2})
    second_id=second.json()['default_preset_id']
    assert first_id!=second_id
    assert len(second.json()['presets'])>=2
    loaded=client.post(f'/azure_api/presets/{first_id}/load',headers=a).json()
    assert loaded['preset']['speed']==0.8 and loaded['default_preset_id']==first_id
    assert client.post(f'/azure_api/presets/{first_id}/load',headers=b).status_code==404
    assert client.put(f'/azure_api/presets/{first_id}',headers=b,json=body).status_code==404
    assert client.delete(f'/azure_api/presets/{first_id}',headers=b).status_code==404
    updated=client.put(f'/azure_api/presets/{first_id}',headers=a,json={**body,'name':'改名','speed':0.9}).json()
    assert updated['preset']['speed']==0.9
    assert client.delete(f'/azure_api/presets/{first_id}',headers=a).status_code==200
    saved=client.get('/azure_api/preferences',headers=a).json()
    assert saved['preset'] is None and any(p['id']==second_id for p in saved['presets'])
    assert client.post('/azure_api/presets',headers=a,json={**body,'name':'  '}).status_code==422
    assert client.post('/azure_api/presets',json=body).status_code==401


def test_legacy_named_preset_migration():
    import json
    from routes import read_preferences
    old={'languages':['zh'],'preset':{'provider':'azure','voice':'old','speed':1,'pitch':0,'filters':{}}}
    with get_db() as conn:
        conn.execute('INSERT INTO preferences(owner,data) VALUES(?,?)',('migration-test',json.dumps(old)))
        conn.commit()
        migrated=read_preferences(conn,'migration-test')
    assert migrated['presets'][0]['voice']=='old'
    assert migrated['default_preset_id']=='legacy-default'
    assert migrated['provider_languages']=={'azure':['zh'],'minimax':['zh']}


def test_preview_cache_parameters_and_isolation(monkeypatch):
    import routes, json
    monkeypatch.setattr(routes,'MINIMAX_API_KEY','test')
    monkeypatch.setattr(routes,'voice_catalog',lambda provider:[normalize(provider,{'ShortName':'preview-en','Locale':'en-GB'})] if provider=='azure' else [normalize(provider,{'voice_id':'preview-zh','language':'zh-CN'})])
    for provider, voice in [('azure','preview-en'),('minimax','preview-zh')]:
        body={'provider':provider,'voice':voice,'speed':0.8,'pitch':-3}
        result=client.post('/azure_api/voices/preview',headers=a,json=body)
        assert result.status_code==202
        tid=result.json()['task_id']
        assert client.post('/azure_api/voices/preview',headers=a,json=body).json()['task_id']==tid
        assert client.post('/azure_api/voices/preview',headers=b,json=body).json()['task_id']!=tid
        assert client.get('/azure_api/tts/'+tid,headers=b).status_code==404
        assert tid not in [t['task_id'] for t in client.get('/azure_api/tts',headers=a).json()]
        with get_db() as conn:
            task=dict(conn.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
        opts=json.loads(task['options'])
        assert opts['preview'] is True and opts['speed']==0.8 and opts['pitch']=='-3Hz'
        assert opts['minimax_pitch']==(-3 if provider=='minimax' else 0)
        assert ('Hello' in task['text']) == (provider=='azure')
        assert client.post('/azure_api/voices/preview',headers=a,json={**body,'voice':'wrong-platform'}).status_code==404
        assert client.post('/azure_api/voices/preview',headers=a,json={**body,'speed':1.1}).json()['task_id']!=tid
    assert client.post('/azure_api/voices/preview',headers=a,json={'provider':'minimax','voice':'preview-zh','pitch':20}).status_code==422
    assert client.post('/azure_api/voices/preview',json={'provider':'azure','voice':'preview-en'}).status_code==401
