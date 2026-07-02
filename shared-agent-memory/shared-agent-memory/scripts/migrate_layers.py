#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path('/opt/shared-agent-memory/data/memory.sqlite')
CANONICAL = {'preference', 'fact', 'service_state', 'decision', 'success', 'failure', 'temporary', 'todo', 'noise'}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def classify(row: sqlite3.Row) -> tuple[str, list[str], int | None, str]:
    old = row['kind']
    content = (row['content'] or '').lower()
    subject = (row['subject'] or '').lower()
    tags = json.loads(row['tags_json'] or '[]')
    text = content + ' ' + subject + ' ' + ' '.join(str(t).lower() for t in tags)

    if old in {'preference', 'fact', 'service_state', 'decision', 'success', 'failure', 'temporary', 'todo', 'noise'}:
        return old, tags, None, 'already canonical'

    if old == 'user_profile' or any(x in text for x in ['用户偏好', 'prefers', '喜欢', '不要', '偏好']):
        return 'preference', tags + ['migration'], None, 'profile/preference signal'

    if old == 'instruction':
        return 'decision', tags + ['migration'], None, 'instruction treated as decision'

    if old == 'system':
        return 'service_state', tags + ['migration'], None, 'system treated as service_state'

    if any(x in text for x in ['失败', 'failed', '拦截', '不能', '报错', 'error', 'wrong', 'blocked']) and any(x in text for x in ['不要', '不是', '不能', '失败', 'failed', 'wrong']):
        return 'failure', tags + ['migration'], None, 'failure lesson signal'

    if any(x in text for x in ['成功', '已验证', '验证', '通过', '修复', 'works', 'ok']) and any(x in text for x in ['修复', '验证', '通过', 'works', '端到端']):
        return 'success', tags + ['migration'], None, 'success/reusable signal'

    if old == 'event':
        if len(row['content'] or '') < 80:
            return 'noise', tags + ['migration'], 7, 'short event likely noise'
        return 'temporary', tags + ['migration'], 30, 'event migrated to temporary'

    if any(x in text for x in ['systemd', 'service', '服务', '端口', 'port', 'config', '配置', '路径', '/opt/', ':9000', ':9100']):
        return 'service_state', tags + ['migration'], None, 'service/config signal'

    return 'fact', tags + ['migration'], None, 'fallback fact'


def rebuild_fts(con: sqlite3.Connection) -> None:
    con.execute('DELETE FROM memories_fts')
    for row in con.execute('SELECT * FROM memories'):
        con.execute(
            'INSERT INTO memories_fts(id,content,subject,tags,scope,kind,source_agent) VALUES (?,?,?,?,?,?,?)',
            (row['id'], row['content'], row['subject'], ' '.join(json.loads(row['tags_json'] or '[]')), row['scope'], row['kind'], row['source_agent']),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description='Migrate shared-agent-memory kind values to canonical layers')
    parser.add_argument('--apply', action='store_true', help='write changes')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()

    with connect() as con:
        rows = con.execute('SELECT * FROM memories WHERE disabled=0 ORDER BY updated_at DESC').fetchall()
        changes = []
        for row in rows:
            new_kind, new_tags, ttl_days, reason = classify(row)
            old_kind = row['kind']
            if new_kind == old_kind and old_kind in CANONICAL:
                continue
            metadata = json.loads(row['metadata_json'] or '{}')
            metadata.setdefault('original_kind', old_kind)
            metadata.setdefault('layer_migration_reason', reason)
            expires_at = row['expires_at']
            if ttl_days and not expires_at:
                expires_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + ttl_days * 86400, tz=timezone.utc).isoformat(timespec='seconds')
            changes.append({
                'id': row['id'],
                'subject': row['subject'],
                'old_kind': old_kind,
                'new_kind': new_kind,
                'reason': reason,
                'content': (row['content'] or '')[:120],
                'tags': sorted(set(new_tags)),
                'metadata': metadata,
                'expires_at': expires_at,
            })
            if args.limit and len(changes) >= args.limit:
                break

        print(json.dumps({'apply': args.apply, 'change_count': len(changes), 'changes': changes}, ensure_ascii=False, indent=2))
        if not args.apply:
            return 0
        stamp = now()
        for change in changes:
            con.execute(
                'UPDATE memories SET kind=?, tags_json=?, metadata_json=?, expires_at=?, updated_at=? WHERE id=?',
                (
                    change['new_kind'],
                    json.dumps(change['tags'], ensure_ascii=False),
                    json.dumps(change['metadata'], ensure_ascii=False),
                    change['expires_at'],
                    stamp,
                    change['id'],
                ),
            )
        rebuild_fts(con)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
