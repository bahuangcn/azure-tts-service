"""Sentence-by-sentence synthesis gives measured, non-estimated boundaries.

Each segment is decoded to identical PCM before concatenation, avoiding MP3
encoder padding drift. Sentence audio spans include their trailing silence.
"""
import os
import json
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from config import AUDIO_DIR, MINIMAX_MODEL
from providers import minimax_post


def sentences(text):
    return [s.strip() for s in re.split(r'(?<=[。！？!?])\s*|(?<=\.)\s+|\n+', text) if s.strip()]

def timestamp(ms, vtt=False):
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f'{h:02}:{m:02}:{s:02}{"." if vtt else ","}{milli:03}'

def subtitles(timings, fmt):
    cues = [f'{i}\n{timestamp(s["start_ms"], fmt == "vtt")} --> {timestamp(s["end_ms"], fmt == "vtt")}\n{s["text"]}\n'
            for i, s in enumerate(timings, 1)]
    return ('WEBVTT\n\n' if fmt == 'vtt' else '') + '\n'.join(cues)

def ffmpeg(*args):
    try:
        subprocess.run([os.environ.get('TTS_FFMPEG', 'ffmpeg'), '-nostdin', '-v', 'error', '-y', *map(str, args)],
                       check=True, capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Audio conversion failed; check ffmpeg installation') from None

def synthesize_story(task):
    from worker import _synth_one, _mark_status
    options = json.loads(task['options'] or '{}')
    segments = options.get('sentences') or sentences(task['text'])
    timings, words, provider_metadata = [], [], []
    frames_total = 0
    final = AUDIO_DIR / (task['task_id'] + '.mp3')
    with tempfile.TemporaryDirectory(dir=AUDIO_DIR) as temp:
        root = Path(temp)
        with wave.open(str(root / 'joined.wav'), 'wb') as joined:
            joined.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            for index, text in enumerate(segments):
                raw, pcm = root / 'segment.mp3', root / 'segment.wav'
                segment_words = []
                if task['provider'] == 'minimax':
                    data = minimax_post('/v1/t2a_v2', {
                        'model': options.get('model') or MINIMAX_MODEL, 'text': text,
                        'stream': False, 'output_format': 'hex',
                        'voice_setting': {'voice_id': task['voice'], 'speed': options.get('speed', 1),
                                          'pitch': options.get('minimax_pitch', 0), 'vol': 1},
                        'audio_setting': {'format': 'mp3', 'sample_rate': 32000, 'bitrate': 128000, 'channel': 1}})
                    audio = (data.get('data') or {}).get('audio')
                    if not audio:
                        raise RuntimeError('MiniMax returned no audio')
                    raw.write_bytes(bytes.fromhex(audio))
                    provider_metadata.append({'trace_id': data.get('trace_id'), 'extra_info': data.get('extra_info')})
                else:
                    segment_words, _ = _synth_one(text, task['voice'], task['rate'], str(raw), task['pitch'])
                ffmpeg('-i', raw, '-ar', 32000, '-ac', 1, '-c:a', 'pcm_s16le', pcm)
                with wave.open(str(pcm), 'rb') as segment:
                    frames = segment.getnframes()
                    if frames == 0:
                        raise RuntimeError('Provider returned empty audio')
                    joined.writeframes(segment.readframes(frames))
                start = round(frames_total * 1000 / 32000)
                frames_total += frames
                end = round(frames_total * 1000 / 32000)
                timings.append({'index': index, 'text': text, 'start_ms': start,
                    'end_ms': end, 'duration_ms': end - start})
                words.extend({**w, 'start_ms': min(end, start + w['start_ms']),
                              'end_ms': min(end, start + w['end_ms'])} for w in segment_words)
        ffmpeg('-i', root / 'joined.wav', '-c:a', 'libmp3lame', '-b:a', '128k', final)
    _mark_status(task['task_id'], 'completed', audio_file=final.name,
        total_ms=round(frames_total * 1000 / 32000), word_timings=json.dumps(words, ensure_ascii=False),
        sentence_timings=json.dumps(timings, ensure_ascii=False), metadata=json.dumps({
            'timing_source': 'measured_sentence_audio', 'sample_rate': 32000, 'channels': 1,
            'format': 'mp3', 'size_bytes': final.stat().st_size,
            'word_timings_available': task['provider'] == 'azure' and bool(words),
            'provider_segments': provider_metadata}, ensure_ascii=False))
