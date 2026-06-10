#!/usr/bin/env python3
"""Azure TTS Service — 250 server, port 8002.

Async TTS with word-level timings, task queue, and SQLite persistence.
"""
import os
import uuid
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

import azure.cognitiveservices.speech as speechsdk
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

# ── Config ────────────────────────────────────────────────────────────
AUDIO_DIR = Path("/opt/azure-tts-service/audio")
AUDIO_DIR.mkdir(exist_ok=True)

DB_PATH = Path("/opt/azure-tts-service/tasks.db")
MAX_QUEUE_WORKERS = int(os.environ.get("TTS_MAX_WORKERS", "4"))

SPEECH_KEY = os.environ.get("AZURE_SPEECH_KEY")
SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "eastus")
DEFAULT_VOICE = os.environ.get("TTS_DEFAULT_VOICE", "zh-CN-XiaochenNeural")
DEFAULT_RATE = os.environ.get("TTS_DEFAULT_RATE", "+20%")

if not SPEECH_KEY:
    # Load from .env file if env var not set
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "AZURE_SPEECH_KEY":
                SPEECH_KEY = v
            elif k == "AZURE_SPEECH_REGION":
                SPEECH_REGION = v
    if not SPEECH_KEY:
        raise RuntimeError("AZURE_SPEECH_KEY not set")

# ── SQLite ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id     TEXT PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'pending',
            text        TEXT NOT NULL,
            voice       TEXT NOT NULL,
            rate        TEXT NOT NULL,
            audio_file  TEXT,
            word_timings TEXT,   -- JSON string
            total_ms    INTEGER,
            error       TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="Azure TTS Service")

def _task_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("word_timings"):
        import json
        d["word_timings"] = json.loads(d["word_timings"])
    d.pop("text", None)  # hide raw text in list responses
    return d


@app.post("/tts")
def create_tts_task(text: str, voice: str = DEFAULT_VOICE, rate: str = DEFAULT_RATE):
    """Submit a TTS task. Returns task_id."""
    if not text.strip():
        raise HTTPException(400, "text is empty")

    task_id = f"tts_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, text, voice, rate, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, text, voice, rate, now, now),
        )
        conn.commit()

    _queue.put(task_id)
    return {"task_id": task_id, "status": "pending"}


@app.get("/tts/{task_id}")
def get_task(task_id: str):
    """Query task status and result."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "task not found")

    d = dict(row)
    if d.get("word_timings"):
        import json
        d["word_timings"] = json.loads(d["word_timings"])

    if d["status"] == "completed":
        d["audio_url"] = f"/tts/audio/{d['audio_file']}"

    return d


@app.get("/tts/audio/{task_id}")
def download_audio(task_id: str):
    """Download synthesized audio file by task_id."""
    with get_db() as conn:
        row = conn.execute("SELECT audio_file FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row or not row["audio_file"]:
        raise HTTPException(404, "audio not found")
    filepath = AUDIO_DIR / row["audio_file"]
    if not filepath.exists():
        raise HTTPException(404, "audio file missing on disk")
    return FileResponse(str(filepath), media_type="audio/mpeg")


@app.get("/tts")
def list_tasks(limit: int = 50):
    """List recent tasks."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_task_to_dict(r) for r in rows]


@app.delete("/tts/{task_id}")
def delete_task(task_id: str):
    """Delete a task and its audio file."""
    with get_db() as conn:
        row = conn.execute("SELECT audio_file FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "task not found")
        if row["audio_file"]:
            try:
                (AUDIO_DIR / row["audio_file"]).unlink(missing_ok=True)
            except OSError:
                pass
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()
    return {"status": "deleted"}


@app.get("/health")
def health():
    return {"status": "ok", "queue_size": _queue.qsize()}


# ── Worker Queue ──────────────────────────────────────────────────────
import queue

_queue: queue.Queue = queue.Queue()


def _worker():
    """Background worker: pop task_id from queue, synthesize, update DB."""
    while True:
        task_id = _queue.get()
        if task_id is None:
            break
        try:
            _synthesize(task_id)
        except Exception as e:
            _mark_failed(task_id, str(e))
        finally:
            _queue.task_done()


def _mark_status(task_id: str, status: str, **extra):
    now = datetime.utcnow().isoformat()
    # Build SET clause parts
    parts = ["status = ?", "updated_at = ?"]
    vals = [status, now]
    
    for k, v in extra.items():
        parts.append(f"{k} = ?")
        vals.append(v)
    
    query = f"UPDATE tasks SET {', '.join(parts)} WHERE task_id = ?"
    vals.append(task_id)
    
    with get_db() as conn:
        conn.execute(query, vals)
        conn.commit()


def _mark_failed(task_id: str, error: str):
    _mark_status(task_id, "failed", error=error)


def _synthesize(task_id: str):
    import json

    # Fetch task info
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return
        task = dict(row)

    _mark_status(task_id, "processing")

    # SSML with word boundary events
    voice = task["voice"]
    rate = task["rate"]
    text = task["text"]
    ssml = (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='zh-CN'>"
        f"<voice name='{voice}'>"
        f"<mstts:viseme type='word_boundary'/>"
        f"<prosody rate='{rate}'>"
        f"{text}"
        f"</prosody>"
        f"</voice>"
        f"</speak>"
    )

    speech_config = speechsdk.SpeechConfig(subscription=SPEECH_KEY, region=SPEECH_REGION)
    speech_config.speech_synthesis_output_format = (
        speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
    )

    audio_file = f"{task_id}.mp3"
    audio_path = str(AUDIO_DIR / audio_file)
    audio_config = speechsdk.audio.AudioOutputConfig(filename=audio_path)

    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=audio_config
    )

    timings = []

    def _on_word_boundary(evt: speechsdk.SpeechSynthesisWordBoundaryEventArgs):
        timings.append({
            "text": evt.text,
            "offset_ms": evt.audio_offset.ticks // 10000,
        })

    synthesizer.synthesis_word_boundary.connect(_on_word_boundary)
    result = synthesizer.speak_ssml_async(ssml).get()

    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        cancellation = result.cancellation_details
        raise RuntimeError(f"TTS failed: {cancellation.reason} — {cancellation.error_details}")

    # Calculate total duration from audio file
    import mutagen.mp3
    try:
        audio = mutagen.mp3.MP3(audio_path)
        total_ms = int(audio.info.length * 1000)
    except Exception:
        total_ms = None

    # Calculate end_ms for each word (end = next word's offset, last = total_ms)
    word_timings = []
    for i, t in enumerate(timings):
        start = int(t["offset_ms"])
        if i + 1 < len(timings):
            end = int(timings[i + 1]["offset_ms"])
        else:
            end = total_ms or start  # fallback
        word_timings.append({
            "text": t["text"],
            "start_ms": start,
            "end_ms": end,
        })

    _mark_status(
        task_id,
        "completed",
        audio_file=audio_file,
        word_timings=json.dumps(word_timings, ensure_ascii=False),
        total_ms=total_ms,
    )


# ── Startup ───────────────────────────────────────────────────────────
def _start_workers():
    """Initialize DB and start worker threads."""
    init_db()
    for _ in range(MAX_QUEUE_WORKERS):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()

# Start workers on import
_start_workers()
