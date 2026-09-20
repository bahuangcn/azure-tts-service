"""Provider HTTP clients and cached, lossless voice catalogs."""
import re
import threading
import time
import requests
from config import SPEECH_KEY, SPEECH_REGION, MINIMAX_API_KEY, MINIMAX_BASE_URL

_cache = {}
_lock = threading.Lock()

def minimax_post(path, payload):
    if not MINIMAX_API_KEY:
        raise RuntimeError('MiniMax is not configured')
    try:
        response = requests.post(MINIMAX_BASE_URL + path, json=payload,
            headers={'Authorization': 'Bearer ' + MINIMAX_API_KEY}, timeout=(10, 180))
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError('MiniMax HTTP request failed') from None
    code = data.get('base_resp', {}).get('status_code')
    if code != 0:
        raise RuntimeError(f'MiniMax provider error code {code}')
    return data

def values(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]

def normalize(provider, raw, source='system'):
    if provider == 'azure':
        facets = {'language': values(raw.get('Locale')) + values(raw.get('SecondaryLocaleList')),
            'gender': values(raw.get('Gender')), 'style': values(raw.get('StyleList')),
            'role': values(raw.get('RolePlayList')), 'type': values(raw.get('VoiceType')),
            'status': values(raw.get('Status')), 'source': ['system']}
        for key, value in (raw.get('VoiceTag') or {}).items():
            facets[key] = values(value)
        return {'id': raw['ShortName'], 'name': raw.get('LocalName') or raw['ShortName'],
            'provider': provider, 'facets': facets, 'official': raw}
    facets = {'source': [source], 'language': values(raw.get('language')),
              'gender': values(raw.get('gender')), 'tag': values(raw.get('description'))}
    # Preserve all official tag fields rather than inventing demographics from IDs.
    for key, value in raw.items():
        if key not in ('voice_id', 'voice_name', 'created_time', 'description'):
            if isinstance(value, dict):
                for subkey, subvalue in value.items():
                    facets[subkey] = values(subvalue)
            elif isinstance(value, (str, list, int, bool)):
                facets[key] = values(value)
    # MiniMax currently supplies prose descriptions, not structured demographics.
    # Expose transparent derived facets; retain descriptions and mark their origin.
    description = ' '.join(values(raw.get('description')))
    derived = {}
    languages = {'zh-CN': ['普通话', 'Mandarin'], 'zh-HK': ['粤语', 'Cantonese'],
        'en': ['英语', 'English'], 'ja': ['日语', 'Japanese'], 'ko': ['韩语', 'Korean'],
        'fr': ['法语', 'French'], 'de': ['德语', 'German'], 'es': ['西班牙语', 'Spanish'],
        'pt': ['葡萄牙语', 'Portuguese'], 'ru': ['俄语', 'Russian'], 'ar': ['阿拉伯语', 'Arabic'],
        'it': ['意大利语', 'Italian'], 'hi': ['印地语', 'Hindi'], 'tr': ['土耳其语', 'Turkish'],
        'vi': ['越南语', 'Vietnamese'], 'th': ['泰语', 'Thai'], 'id': ['印尼语', 'Indonesian']}
    if not facets['language']:
        derived['language'] = [key for key, names in languages.items() if any(n.casefold() in description.casefold() for n in names)]
    if not facets['gender']:
        female = bool(re.search(r'女性|女声|女孩|御姐|大婶|少女|female|woman|girl', description, re.I))
        male = bool(re.search(r'男性|男声|男孩|少年|\bmale\b|\bman\b|\bboy\b', description, re.I))
        derived['gender'] = ['Female'] if female and not male else ['Male'] if male and not female else []
    derived['style'] = [label for label in ['温柔', '温和', '沉稳', '活泼', '甜美', '清晰', '有力', '低沉', '沙哑', '磁性', '可爱', '专业', '欢快', '优雅'] if label in description]
    derived['age'] = [label for label in ['儿童', '青年', '中年', '老年'] if label in description]
    for key, value in derived.items():
        if value:
            facets[key] = value
    facets['classification'] = ['official_description_derived' if any(derived.values()) else 'official_fields']
    return {'id': raw['voice_id'], 'name': raw.get('voice_name') or raw['voice_id'],
            'provider': provider, 'facets': facets, 'official': raw}

def voice_catalog(provider):
    with _lock:
        cached = _cache.get(provider)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        if provider == 'azure':
            if not SPEECH_KEY:
                raise RuntimeError('Azure is not configured')
            try:
                r = requests.get(f'https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/voices/list',
                    headers={'Ocp-Apim-Subscription-Key': SPEECH_KEY}, timeout=(10, 30))
                r.raise_for_status()
                result = [normalize('azure', v) for v in r.json()]
            except (requests.RequestException, ValueError):
                raise RuntimeError('Azure voice catalog unavailable') from None
        else:
            data = minimax_post('/v1/get_voice', {'voice_type': 'all'})
            result = [normalize('minimax', v, source) for field, source in
                [('system_voice', 'system'), ('voice_generation', 'designed'), ('voice_cloning', 'cloned')]
                for v in data.get(field) or []]
        _cache[provider] = (time.monotonic(), result)
        return result
