"""Authenticated, owner-scoped TTS API shared by legacy UI and Baby Story."""
import json
import uuid
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, model_validator
from config import DEFAULT_VOICE, AUDIO_DIR, MAX_PENDING, SPEECH_KEY, MINIMAX_API_KEY
from database import get_db
from worker import _queue
from providers import voice_catalog
from security import authenticate, limit_submission
from synthesis import sentences as split_sentences, subtitles

router = APIRouter()

class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    provider: Literal['azure', 'minimax'] = 'azure'
    voice: str = Field(default=DEFAULT_VOICE, min_length=1, max_length=200)
    rate: str = Field(default='+20%', pattern=r'^(?:[+-]?\d{1,3}%|[0-2](?:\.\d{1,2})?)$')
    pitch: str = Field(default='+0Hz', pattern=r'^[+-]?\d{1,3}(?:Hz|%)$')
    speed: float = Field(default=1, ge=0.5, le=2)
    minimax_pitch: int = Field(default=0, ge=-12, le=12)
    model: Literal['speech-2.8-hd', 'speech-2.8-turbo', 'speech-2.6-hd', 'speech-2.6-turbo', 'speech-02-hd', 'speech-02-turbo'] | None = None
    sentences: list[str] | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def validate_segments(self):
        parts = self.sentences or split_sentences(self.text)
        if not self.text.strip() or not parts or any(not p.strip() or len(p) > 3000 for p in parts):
            raise ValueError('Text and sentences must be nonempty; each sentence must be at most 3000 characters')
        if len(parts) > 200 or sum(map(len, parts)) > 20000:
            raise ValueError('At most 200 sentences and 20000 characters are allowed')
        if self.sentences and ''.join(self.text.split()) != ''.join(''.join(parts).split()):
            raise ValueError('sentences must reproduce text (ignoring whitespace)')
        return self

def owned(task_id, client):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM tasks WHERE task_id=? AND owner=?', (task_id, client)).fetchone()
    if not row:
        raise HTTPException(404, 'task not found')
    return dict(row)

def present(task):
    for key in ('word_timings', 'sentence_timings', 'metadata'):
        task[key] = json.loads(task[key]) if task.get(key) else None
    for key in ('owner', 'options', 'audio_file', 'result_url'):
        task.pop(key, None)
    if task['status'] == 'completed':
        base = f'/azure_api/tts/{task["task_id"]}'
        task.update(audio_url=f'/azure_api/tts/audio/{task["task_id"]}', timing_url=base+'/timing',
                    subtitles={'srt': base+'/subtitles.srt', 'vtt': base+'/subtitles.vtt'},
                    duration_ms=task['total_ms'])
    return task

@router.get('/health')
def health():
    return {'status': 'ok'}

@router.get('/voices')
def voices(request: Request, provider: Literal['azure', 'minimax'] = 'azure',
           q: str = '', client: str = Depends(authenticate)):
    try:
        catalog = voice_catalog(provider)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from None
    facets = {}
    for voice in catalog:
        for key, tags in voice['facets'].items():
            facets.setdefault(key, set()).update(tags)
    # AND across dimensions; OR across repeated values of the same dimension.
    filters = {k: request.query_params.getlist(k) for k in request.query_params if k not in ('provider', 'q')}
    unknown = set(filters) - set(facets)
    if unknown:
        raise HTTPException(422, 'Unknown filter dimensions: ' + ', '.join(sorted(unknown)))
    result = [v for v in catalog if (not q or q.casefold() in json.dumps(v, ensure_ascii=False).casefold())
              and all(set(vals) & set(v['facets'].get(key, [])) for key, vals in filters.items())]
    return {'voices': result, 'total': len(result), 'catalog_total': len(catalog),
            'facets': {k: sorted(v) for k, v in facets.items()}}

@router.post('/tts', status_code=202)
def create_tts_task(body: SynthesisRequest, client: str = Depends(authenticate)):
    return enqueue_synthesis(body, client)

def enqueue_synthesis(body, client, preview=False):
    if not (SPEECH_KEY if body.provider == 'azure' else MINIMAX_API_KEY):
        raise HTTPException(503, body.provider + ' is not configured')
    options = body.model_dump()
    if preview:
        options['preview'] = True
    serialized = json.dumps(options, ensure_ascii=False, sort_keys=True)
    task_id = 'tts_' + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if preview:
            cached = conn.execute("SELECT * FROM tasks WHERE owner=? AND options=? AND status IN ('pending','processing','completed') ORDER BY created_at DESC LIMIT 1", (client, serialized)).fetchone()
            if cached and (cached['status'] != 'completed' or (cached['audio_file'] and (AUDIO_DIR / cached['audio_file']).is_file())):
                return {'task_id': cached['task_id'], 'status': cached['status'], 'provider': body.provider}
        limit_submission(client)
        count = conn.execute("SELECT count(*) FROM tasks WHERE status IN ('pending','processing')").fetchone()[0]
        if count >= MAX_PENDING:
            raise HTTPException(429, 'Task queue is full', headers={'Retry-After': '30'})
        conn.execute('INSERT INTO tasks (task_id,text,voice,rate,pitch,provider,owner,options,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (task_id, body.text, body.voice, body.rate, body.pitch, body.provider, client,
                      serialized, now, now))
        conn.commit()
    _queue.put(task_id)
    return {'task_id': task_id, 'status': 'pending', 'provider': body.provider,
            'status_url': '/azure_api/tts/' + task_id}

@router.get('/tts')
def list_tasks(limit: int = Query(50, ge=1, le=200), client: str = Depends(authenticate)):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE owner=? AND COALESCE(json_extract(options, '$.preview'), 0)=0 ORDER BY created_at DESC LIMIT ?", (client, limit)).fetchall()
    return [present(dict(r)) for r in rows]

@router.get('/tts/{task_id}')
def get_task(task_id: str, client: str = Depends(authenticate)):
    return present(owned(task_id, client))

@router.get('/tts/audio/{task_id}')
def download_audio(task_id: str, client: str = Depends(authenticate)):
    task = owned(task_id, client)
    if task['status'] != 'completed' or not task['audio_file']:
        raise HTTPException(404, 'audio not ready')
    path = AUDIO_DIR / task['audio_file']
    if not path.is_file():
        raise HTTPException(404, 'audio not found')
    return FileResponse(path, media_type='audio/mpeg', filename=path.name, headers={'Cache-Control': 'private, no-store'})

@router.get('/tts/{task_id}/timing')
def download_timing(task_id: str, client: str = Depends(authenticate)):
    task = owned(task_id, client)
    if task['status'] != 'completed':
        raise HTTPException(409, 'task not complete')
    return {key: json.loads(task[key] or '[]') for key in ('sentence_timings', 'word_timings')}

@router.get('/tts/{task_id}/subtitles.{fmt}')
def download_subtitles(task_id: str, fmt: Literal['srt', 'vtt'], client: str = Depends(authenticate)):
    task = owned(task_id, client)
    if task['status'] != 'completed' or not task['sentence_timings']:
        raise HTTPException(409, 'subtitles not ready')
    return Response(subtitles(json.loads(task['sentence_timings']), fmt),
        media_type='text/vtt' if fmt == 'vtt' else 'application/x-subrip',
        headers={'Content-Disposition': f'attachment; filename="{task_id}.{fmt}"'})

@router.delete('/tts/{task_id}')
def delete_task(task_id: str, client: str = Depends(authenticate)):
    task = owned(task_id, client)
    if task['status'] in ('pending', 'processing'):
        raise HTTPException(409, 'Cannot delete an active task')
    if task['audio_file']:
        (AUDIO_DIR / task['audio_file']).unlink(missing_ok=True)
    with get_db() as conn:
        conn.execute('DELETE FROM tasks WHERE task_id=? AND owner=?', (task_id, client))
        conn.commit()
    return {'status': 'deleted'}


class SavedPreset(BaseModel):
    provider: Literal['azure', 'minimax']
    voice: str = Field(min_length=1, max_length=200)
    speed: float = Field(ge=0.5, le=2)
    pitch: int = Field(ge=-50, le=50)
    filters: dict[str, str] = Field(default_factory=dict, max_length=40)

    @model_validator(mode='after')
    def check_pitch(self):
        if self.provider == 'minimax' and not -12 <= self.pitch <= 12:
            raise ValueError('MiniMax pitch must be -12 to 12')
        if any(len(k) > 100 or len(v) > 200 for k, v in self.filters.items()):
            raise ValueError('Filter value too long')
        return self

class PreferencesUpdate(BaseModel):
    languages: list[str] = Field(default_factory=lambda: ['zh', 'en'], max_length=300)
    preset: SavedPreset | None = None

    @model_validator(mode='after')
    def check_languages(self):
        import re
        if any(not re.fullmatch(r'[a-z]{2,3}|unknown', value) for value in self.languages):
            raise ValueError('Use language-family codes, such as zh or en')
        self.languages = list(dict.fromkeys(self.languages))
        return self

def read_preferences(conn, client):
    row = conn.execute('SELECT data FROM preferences WHERE owner=?', (client,)).fetchone()
    data = {'preset': None, **(json.loads(row['data']) if row else {})}
    # Preserve an existing global selection as the initial value for each platform.
    legacy = data.pop('languages', ['zh', 'en'])
    per_provider = data.setdefault('provider_languages', {})
    for provider in ('azure', 'minimax'):
        per_provider.setdefault(provider, list(legacy))
    if 'presets' not in data:
        data['presets'] = []
        data['default_preset_id'] = None
        if data.get('preset'):
            data['presets'].append({'id': 'legacy-default', 'name': '原默认配置', **data['preset']})
            data['default_preset_id'] = 'legacy-default'
    return data

def platform_preferences(data, provider):
    return {'provider': provider, 'languages': data['provider_languages'][provider], 'preset': data.get('preset'), 'presets': data['presets'], 'default_preset_id': data.get('default_preset_id')}

@router.get('/preferences')
def get_preferences(provider: Literal['azure', 'minimax'] = 'azure', client: str = Depends(authenticate)):
    with get_db() as conn:
        return platform_preferences(read_preferences(conn, client), provider)

@router.put('/preferences')
def save_preferences(body: PreferencesUpdate, provider: Literal['azure', 'minimax'] = 'azure', client: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        data = read_preferences(conn, client)
        changes = body.model_dump(exclude_unset=True)
        if 'languages' in changes:
            data['provider_languages'][provider] = changes['languages']
        if 'preset' in changes:
            data['preset'] = changes['preset']
        conn.execute('INSERT INTO preferences(owner,data) VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET data=excluded.data',
                     (client, json.dumps(data, ensure_ascii=False)))
        conn.commit()
    return platform_preferences(data, provider)


class NamedPreset(SavedPreset):
    name: str = Field(min_length=1, max_length=60)

    @model_validator(mode='after')
    def trim_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError('A configuration name is required')
        return self


def persist_preferences(conn, client, data):
    conn.execute('INSERT INTO preferences(owner,data) VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET data=excluded.data',
                 (client, json.dumps(data, ensure_ascii=False)))
    conn.commit()


def activate_preset(data, preset):
    data['default_preset_id'] = preset['id']
    data['preset'] = SavedPreset.model_validate(preset).model_dump()


@router.post('/presets', status_code=201)
def create_preset(body: NamedPreset, client: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        data = read_preferences(conn, client)
        if len(data['presets']) >= 50:
            raise HTTPException(422, 'At most 50 configurations can be saved')
        preset = {'id': uuid.uuid4().hex, **body.model_dump()}
        data['presets'].append(preset)
        activate_preset(data, preset)
        persist_preferences(conn, client, data)
    return platform_preferences(data, body.provider)


@router.put('/presets/{preset_id}')
def update_preset(preset_id: str, body: NamedPreset, client: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        data = read_preferences(conn, client)
        target = next((p for p in data['presets'] if p['id'] == preset_id), None)
        if not target:
            raise HTTPException(404, 'configuration not found')
        target.update(body.model_dump())
        activate_preset(data, target)
        persist_preferences(conn, client, data)
    return platform_preferences(data, body.provider)


@router.post('/presets/{preset_id}/load')
def load_preset(preset_id: str, client: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        data = read_preferences(conn, client)
        preset = next((p for p in data['presets'] if p['id'] == preset_id), None)
        if not preset:
            raise HTTPException(404, 'configuration not found')
        activate_preset(data, preset)
        persist_preferences(conn, client, data)
    return platform_preferences(data, preset['provider'])


@router.delete('/presets/{preset_id}')
def delete_preset(preset_id: str, client: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        data = read_preferences(conn, client)
        if not any(p['id'] == preset_id for p in data['presets']):
            raise HTTPException(404, 'configuration not found')
        data['presets'] = [p for p in data['presets'] if p['id'] != preset_id]
        if data.get('default_preset_id') == preset_id:
            data['default_preset_id'] = None
            data['preset'] = None
        persist_preferences(conn, client, data)
    return {'status': 'deleted'}


class VoicePreviewRequest(BaseModel):
    provider: Literal['azure', 'minimax']
    voice: str = Field(min_length=1, max_length=200)
    speed: float = Field(default=1, ge=0.5, le=2)
    pitch: int = Field(default=0, ge=-50, le=50)


@router.post('/voices/preview', status_code=202)
def preview_voice(body: VoicePreviewRequest, client: str = Depends(authenticate)):
    if body.provider == 'minimax' and not -12 <= body.pitch <= 12:
        raise HTTPException(422, 'MiniMax pitch must be -12 to 12')
    try:
        voice = next((v for v in voice_catalog(body.provider) if v['id'] == body.voice), None)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from None
    if voice is None:
        raise HTTPException(404, 'voice not found in selected platform')
    languages = voice.get('primary_languages', voice['facets'].get('language', []))
    chinese = not languages or languages[0].lower().startswith('zh')
    text = '你好，欢迎来到故事世界。今晚，让我们一起听一个温暖的故事。' if chinese else 'Hello, welcome to our story world. Tonight, let us share a warm and wonderful story.'
    request = SynthesisRequest(text=text, provider=body.provider, voice=body.voice,
        speed=body.speed, rate=f'{round((body.speed-1)*100)}%',
        pitch=f'{body.pitch}Hz', minimax_pitch=body.pitch if body.provider == 'minimax' else 0)
    return enqueue_synthesis(request, client, preview=True)
