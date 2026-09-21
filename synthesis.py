"""Continuous synthesis with provider-native sentence and word timing data."""
import os
import json
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from config import AUDIO_DIR, MINIMAX_MODEL
from providers import minimax_post
from reading_plan import minimax_text, azure_content


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

def fetch_minimax_subtitles(url):
    """Download provider-generated subtitles without forwarding API credentials."""
    import requests
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError('Invalid provider subtitle URL')
    try:
        response = requests.get(url, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError('Expected subtitle array')
        return data
    except (requests.RequestException, ValueError):
        raise RuntimeError('Provider subtitle download failed') from None


def native_words_to_sentences(text, words, requested=None):
    """Group provider word times by original text. Never interpolate timings."""
    positioned, cursor = [], 0
    for word in words:
        start = word.get('text_start')
        end = word.get('text_end')
        if start is None or end is None or text[start:end] != word['text']:
            start = text.find(word['text'], cursor)
            if start < 0:
                continue
            end = start + len(word['text'])
        cursor = end
        positioned.append((start, end, word))
    result, cursor = [], 0
    for sentence in requested or sentences(text):
        start = text.find(sentence, cursor)
        if start < 0:
            return []
        end = start + len(sentence)
        relevant = [w for a, b, w in positioned if a < end and b > start]
        if not relevant:
            return []
        result.append({'text': sentence, 'start_ms': relevant[0]['start_ms'],
                       'end_ms': relevant[-1]['end_ms']})
        cursor = end
    return result


def parse_minimax_timings(raw, text):
    words, segments = [], []
    for segment in raw:
        segments.append({'text': re.sub(r'<#[^>]*#>', '', segment['text']), 'start_ms': round(segment['time_begin']),
                         'end_ms': round(segment['time_end'])})
        for word in segment.get('timestamped_words', []):
            if re.fullmatch(r'<#[^>]*#>', word['word']):
                continue
            words.append({'text': word['word'], 'start_ms': round(word['time_begin']),
                          'end_ms': round(word['time_end']), 'text_start': word.get('word_begin'),
                          'text_end': word.get('word_end')})
    grouped = native_words_to_sentences(text, words) if words else []
    return grouped or segments, words, 'native_words_grouped' if grouped else 'native_subtitle_segments'


def synthesize_story(task):
    from worker import _synth_one, _mark_status
    from chunker import split_text
    options = json.loads(task['options'] or '{}')
    # Keep all nearby sentences together. This bound is for request size and
    # latency, not subtitle segmentation; no per-sentence synthesis calls.
    arrangement = options.get('arrangement')
    controls = arrangement['blocks'] if arrangement else []
    blocks = [b['text'] for b in controls] if controls else split_text(task['text'], 3000)
    timings, words, provider_metadata, warnings = [], [], [], []
    frames_total = 0
    final = AUDIO_DIR / (task['task_id'] + '.mp3')
    sources = set()
    with tempfile.TemporaryDirectory(dir=AUDIO_DIR) as temp:
        root = Path(temp)
        with wave.open(str(root / 'joined.wav'), 'wb') as joined:
            joined.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            for block_index, text in enumerate(blocks):
                raw_audio, pcm = root / 'block.mp3', root / 'block.wav'
                block_words, block_sentences = [], []
                control = controls[block_index] if controls else None
                if task['provider'] == 'minimax':
                    data = minimax_post('/v1/t2a_v2', {
                        'model': options.get('model') or MINIMAX_MODEL, 'text': minimax_text(control) if control else text,
                        'stream': False, 'output_format': 'hex', 'subtitle_enable': True, 'subtitle_type': 'word',
                        'voice_setting': {'voice_id': task['voice'], 'speed': control['speed'] if control else options.get('speed', 1),
                                          'pitch': control['pitch'] if control else options.get('minimax_pitch', 0), 'vol': 1,
                                          **({'emotion': control['emotion']} if control and control['emotion'] != 'neutral' else {})},
                        'audio_setting': {'format': 'mp3', 'sample_rate': 32000, 'bitrate': 128000, 'channel': 1}})
                    audio = (data.get('data') or {}).get('audio')
                    if not audio:
                        raise RuntimeError('MiniMax returned no audio')
                    raw_audio.write_bytes(bytes.fromhex(audio))
                    raw_subtitles = []
                    subtitle_file = (data.get('data') or {}).get('subtitle_file')
                    source = 'unavailable'
                    if subtitle_file:
                        try:
                            raw_subtitles = fetch_minimax_subtitles(subtitle_file)
                            block_sentences, block_words, source = parse_minimax_timings(raw_subtitles, text)
                        except (RuntimeError, KeyError, TypeError, ValueError):
                            warnings.append(f'Block {block_index}: native subtitles unavailable')
                    else:
                        warnings.append(f'Block {block_index}: provider returned no subtitles')
                    provider_metadata.append({'trace_id': data.get('trace_id'), 'extra_info': data.get('extra_info'),
                                              'subtitle_data': raw_subtitles, 'timing_source': source})
                else:
                    block_words, _ = _synth_one(text, task['voice'], task['rate'], str(raw_audio), task['pitch'],
                                               sentence_events=block_sentences, **({"ssml_content": azure_content(control)} if control else {}))
                    source = 'azure_sentence_boundary'
                    if not block_sentences:
                        block_sentences = native_words_to_sentences(text, block_words)
                        source = 'native_words_grouped' if block_sentences else 'unavailable'
                    provider_metadata.append({'sentence_events': list(block_sentences), 'timing_source': source})
                if not block_sentences:
                    warnings.append(f'Block {block_index}: no native sentence timestamps; not estimated')
                sources.add(source)
                ffmpeg('-i', raw_audio, '-ar', 32000, '-ac', 1, '-c:a', 'pcm_s16le', pcm)
                with wave.open(str(pcm), 'rb') as segment:
                    frames = segment.getnframes()
                    if frames == 0:
                        raise RuntimeError('Provider returned empty audio')
                    joined.writeframes(segment.readframes(frames))
                # Measuring block duration only positions later blocks on the
                # joined track; sentence durations always come from native data.
                offset = round(frames_total * 1000 / 32000)
                provider_metadata[-1].update(block_index=block_index, start_ms=offset, text=text)
                frames_total += frames
                for sentence in block_sentences:
                    start, end = sentence['start_ms'], sentence['end_ms']
                    timings.append({'index': len(timings), 'text': sentence['text'],
                                    'start_ms': offset + start, 'end_ms': offset + end,
                                    'duration_ms': end - start, 'timing_source': source})
                words.extend({'text': w['text'], 'start_ms': offset + w['start_ms'],
                              'end_ms': offset + w['end_ms']} for w in block_words)
        ffmpeg('-i', root / 'joined.wav', '-c:a', 'libmp3lame', '-b:a', '128k', final)
    if options.get('sentences'):
        grouped = native_words_to_sentences(task['text'], words, options['sentences'])
        if grouped:
            timings = [{'index': i, **t, 'duration_ms': t['end_ms']-t['start_ms'],
                        'timing_source': 'native_words_grouped'} for i, t in enumerate(grouped)]
            sources.add('native_words_grouped')
        else:
            warnings.append('Custom sentence grouping unavailable; retained provider boundaries')
    _mark_status(task['task_id'], 'completed', audio_file=final.name,
        total_ms=round(frames_total * 1000 / 32000), word_timings=json.dumps(words, ensure_ascii=False),
        sentence_timings=json.dumps(timings, ensure_ascii=False), metadata=json.dumps({
            'timing_source': 'provider_native', 'timing_sources': sorted(sources),
            'synthesis_mode': 'ai_arranged_blocks' if arrangement else 'continuous_blocks', 'block_count': len(blocks),
            'reading_plan': arrangement,
            'sample_rate': 32000, 'channels': 1, 'format': 'mp3', 'size_bytes': final.stat().st_size,
            'word_timings_available': bool(words), 'timing_warnings': warnings,
            'provider_segments': provider_metadata}, ensure_ascii=False))
