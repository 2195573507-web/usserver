#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB = Path('/opt/shared-agent-memory/data/memory.sqlite')
API = 'http://[REDACTED_IP]:9400/api/local-task'

def count_recent_tasks() -> str:
    try:
        with sqlite3.connect(DB) as con:
            total = con.execute("SELECT count(*) FROM local_tasks").fetchone()[0]
            queued = con.execute("SELECT count(*) FROM local_tasks WHERE status='queued'").fetchone()[0]
            done = con.execute("SELECT count(*) FROM local_tasks WHERE status='done'").fetchone()[0]
            fallback = con.execute("SELECT count(*) FROM local_tasks WHERE status in ('fallback','failed')").fetchone()[0]
        return f'local_tasks total={total}, queued={queued}, done={done}, fallback_or_failed={fallback}'
    except Exception as exc:
        return f'local_tasks 状态读取失败：{exc}'

now = datetime.now(timezone.utc).isoformat()
content = f'''
定时整理任务触发时间：{now}
请基于以下状态生成一份很短的中文整理摘要草稿：
- {count_recent_tasks()}
- 目标：概括本地轻量任务池运行状态、可能的积压/失败情况、下一步建议。
- 限制：只基于给出的文本做摘要草稿，不添加额外事实。
'''.strip()

payload = {
    'task_type': 'report-draft',
    'content': content,
    'source_agent': 'qwen-organize-timer',
    'sync': False,
    'timeout_seconds': 180,
    'metadata': {'origin': 'qwen-organize-timer', 'scheduled': True, 'created_at': now},
}
req = urllib.request.Request(
    API,
    data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
    method='POST',
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(resp.read().decode('utf-8'))
