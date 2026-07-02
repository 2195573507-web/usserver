#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path('/opt/shared-agent-memory')
sys.path.insert(0, str(ROOT))
import main  # noqa: E402

DB_PATH = ROOT / 'data' / 'memory.sqlite'
POLL_SECONDS = 5
BATCH_SIZE = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_queued():
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT * FROM local_tasks
               WHERE status='queued' AND route='qwen'
               ORDER BY created_at ASC LIMIT ?""",
            (BATCH_SIZE,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_task(task_id: str, status: str, **fields):
    allowed = {'route', 'result', 'error', 'model', 'latency_ms'}
    sets = ['updated_at=?', 'status=?']
    values = [now(), status]
    for key, value in fields.items():
        if key in allowed:
            sets.append(f'{key}=?')
            values.append(value)
    values.append(task_id)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"UPDATE local_tasks SET {', '.join(sets)} WHERE id=?", values)


def process(row):
    task_id = row['id']
    update_task(task_id, 'running')
    try:
        worker = main.run_qwen_local_task(row['task_type'], row['content'], timeout_seconds=180)
        update_task(
            task_id,
            'done',
            result=worker['result'],
            model=worker['model'],
            latency_ms=int(worker.get('latency_ms') or 0),
        )
        print(f"done {task_id} {row['task_type']} {worker.get('latency_ms')}ms", flush=True)
    except Exception as exc:
        update_task(task_id, 'fallback', route='gpt', error=str(exc))
        print(f"fallback {task_id} {row['task_type']}: {exc}", flush=True)


def main_loop():
    print('local task worker started', flush=True)
    while True:
        rows = fetch_queued()
        if not rows:
            time.sleep(POLL_SECONDS)
            continue
        for row in rows:
            process(row)


if __name__ == '__main__':
    main_loop()
