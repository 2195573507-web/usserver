#!/usr/bin/env python3
"""
Server observability indexer.

Read-only source scanning; writes normalized metadata into SQLite.
Does not replace original files or change service runtime paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path('/opt/server-observability-index/observability.sqlite')
ROOTS_FOR_FILE_INVENTORY = [
    Path('/opt'),
    Path('/root/.hermes'),
    Path('/root/.openclaw'),
    Path('/root/obsidian-vault'),
    Path('/root/server-organization'),
]
SKIP_DIR_NAMES = {
    'node_modules', '.git', '.venv', 'venv', '__pycache__', '.cache', 'dist', 'build', '.pnpm',
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_parent()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS index_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sub2api_log_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_inode INTEGER NOT NULL,
            line_no INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            line_hash TEXT NOT NULL UNIQUE,
            ts_text TEXT,
            level TEXT,
            method TEXT,
            route TEXT,
            status_code INTEGER,
            latency_ms REAL,
            upstream TEXT,
            message TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sub2api_log_ts ON sub2api_log_index(ts_text);
        CREATE INDEX IF NOT EXISTS idx_sub2api_log_status ON sub2api_log_index(status_code);
        CREATE INDEX IF NOT EXISTS idx_sub2api_log_level ON sub2api_log_index(level);

        CREATE TABLE IF NOT EXISTS hermes_request_index (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            sha256 TEXT NOT NULL,
            created_hint TEXT,
            provider TEXT,
            model TEXT,
            status TEXT,
            error_hint TEXT,
            message_count INTEGER,
            top_keys TEXT,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hermes_request_mtime ON hermes_request_index(mtime);
        CREATE INDEX IF NOT EXISTS idx_hermes_request_provider ON hermes_request_index(provider);
        CREATE INDEX IF NOT EXISTS idx_hermes_request_status ON hermes_request_index(status);

        CREATE TABLE IF NOT EXISTS openclaw_session_index (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            path TEXT,
            created_at TEXT,
            updated_at TEXT,
            message_count INTEGER,
            status TEXT,
            summary TEXT,
            raw_keys TEXT,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_openclaw_session_updated ON openclaw_session_index(updated_at);

        CREATE TABLE IF NOT EXISTS server_file_inventory (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            sha256 TEXT,
            category TEXT NOT NULL,
            ext TEXT,
            source_root TEXT NOT NULL,
            is_runtime INTEGER NOT NULL DEFAULT 0,
            is_db INTEGER NOT NULL DEFAULT 0,
            is_report INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_file_inventory_category ON server_file_inventory(category);
        CREATE INDEX IF NOT EXISTS idx_file_inventory_size ON server_file_inventory(size);
        CREATE INDEX IF NOT EXISTS idx_file_inventory_mtime ON server_file_inventory(mtime);

        CREATE TABLE IF NOT EXISTS memory_maintenance_report_index (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            agent TEXT,
            report_date TEXT,
            hour TEXT,
            health_status TEXT,
            memories INTEGER,
            embeddings INTEGER,
            candidates_count INTEGER,
            needs_human INTEGER NOT NULL DEFAULT 0,
            title TEXT,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_report_date ON memory_maintenance_report_index(report_date, hour);
        CREATE INDEX IF NOT EXISTS idx_memory_report_agent ON memory_maintenance_report_index(agent);
        '''
    )
    conn.commit()


def sha256_file(path: Path, max_bytes: int | None = None) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        remaining = max_bytes
        while True:
            if remaining is None:
                chunk = f.read(1024 * 1024)
            else:
                if remaining <= 0:
                    break
                chunk = f.read(min(1024 * 1024, remaining))
                remaining -= len(chunk)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: Path, include_all: bool = False) -> Iterable[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if include_all or d not in SKIP_DIR_NAMES]
        for name in filenames:
            yield Path(dirpath) / name


def classify_file(path: Path) -> tuple[str, int, int, int]:
    p = str(path)
    ext = path.suffix.lower()
    is_db = 1 if ext in {'.db', '.sqlite', '.sqlite3'} or p.endswith(('-wal', '-shm')) else 0
    is_report = 1 if ('记忆治理' in p or 'server-organization' in p) and ext in {'.md', '.txt', '.tsv'} else 0
    is_runtime = 1 if any(s in p for s in ['/root/.hermes/', '/root/.openclaw/', '/www/docker', '/var/lib/docker']) else 0
    if is_db:
        cat = 'database'
    elif ext in {'.log'}:
        cat = 'log'
    elif ext in {'.json', '.yaml', '.yml', '.toml'}:
        cat = 'config_or_state'
    elif ext in {'.md', '.txt', '.tsv'}:
        cat = 'document_or_report'
    elif ext in {'.py', '.sh', '.js', '.ts'}:
        cat = 'script_or_code'
    else:
        cat = 'other'
    return cat, is_runtime, is_db, is_report


def index_sub2api_logs(conn: sqlite3.Connection) -> int:
    log_path = Path('/opt/sub2api/data/logs/sub2api.log')
    if not log_path.exists():
        return 0
    st = log_path.stat()
    inserted = 0
    ts_patterns = [
        re.compile(r'(?P<ts>\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)'),
        re.compile(r'(?P<ts>\d{2}:\d{2}:\d{2})'),
    ]
    http_re = re.compile(r'(?P<method>GET|POST|PUT|PATCH|DELETE|OPTIONS)\s+(?P<route>/[^\s"?]*)')
    status_re = re.compile(r'(?i)\b(?:status|status_code|code|http_status)[=: ]+(?P<status>[1-5]\d{2})\b')
    http_access_status_re = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|OPTIONS)\s+[^\"]+"\s+(?P<status>[1-5]\d{2})\b')
    latency_re = re.compile(r'(?P<lat>\d+(?:\.\d+)?)\s*(?:ms|毫秒)')
    level_re = re.compile(r'\b(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|FATAL|TRACE)\b', re.I)
    upstream_re = re.compile(r'\b(?:upstream|channel|provider|model)[:= ]+(?P<up>[A-Za-z0-9_.:/-]+)', re.I)
    with log_path.open('rb') as f:
        offset = 0
        for line_no, raw in enumerate(f, 1):
            current_offset = offset
            offset += len(raw)
            try:
                line = raw.decode('utf-8', 'replace').rstrip('\n')
            except Exception:
                line = repr(raw[:500])
            if not line.strip():
                continue
            line_hash = hashlib.sha256(f'{st.st_ino}:{line_no}:{line}'.encode()).hexdigest()
            ts_text = None
            for pat in ts_patterns:
                m = pat.search(line)
                if m:
                    ts_text = m.group('ts')
                    break
            method = route = level = upstream = None
            status_code = None
            latency_ms = None
            m = http_re.search(line)
            if m:
                method, route = m.group('method'), m.group('route')
            m = http_access_status_re.search(line)
            if not m:
                m = status_re.search(line)
            if m:
                try: status_code = int(m.group('status'))
                except Exception: pass
            m = latency_re.search(line)
            if m:
                try: latency_ms = float(m.group('lat'))
                except Exception: pass
            m = level_re.search(line)
            if m:
                level = m.group('level').upper()
            m = upstream_re.search(line)
            if m:
                upstream = m.group('up')[:200]
            conn.execute(
                '''INSERT OR IGNORE INTO sub2api_log_index
                (file_path,file_inode,line_no,byte_offset,line_hash,ts_text,level,method,route,status_code,latency_ms,upstream,message,indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (str(log_path), st.st_ino, line_no, current_offset, line_hash, ts_text, level, method, route, status_code, latency_ms, upstream, line[:2000], utc_now())
            )
            if conn.total_changes:
                inserted += 1
    conn.commit()
    return inserted


def extract_provider_model(obj: Any) -> tuple[str | None, str | None, str | None, str | None, int | None, str]:
    provider = model = status = error_hint = None
    message_count = None
    keys = []
    if isinstance(obj, dict):
        keys = list(obj.keys())[:50]
        text = json.dumps(obj, ensure_ascii=False)[:20000]
        for k in ('provider', 'Provider'):
            if isinstance(obj.get(k), str): provider = obj[k]
        for k in ('model', 'Model'):
            if isinstance(obj.get(k), str): model = obj[k]
        # fuzzy from serialized payload
        if not model:
            m = re.search(r'"model"\s*:\s*"([^"]+)"', text)
            if m: model = m.group(1)
        if not provider:
            m = re.search(r'"provider"\s*:\s*"([^"]+)"', text)
            if m: provider = m.group(1)
        if 'error' in text.lower():
            status = 'error'
            m = re.search(r'(?i)(error[^,}\n]{0,300})', text)
            if m: error_hint = m.group(1)[:300]
        else:
            status = 'unknown'
        if isinstance(obj.get('messages'), list):
            message_count = len(obj['messages'])
        else:
            m = re.findall(r'"role"\s*:', text)
            if m: message_count = len(m)
    return provider, model, status, error_hint, message_count, ','.join(keys)


def index_hermes_requests(conn: sqlite3.Connection) -> int:
    base = Path('/root/.hermes/sessions')
    count = 0
    if not base.exists(): return 0
    for path in base.glob('request_dump_*.json'):
        try:
            st = path.stat()
            content = path.read_text(errors='replace')
            try: obj = json.loads(content)
            except Exception: obj = None
            provider, model, status, error_hint, msg_count, top_keys = extract_provider_model(obj)
            sha = sha256_file(path)
            created_hint = None
            m = re.search(r'request_dump_(\d{8})_(\d{6})', path.name)
            if m:
                created_hint = f'{m.group(1)}T{m.group(2)}'
            conn.execute(
                '''INSERT INTO hermes_request_index
                (path,size,mtime,sha256,created_hint,provider,model,status,error_hint,message_count,top_keys,indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,sha256=excluded.sha256,
                created_hint=excluded.created_hint,provider=excluded.provider,model=excluded.model,status=excluded.status,
                error_hint=excluded.error_hint,message_count=excluded.message_count,top_keys=excluded.top_keys,indexed_at=excluded.indexed_at''',
                (str(path), st.st_size, st.st_mtime, sha, created_hint, provider, model, status, error_hint, msg_count, top_keys, utc_now())
            )
            count += 1
        except Exception as e:
            print(f'[hermes_request] skip {path}: {e}')
    conn.commit()
    return count


def index_openclaw_sessions(conn: sqlite3.Connection) -> int:
    path = Path('/root/.openclaw/agents/main/sessions/sessions.json')
    if not path.exists(): return 0
    try:
        data = json.loads(path.read_text(errors='replace'))
    except Exception as e:
        print(f'[openclaw_sessions] parse failed: {e}')
        return 0
    sessions = []
    if isinstance(data, list):
        sessions = data
    elif isinstance(data, dict):
        for key in ('sessions', 'items', 'data'):
            if isinstance(data.get(key), list):
                sessions = data[key]
                break
        if not sessions:
            # maybe mapping id->session
            sessions = [dict(v, id=k) if isinstance(v, dict) else {'id': k, 'value': v} for k, v in data.items()]
    count = 0
    for i, s in enumerate(sessions):
        if not isinstance(s, dict):
            continue
        sid = str(s.get('id') or s.get('session_id') or s.get('sessionId') or s.get('uuid') or f'idx-{i}')
        title = s.get('title') or s.get('name') or s.get('summary')
        created = s.get('created_at') or s.get('createdAt') or s.get('created')
        updated = s.get('updated_at') or s.get('updatedAt') or s.get('mtime') or s.get('lastActiveAt')
        msg_count = None
        for k in ('message_count', 'messageCount', 'messages'):
            v = s.get(k)
            if isinstance(v, int): msg_count = v
            elif isinstance(v, list): msg_count = len(v)
        status = s.get('status') or s.get('state')
        summary = s.get('summary') or s.get('description')
        raw_keys = ','.join(list(s.keys())[:50])
        conn.execute(
            '''INSERT INTO openclaw_session_index
            (session_id,title,path,created_at,updated_at,message_count,status,summary,raw_keys,indexed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET title=excluded.title,path=excluded.path,created_at=excluded.created_at,
            updated_at=excluded.updated_at,message_count=excluded.message_count,status=excluded.status,summary=excluded.summary,
            raw_keys=excluded.raw_keys,indexed_at=excluded.indexed_at''',
            (sid, str(title)[:500] if title else None, str(path), str(created) if created else None, str(updated) if updated else None,
             msg_count, str(status)[:100] if status else None, str(summary)[:2000] if summary else None, raw_keys, utc_now())
        )
        count += 1
    conn.commit()
    return count


def index_file_inventory(conn: sqlite3.Connection) -> int:
    count = 0
    now = utc_now()
    for root in ROOTS_FOR_FILE_INVENTORY:
        if not root.exists(): continue
        include_all = root in {Path('/root/obsidian-vault'), Path('/root/server-organization')}
        for path in iter_files(root, include_all=include_all):
            try:
                st = path.stat()
                if st.st_size > 50 * 1024 * 1024:
                    sha = None
                elif st.st_size <= 2 * 1024 * 1024:
                    sha = sha256_file(path)
                else:
                    sha = sha256_file(path, max_bytes=1024 * 1024) + ':partial1m'
                cat, is_runtime, is_db, is_report = classify_file(path)
                conn.execute(
                    '''INSERT INTO server_file_inventory
                    (path,size,mtime,sha256,category,ext,source_root,is_runtime,is_db,is_report,indexed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,sha256=excluded.sha256,
                    category=excluded.category,ext=excluded.ext,source_root=excluded.source_root,is_runtime=excluded.is_runtime,
                    is_db=excluded.is_db,is_report=excluded.is_report,indexed_at=excluded.indexed_at''',
                    (str(path), st.st_size, st.st_mtime, sha, cat, path.suffix.lower(), str(root), is_runtime, is_db, is_report, now)
                )
                count += 1
            except Exception as e:
                print(f'[file_inventory] skip {path}: {e}')
    conn.commit()
    return count


def index_memory_reports(conn: sqlite3.Connection) -> int:
    base = Path('/root/obsidian-vault/记忆治理')
    if not base.exists(): return 0
    count = 0
    for path in base.glob('*.md'):
        try:
            st = path.stat()
            text = path.read_text(errors='replace')
            title = None
            for line in text.splitlines():
                if line.startswith('#'):
                    title = line.strip('# ').strip()
                    break
            agent = None
            name = path.name
            if 'Hermes' in name: agent = 'hermes'
            elif 'OpenClaw' in name: agent = 'openclaw'
            elif '记忆' in name: agent = 'unknown'
            m = re.search(r'(\d{4}-\d{2}-\d{2}).*?(\d{2})?', name)
            report_date = m.group(1) if m else None
            hour = None
            hm = re.search(r'-(\d{2})\.md$', name)
            if hm: hour = hm.group(1)
            health = None
            for pat in [r'health[：: ]+([A-Za-z0-9_-]+)', r'健康状态[：: ]+([^\n]+)']:
                mm = re.search(pat, text, re.I)
                if mm:
                    health = mm.group(1).strip()[:100]
                    break
            memories = embeddings = None
            mm = re.search(r'memories[：: ]+(\d+)', text, re.I)
            if mm: memories = int(mm.group(1))
            mm = re.search(r'embeddings[：: ]+(\d+)', text, re.I)
            if mm: embeddings = int(mm.group(1))
            candidates_count = len(re.findall(r'候选|冲突|重复|过期|异常|人工', text))
            needs_human = 1 if re.search(r'需要人工|人工确认|异常|冲突|重复|过期', text) else 0
            conn.execute(
                '''INSERT INTO memory_maintenance_report_index
                (path,size,mtime,agent,report_date,hour,health_status,memories,embeddings,candidates_count,needs_human,title,indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,agent=excluded.agent,
                report_date=excluded.report_date,hour=excluded.hour,health_status=excluded.health_status,memories=excluded.memories,
                embeddings=excluded.embeddings,candidates_count=excluded.candidates_count,needs_human=excluded.needs_human,
                title=excluded.title,indexed_at=excluded.indexed_at''',
                (str(path), st.st_size, st.st_mtime, agent, report_date, hour, health, memories, embeddings,
                 candidates_count, needs_human, title, utc_now())
            )
            count += 1
        except Exception as e:
            print(f'[memory_reports] skip {path}: {e}')
    conn.commit()
    return count


def summarize(conn: sqlite3.Connection) -> str:
    tables = ['sub2api_log_index','hermes_request_index','openclaw_session_index','server_file_inventory','memory_maintenance_report_index']
    lines = []
    for t in tables:
        n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        lines.append(f'{t}: {n}')
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='all', choices=['all','logs','requests','sessions','files','memory-reports'])
    args = parser.parse_args()
    conn = connect()
    init_db(conn)
    started = utc_now()
    cur = conn.execute('INSERT INTO index_runs(started_at,mode,status) VALUES (?,?,?)', (started, args.mode, 'running'))
    run_id = cur.lastrowid
    notes = []
    status = 'ok'
    try:
        if args.mode in ('all','logs'):
            notes.append(f'sub2api_log_inserted={index_sub2api_logs(conn)}')
        if args.mode in ('all','requests'):
            notes.append(f'hermes_requests={index_hermes_requests(conn)}')
        if args.mode in ('all','sessions'):
            notes.append(f'openclaw_sessions={index_openclaw_sessions(conn)}')
        if args.mode in ('all','files'):
            notes.append(f'file_inventory={index_file_inventory(conn)}')
        if args.mode in ('all','memory-reports'):
            notes.append(f'memory_reports={index_memory_reports(conn)}')
        conn.execute('PRAGMA optimize')
    except Exception as e:
        status = 'error'
        notes.append(f'error={e!r}')
        raise
    finally:
        conn.execute('UPDATE index_runs SET finished_at=?, status=?, notes=? WHERE id=?', (utc_now(), status, '; '.join(notes), run_id))
        conn.commit()
    print('Index run:', '; '.join(notes))
    print(summarize(conn))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
