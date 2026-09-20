"""Baby Story Creator server-side integration example (requests + mTLS).

Environment: STORY_TTS_TOKEN, STORY_TTS_CERT, STORY_TTS_CERT_KEY.
Never include these credentials in a browser bundle.
"""
import os
import time
from urllib.parse import urljoin, urlsplit
import requests

class StoryVoiceClient:
    def __init__(self, base_url=None, token=None, cert=None):
        self.base = (base_url or os.environ.get('STORY_TTS_URL',
                     'https://tools.exnihilo.site/api/v1')).rstrip('/')
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'Bearer ' + (token or os.environ['STORY_TTS_TOKEN'])
        self.session.cert = cert or (os.environ['STORY_TTS_CERT'], os.environ['STORY_TTS_CERT_KEY'])

    def synthesize(self, text, voice, provider='azure', timeout=1800, **options):
        response = self.session.post(self.base+'/tts', json={
            'text': text, 'voice': voice, 'provider': provider, **options}, timeout=30)
        response.raise_for_status()
        task_id = response.json()['task_id']
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.session.get(self.base+'/tts/'+task_id, timeout=30)
            response.raise_for_status()
            result = response.json()
            if result['status'] == 'completed':
                return result
            if result['status'] == 'failed':
                raise RuntimeError(result.get('error') or 'Synthesis failed')
            time.sleep(2)
        raise TimeoutError(f'Task {task_id} is still pending; query it before submitting again')

    def download(self, relative_url, path):
        url = urljoin(self.base+'/', relative_url)
        if urlsplit(url).netloc != urlsplit(self.base).netloc or urlsplit(url).scheme != 'https':
            raise ValueError('Download must use the same HTTPS origin')
        with self.session.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(path, 'wb') as output:
                for chunk in response.iter_content(65536):
                    output.write(chunk)
