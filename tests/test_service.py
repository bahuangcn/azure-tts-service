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
