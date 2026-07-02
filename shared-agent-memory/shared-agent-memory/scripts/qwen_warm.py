#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.request

OLLAMA_URL = 'http://[REDACTED_IP]:11434/api/generate'
MODEL = 'qwen2.5:3b'

payload = {
    'model': MODEL,
    'prompt': '只回复 OK',
    'stream': False,
    'keep_alive': '30m',
    'options': {
        'temperature': 0,
        'num_predict': 3,
    },
}

try:
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        data = json.loads(response.read().decode('utf-8'))
    text = str(data.get('response') or '').strip()
    if not text:
        raise RuntimeError('empty response')
    print(f'qwen warm ok: {text[:20]}')
except Exception as exc:
    print(f'qwen warm failed: {exc}', file=sys.stderr)
    raise
