#!/usr/bin/env python3
"""Shared memory governance without OpenClaw-side MEMORY.md changes.

Modes:
- decay: mark low-importance old active memories as noise candidates in a report.
- audit: report duplicates, contradictions, disabled counts, and action candidates.

This script is intentionally conservative: it writes reports and exports markdown,
but does not delete memories or edit OpenClaw workspace files.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path('/opt/shared-agent-memory/data/memory.sqlite')
REPORT_DIR = Path('/root/obsidian-vault/记忆治理')
EXPORT = Path('/root/obsidian-vault/shared-memory/exports/shared-memory.md')


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def tags(row: sqlite3.Row) -> list[str]:
    try:
        data = json.loads(row['tags_json'] or '[]')
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_report(name: str, body: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = now_utc().strftime('%Y-%m-%d')
    path = REPORT_DIR / f'{date}-{name}.md'
    path.write_text(body, encoding='utf-8')
    return path


def load_active(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute('''
        SELECT * FROM memories
        WHERE disabled = 0
        ORDER BY importance DESC, updated_at DESC
    ''').fetchall()


def mode_decay() -> Path:
    with connect() as con:
        rows = load_active(con)
        candidates = [r for r in rows if int(r['importance']) < 5]
        active_temporary = [r for r in rows if r['kind'] == 'temporary']
        noise = [r for r in rows if r['kind'] == 'noise']

    lines = [
        '---',
        'title: 共享记忆衰减审查',
        'tags: [memory, governance, decay]',
        f'updated: {now_utc().date()}',
        '---',
        '',
        '# 共享记忆衰减审查',
        '',
        '> 只生成候选报告，不自动删除、不自动合并重要记忆。',
        '',
        '## 统计',
        '',
        f'- 活跃记忆：{len(rows)}',
        f'- importance < 5 候选：{len(candidates)}',
        f'- active temporary：{len(active_temporary)}',
        f'- active noise：{len(noise)}',
        '',
        '## 候选条目',
        '',
    ]
    if not candidates:
        lines.append('- 暂无 importance < 5 的活跃候选。')
    for r in candidates[:50]:
        lines.append(f"- `{r['id']}` [{r['kind']}/{r['scope']}] importance={r['importance']} tags={', '.join(tags(r))}: {r['content'][:160]}")
    lines += ['', '## 相关', '', '- [[shared-memory/exports/shared-memory|共享记忆导出]]', '- [[MOC|知识库导航]]', '']
    return write_report('共享记忆衰减审查', '\n'.join(lines))


def mode_audit() -> Path:
    with connect() as con:
        rows = load_active(con)
        disabled = con.execute('SELECT count(*) FROM memories WHERE disabled=1').fetchone()[0]

    by_subject: dict[str, list[sqlite3.Row]] = defaultdict(list)
    by_tag: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r['subject']:
            by_subject[r['subject'].strip().lower()].append(r)
        for t in tags(r):
            by_tag[t].append(r)

    duplicate_subjects = {k: v for k, v in by_subject.items() if len(v) > 1}
    qwen_active = [r for r in rows if any(t in ('qwen', '千问') for t in tags(r))]
    temporary = [r for r in rows if r['kind'] == 'temporary']
    no_tags = [r for r in rows if not tags(r)]

    lines = [
        '---',
        'title: 共享记忆季度审计',
        'tags: [memory, governance, audit]',
        f'updated: {now_utc().date()}',
        '---',
        '',
        '# 共享记忆季度审计',
        '',
        '> 只生成审计报告和候选项，不自动删除、不绕过人工确认。',
        '',
        '## 总览',
        '',
        f'- 活跃记忆：{len(rows)}',
        f'- 已禁用记忆：{disabled}',
        f'- active temporary：{len(temporary)}',
        f'- 无 tags 活跃记忆：{len(no_tags)}',
        f'- qwen/千问活跃记忆：{len(qwen_active)}（应仅保留当前禁用/移除决策）',
        f'- subject 重复组：{len(duplicate_subjects)}',
        '',
        '## 需要关注',
        '',
    ]
    if temporary:
        lines.append('### Active temporary')
        for r in temporary:
            lines.append(f"- `{r['id']}` {r['content'][:160]}")
        lines.append('')
    if no_tags:
        lines.append('### 无 tags 条目')
        for r in no_tags[:50]:
            lines.append(f"- `{r['id']}` [{r['kind']}/{r['scope']}] importance={r['importance']}: {r['content'][:160]}")
        lines.append('')
    if duplicate_subjects:
        lines.append('### Subject 重复候选')
        for subject, group in list(duplicate_subjects.items())[:30]:
            lines.append(f'- `{subject}` × {len(group)}')
            for r in group[:5]:
                lines.append(f"  - `{r['id']}` [{r['kind']}] importance={r['importance']}")
        lines.append('')
    if qwen_active:
        lines.append('### Qwen/千问相关活跃条目')
        for r in qwen_active:
            lines.append(f"- `{r['id']}` [{r['kind']}] importance={r['importance']}: {r['content'][:180]}")
        lines.append('')
    lines += ['', '## 相关', '', '- [[shared-memory/exports/shared-memory|共享记忆导出]]', '- [[agents/hermes/MEMORY|Hermes Working Memory]]', '- [[MOC|知识库导航]]', '']
    return write_report('共享记忆季度审计', '\n'.join(lines))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else 'audit'
    if mode == 'decay':
        path = mode_decay()
    elif mode == 'audit':
        path = mode_audit()
    else:
        print('usage: shared_memory_governance.py [decay|audit]', file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
