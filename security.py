"""Per-client bearer authentication; secrets never appear in URLs."""
import secrets
import threading
import time
from collections import defaultdict, deque
from fastapi import HTTPException, Request
from config import API_KEYS, RATE_LIMIT

_lock = threading.Lock()
_calls = defaultdict(deque)

def authenticate(request: Request) -> str:
    scheme, _, token = request.headers.get('authorization', '').partition(' ')
    if not API_KEYS:
        raise HTTPException(503, 'API credentials are not configured')
    if scheme.lower() == 'bearer' and token:
        for client, key in API_KEYS.items():
            if secrets.compare_digest(token.encode(), key.encode()):
                return client
    raise HTTPException(401, 'Valid Bearer token required', headers={'WWW-Authenticate': 'Bearer'})

def limit_submission(client: str):
    now = time.monotonic()
    with _lock:
        calls = _calls[client]
        while calls and calls[0] <= now - 60:
            calls.popleft()
        if len(calls) >= RATE_LIMIT:
            raise HTTPException(429, 'Submission rate limit exceeded', headers={'Retry-After': '60'})
        calls.append(now)
