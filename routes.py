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
    if not (SPEECH_KEY if body.provider == 'azure' else MINIMAX_API_KEY):
        raise HTTPException(503, body.provider + ' is not configured')
    limit_submission(client)
    task_id = 'tts_' + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute("SELECT count(*) FROM tasks WHERE status IN ('pending','processing')").fetchone()[0]
        if count >= MAX_PENDING:
            raise HTTPException(429, 'Task queue is full', headers={'Retry-After': '30'})
        conn.execute('INSERT INTO tasks (task_id,text,voice,rate,pitch,provider,owner,options,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (task_id, body.text, body.voice, body.rate, body.pitch, body.provider, client,
                      body.model_dump_json(), now, now))
        conn.commit()
    _queue.put(task_id)
    return {'task_id': task_id, 'status': 'pending', 'provider': body.provider,
            'status_url': '/azure_api/tts/' + task_id}

@router.get('/tts')
def list_tasks(limit: int = Query(50, ge=1, le=200), client: str = Depends(authenticate)):
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM tasks WHERE owner=? ORDER BY created_at DESC LIMIT ?', (client, limit)).fetchall()
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
