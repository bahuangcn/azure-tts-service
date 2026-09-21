import json
import xml.etree.ElementTree as ET
import pytest
from test_service import client, a, b, get_db
from reading_plan import build_plan, ModelPlan, azure_content, minimax_text, plan_blocks
from providers import normalize

TEXT = '很久以前，小兔子看着月亮。妈妈轻声讲着故事。'

def model_result(payload):
    return ModelPlan.model_validate({'summary':'温柔舒缓，保留原文', 'blocks':[
        {'index':i,'speed':0.85,'pitch':0,'emotion':'calm','explanation':'在开头留白',
         'pauses':[{'after':'很久以前，','duration_ms':600}], 'emphasis':[{'text':'月亮'}]}
        for i,_ in enumerate(payload['blocks'])]})


def test_plan_keeps_original_and_compiles_controls(monkeypatch):
    monkeypatch.setattr('reading_plan.ask_model',model_result)
    for provider,raw in [('azure',{'ShortName':'zh-CN-TestNeural','Locale':'zh-CN','StyleList':['calm']}),
                         ('minimax',{'voice_id':'Chinese (Mandarin)_Test'})]:
        voice=normalize(provider,raw)
        plan=build_plan(TEXT,provider,voice,'mother','温暖自然',1,0)
        assert ''.join(b['text'] for b in plan['blocks'])==TEXT
        block=plan['blocks'][0]
        assert block['speed']==0.85 and block['pauses'][0]['position']==5
        if provider=='azure':
            content=azure_content(block)
            tree=ET.fromstring('<root xmlns:mstts="https://www.w3.org/2001/mstts">'+content+'</root>')
            assert ''.join(tree.itertext())==TEXT
            assert tree.find('.//break').attrib['time']=='600ms'
            assert 'style="calm"' in content and 'rate="-15%"' in content
        else:
            rendered=minimax_text(block)
            assert '<#0.60#>' in rendered
            import re
            assert re.sub(r'<#[^>]*#>','',rendered)==TEXT
    assert ''.join(plan_blocks('第一句。第二句。'))=='第一句。第二句。'


def test_invalid_llm_controls_are_not_executed(monkeypatch):
    monkeypatch.setattr('reading_plan.ask_model',lambda payload:ModelPlan.model_validate({
        'summary':'test','blocks':[{'index':0,'speed':1,'pitch':0,'emotion':'invented-style','explanation':'',
        'pauses':[{'after':'模型新增独白','duration_ms':500}], 'emphasis':[{'text':'不存在的词'}]}]}))
    voice=normalize('azure',{'ShortName':'a','Locale':'zh-CN'})
    plan=build_plan(TEXT,'azure',voice,'mother','',1,0)
    assert plan['blocks'][0]['text']==TEXT
    assert plan['blocks'][0]['emotion']=='neutral'
    assert not plan['blocks'][0]['pauses'] and not plan['blocks'][0]['emphasis']
    assert plan['warnings']
    with pytest.raises(ValueError,match='HD'):
        build_plan(TEXT,'azure',normalize('azure',{'ShortName':'en-US-Ava:DragonHDLatestNeural'}),'mother','',1,0)


def test_plan_api_owner_and_stale_text(monkeypatch):
    import routes
    monkeypatch.setattr('reading_plan.ask_model',model_result)
    monkeypatch.setattr(routes,'voice_catalog',lambda provider:[normalize('azure',{'ShortName':'test','Locale':'zh-CN','StyleList':['calm']})])
    body={'text':TEXT,'provider':'azure','voice':'test','scene':'mother','speed':1,'pitch':0}
    assert client.post('/azure_api/reading-plans',json=body).status_code==401
    result=client.post('/azure_api/reading-plans',headers=a,json=body)
    assert result.status_code==201,result.text
    plan=result.json();pid=plan['id']
    assert client.get('/azure_api/reading-plans/'+pid,headers=b).status_code==404
    assert client.post(f'/azure_api/reading-plans/{pid}/preview',headers=b).status_code==404
    payload={'text':TEXT,'provider':'azure','voice':'test','arrangement_id':pid}
    assert client.post('/azure_api/tts',headers=b,json=payload).status_code==404
    assert client.post('/azure_api/tts',headers=a,json={**payload,'text':'已修改'}).status_code==409
    submitted=client.post('/azure_api/tts',headers=a,json=payload)
    assert submitted.status_code==202
    with get_db() as conn:
        task=dict(conn.execute('SELECT * FROM tasks WHERE task_id=?',(submitted.json()['task_id'],)).fetchone())
    assert json.loads(task['options'])['arrangement']['blocks'][0]['text']==TEXT
    assert task['text']==TEXT
    preview=client.post(f'/azure_api/reading-plans/{pid}/preview',headers=a)
    assert preview.status_code==202
    assert preview.json()['task_id'] not in [t['task_id'] for t in client.get('/azure_api/tts',headers=a).json()]


def test_arrangement_reaches_synthesis(monkeypatch):
    import synthesis, wave, io
    monkeypatch.setattr('reading_plan.ask_model',model_result)
    for provider in ['azure','minimax']:
        voice=normalize('azure',{'ShortName':'test','Locale':'zh-CN','StyleList':['calm']}) if provider=='azure' else normalize('minimax',{'voice_id':'test'})
        plan=build_plan(TEXT,provider,voice,'mother','',1,0)
        def fake_azure(text,voice,rate,path,pitch,sentence_events=None,ssml_content=None):
            assert text==TEXT and '<break time="600ms"/>' in ssml_content
            with wave.open(path,'wb') as f:
                f.setparams((1,2,32000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*3200)
            sentence_events.append({'text':text,'start_ms':0,'end_ms':90})
            return [],100
        buf=io.BytesIO()
        with wave.open(buf,'wb') as f:
            f.setparams((1,2,32000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*3200)
        def fake_mini(path,payload):
            assert '<#0.60#>' in payload['text']
            assert payload['voice_setting']['emotion']=='calm'
            assert payload['voice_setting']['speed']==0.85
            return {'data':{'audio':buf.getvalue().hex()}}
        monkeypatch.setattr('worker._synth_one',fake_azure)
        monkeypatch.setattr(synthesis,'minimax_post',fake_mini)
        captured={}
        monkeypatch.setattr('worker._mark_status',lambda tid,status,**kwargs:captured.update(kwargs))
        synthesis.synthesize_story({'task_id':'arrangement-test-'+provider,'text':TEXT,'voice':'test','rate':'0%','pitch':'0Hz','provider':provider,'options':json.dumps({'arrangement':plan})})
        metadata=json.loads(captured['metadata'])
        assert metadata['synthesis_mode']=='ai_arranged_blocks' and metadata['block_count']==1
        assert metadata['reading_plan']['source_text']==TEXT


def test_model_tool_array_envelopes():
    from reading_plan import ModelPlan
    result = ModelPlan.model_validate({'summary': '舒缓', 'blocks': {'item': {
        'index': 0, 'speed': .85, 'pitch': 0, 'emotion': 'calm', 'explanation': '',
        'pauses': {'item': [{'after': '很久以前，', 'duration_ms': 400}]},
        'emphasis': {'item': {'text': '温暖'}}}}})
    assert result.blocks[0].pauses[0].duration_ms == 400
    assert result.blocks[0].emphasis[0].text == '温暖'
