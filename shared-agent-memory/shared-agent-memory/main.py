from __future__ import annotations

import json
import math
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

BASE_DIR = Path('/opt/shared-agent-memory')
DATA_DIR = BASE_DIR / 'data'
DB_PATH = DATA_DIR / 'memory.sqlite'
VAULT_DIR = Path('/root/obsidian-vault/shared-memory')
EXPORT_DIR = VAULT_DIR / 'exports'
OLLAMA_BASE = 'http://[REDACTED_IP]:11434'
CLASSIFIER_MODEL = None  # qwen removed 2026-06-24
EMBED_MODEL = 'nomic-embed-text'

CANONICAL_KINDS = [
    'preference',
    'fact',
    'service_state',
    'decision',
    'success',
    'failure',
    'temporary',
    'todo',
    'noise',
]
DEFAULT_CONTEXT_KINDS = ['preference', 'fact', 'service_state', 'decision', 'success']
DEBUG_CONTEXT_KINDS = DEFAULT_CONTEXT_KINDS + ['failure']
PLANNING_CONTEXT_KINDS = DEFAULT_CONTEXT_KINDS + ['todo']
LAYER_TITLES = {
    'preference': 'Preferences',
    'fact': 'Facts',
    'service_state': 'Service State',
    'decision': 'Decisions',
    'success': 'Success Patterns',
    'failure': 'Failure Lessons',
    'temporary': 'Temporary',
    'todo': 'Todo',
    'noise': 'Noise Archive',
}

DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='Shared Agent Memory', version='1.3.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])


class MemoryIn(BaseModel):
    content: str = Field(min_length=1)
    scope: str = 'shared'
    kind: str = 'fact'
    source_agent: str = 'unknown'
    subject: str = ''
    confidence: float = 0.8
    importance: int = 5
    tags: list[str] = []
    metadata: dict[str, Any] = {}
    ttl_days: Optional[int] = None


class ClassifyIn(BaseModel):
    content: str = Field(min_length=1)
    source_agent: str = 'unknown'
    scope: str = 'shared'
    subject_hint: str = ''
    tags_hint: list[str] = []


class AutoMemoryIn(BaseModel):
    content: str = Field(min_length=1)
    source_agent: str = 'unknown'
    scope: str = 'shared'
    subject_hint: str = ''
    tags_hint: list[str] = []
    force: bool = False


class LocalTaskIn(BaseModel):
    task_type: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_agent: str = 'unknown'
    sync: bool = True
    timeout_seconds: int = 10
    metadata: dict[str, Any] = {}


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    disabled: Optional[bool] = None
    confidence: Optional[float] = None
    importance: Optional[int] = None
    tags: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None


class EventIn(BaseModel):
    summary: str = Field(min_length=1)
    actor: str = 'unknown'
    project: str = ''
    result: str = ''
    files: list[str] = []
    services: list[str] = []
    metadata: dict[str, Any] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript('''
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    importance INTEGER NOT NULL DEFAULT 5,
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    disabled INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED, content, subject, tags, scope, kind, source_agent,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]',
    services_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS local_tasks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    route TEXT NOT NULL,
    content TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    id UNINDEXED, summary, result, project, actor,
    tokenize='unicode61 remove_diacritics 2'
);
''')


def is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        value = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value < now_dt()
    except Exception:
        return False


def parse_kinds(kind: str = '', kinds: str = '') -> list[str]:
    values: list[str] = []
    if kind:
        values.append(kind)
    if kinds:
        values.extend(k.strip() for k in kinds.split(',') if k.strip())
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def context_kinds(mode: str, kind: str = '', kinds: str = '', include_failure: bool = False, include_temporary: bool = False, include_noise: bool = False) -> list[str]:
    explicit = parse_kinds(kind, kinds)
    if explicit:
        return explicit
    if mode == 'debug':
        selected = list(DEBUG_CONTEXT_KINDS)
    elif mode == 'planning':
        selected = list(PLANNING_CONTEXT_KINDS)
    elif mode == 'all':
        selected = [k for k in CANONICAL_KINDS if include_noise or k != 'noise']
    else:
        selected = list(DEFAULT_CONTEXT_KINDS)
    if include_failure and 'failure' not in selected:
        selected.append('failure')
    if include_temporary and 'temporary' not in selected:
        selected.append('temporary')
    if include_noise and 'noise' not in selected:
        selected.append('noise')
    return selected


def row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item['tags'] = json.loads(item.pop('tags_json') or '[]')
    item['metadata'] = json.loads(item.pop('metadata_json') or '{}')
    item.pop('vector_json', None)
    item['disabled'] = bool(item['disabled'])
    item['expired'] = is_expired(item.get('expires_at'))
    item['canonical'] = item.get('kind') in CANONICAL_KINDS
    return item


def normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        raw = [part.strip() for part in tags.replace('，', ',').split(',')]
    elif isinstance(tags, list):
        raw = [str(part).strip() for part in tags]
    else:
        raw = []
    result, seen = [], set()
    for tag in raw:
        tag = tag.lower().replace(' ', '-')[:40]
        if tag and tag not in seen:
            result.append(tag)
            seen.add(tag)
    return result[:12]


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, number))


def clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return max(minimum, min(maximum, number))


def ollama_json(path: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    req = urllib.request.Request(
        OLLAMA_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def heuristic_classify(content: str, source_agent: str, scope: str, subject_hint: str, tags_hint: list[str]) -> dict[str, Any]:
    text = content.strip()
    lower = text.lower()
    kind = 'fact'
    if any(word in lower for word in ['偏好', '喜欢', 'prefers', '不要反复', '希望']):
        kind = 'preference'
    elif any(word in lower for word in ['todo', '待办', '下一步', '需要做']):
        kind = 'todo'
    elif any(word in lower for word in ['失败', 'failed', '报错', '不可用', 'blocked']):
        kind = 'failure'
    elif any(word in lower for word in ['成功', 'success', '验证通过', '已完成', '可复用']):
        kind = 'success'
    elif any(word in lower for word in ['决定', 'decision', '采用', '保持', '不要更换']):
        kind = 'decision'
    elif any(word in lower for word in ['service', 'systemd', 'port', '端口', '配置', 'openclaw', 'hermes', 'dashboard']):
        kind = 'service_state'
    elif len(text) < 20:
        kind = 'noise'
    return {
        'should_save': kind != 'noise',
        'kind': kind,
        'scope': scope,
        'subject': subject_hint.strip()[:100] or text.split('\n', 1)[0][:80],
        'summary': text[:800],
        'tags': normalize_tags(tags_hint + [source_agent, kind]),
        'importance': 8 if kind in ('preference', 'service_state', 'decision', 'success', 'failure') else 5,
        'confidence': 0.55,
        'sensitivity': 'shared',
        'ttl_days': 7 if kind in ('temporary', 'noise') else None,
        'reason': 'heuristic fallback',
    }


def classify_memory_content(content: str, source_agent: str = 'unknown', scope: str = 'shared', subject_hint: str = '', tags_hint: list[str] | None = None) -> dict[str, Any]:
    tags_hint = tags_hint or []
    prompt = (
        '你是本机长期记忆分类器。只输出严格 JSON，不要解释。\n'
        f'可选 kind: {", ".join(CANONICAL_KINDS)}。\n'
        '判断内容是否值得保存，并输出字段：should_save(bool), kind, scope, subject, summary, tags(array), importance(1-10), confidence(0-1), sensitivity(public/shared/private/secret), ttl_days(null或整数), reason。\n'
        '任何 API key/token/password/secret/credential/连接串都必须 sensitivity=secret 且 should_save=false。\n'
        f'source_agent={source_agent}; scope={scope}; subject_hint={subject_hint}; tags_hint={json.dumps(tags_hint, ensure_ascii=False)}\n'
        f'内容：\n{content[:4000]}\n'
    )
    try:
        data = ollama_json('/api/generate', {'model': CLASSIFIER_MODEL, 'prompt': prompt, 'stream': False, 'format': 'json'}, timeout=45)
        parsed = extract_json_object(data.get('response', '{}'))
    except Exception as exc:
        parsed = heuristic_classify(content, source_agent, scope, subject_hint, tags_hint)
        parsed['error'] = f'qwen_classify_failed: {exc}'
    parsed['kind'] = parsed.get('kind') if parsed.get('kind') in CANONICAL_KINDS else 'fact'
    parsed['scope'] = str(parsed.get('scope') or scope or 'shared')[:40]
    parsed['subject'] = str(parsed.get('subject') or subject_hint or content.strip().split('\n', 1)[0])[:100]
    parsed['summary'] = str(parsed.get('summary') or content.strip())[:1200]
    parsed['tags'] = normalize_tags(tags_hint + normalize_tags(parsed.get('tags', [])))
    parsed['importance'] = clamp_int(parsed.get('importance'), 5, 1, 10)
    parsed['confidence'] = clamp_float(parsed.get('confidence'), 0.6, 0.0, 1.0)
    parsed['sensitivity'] = str(parsed.get('sensitivity') or 'shared').lower()
    if parsed['sensitivity'] not in ('public', 'shared', 'private', 'secret'):
        parsed['sensitivity'] = 'shared'
    ttl = parsed.get('ttl_days')
    parsed['ttl_days'] = None if ttl in (None, '', 'null') else clamp_int(ttl, 7, 1, 365)
    parsed['should_save'] = bool(parsed.get('should_save')) and parsed['sensitivity'] != 'secret'
    return parsed


LIGHT_TASK_TYPES = {
    'summary', 'recap', 'short-ack', 'memory-classify', 'memory-summary', 'completion-postprocess',
    'compaction-summary', 'cron-summary', 'subagent-summary', 'log-summary', 'report-draft',
    'polish', 'translate', 'extract-points', 'status-summary',
}
HIGH_RISK_PATTERNS = [
    '代码', 'code', '脚本', '执行', '运行命令', '命令', '查服务器', '服务器', '修改配置', '配置变更',
    '重启', 'systemctl', 'docker', 'git', 'patch', 'diff', '排障', '故障', '诊断', '多工具',
    '联网', '搜索', '安全', '隐私', '密码', '密钥', 'token', 'api key', '删除', '清除',
    '重要决策', '架构', '长上下文', '数据库', '迁移', '权限',
]
ASYNC_LOCAL_TASK_TYPES = {
    'memory-summary', 'completion-postprocess', 'compaction-summary', 'cron-summary',
    'subagent-summary', 'log-summary', 'report-draft', 'polish',
}


def classify_local_task(task_type: str, content: str) -> dict[str, Any]:
    normalized = task_type.strip().lower().replace('_', '-').replace(' ', '-')
    lower = content.lower()
    if normalized not in LIGHT_TASK_TYPES:
        return {'route': 'gpt', 'reason': 'task_type_not_light', 'task_type': normalized}
    if len(content) > 6000:
        return {'route': 'gpt', 'reason': 'content_too_long', 'task_type': normalized}
    if any(pattern.lower() in lower for pattern in HIGH_RISK_PATTERNS):
        return {'route': 'gpt', 'reason': 'high_risk_keyword', 'task_type': normalized}
    return {'route': 'qwen', 'reason': 'low_risk_light_task', 'task_type': normalized}


def local_task_prompt(task_type: str, content: str) -> str:
    instructions = {
        'summary': '用一句中文总结。',
        'recap': '用两句中文回顾。',
        'short-ack': '用一句中文确认。',
        'memory-classify': '输出 kind 和一句摘要。',
        'memory-summary': '整理成一条短记忆摘要，标出主题和价值。',
        'completion-postprocess': '整理 agent 完成后的低风险文本结果，输出三点以内。',
        'compaction-summary': '压缩会话摘要，保留用户偏好、决策、未完成事项。',
        'cron-summary': '整理定时任务输出，提取结果、异常、后续建议。',
        'subagent-summary': '整理子 agent 汇总，提取结论和可复用信息。',
        'log-summary': '用三点概括日志。',
        'report-draft': '写简短报告草稿。',
        'polish': '润色为自然中文。',
        'translate': '准确翻译。',
        'extract-points': '提取三条要点。',
        'status-summary': '输出一句状态摘要。',
    }
    return f"低风险文本任务。{instructions.get(task_type, '简洁处理。')} 不执行命令，不声称实时检查。\n文本：{content[:3000]}"


def run_qwen_local_task(task_type: str, content: str, timeout_seconds: int = 10) -> dict[str, Any]:
    max_timeout = 300 if task_type in ASYNC_LOCAL_TASK_TYPES else 60
    default_timeout = 180 if task_type in ASYNC_LOCAL_TASK_TYPES else 10
    timeout_seconds = clamp_int(timeout_seconds, default_timeout, 8, max_timeout)
    max_tokens = 32 if task_type in ('summary', 'status-summary', 'short-ack') else 160 if task_type in ASYNC_LOCAL_TASK_TYPES else 64
    payload = {
        'model': CLASSIFIER_MODEL,
        'prompt': local_task_prompt(task_type, content),
        'stream': False,
        'keep_alive': '30m',
        'options': {'temperature': 0.1, 'num_predict': max_tokens, 'num_ctx': 1024},
    }
    started = datetime.now(timezone.utc)
    data = ollama_json('/api/generate', payload, timeout=timeout_seconds)
    latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    result = str(data.get('response') or '').strip()
    if not result:
        raise RuntimeError('empty qwen output')
    return {'result': result, 'latency_ms': latency_ms, 'model': CLASSIFIER_MODEL}


def record_local_task(task_id: str, source_agent: str, task_type: str, route: str, status: str, content: str, result: str = '', error: str = '', model: str = '', latency_ms: int = 0, metadata: dict[str, Any] | None = None) -> None:
    current = now()
    with connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO local_tasks(id,created_at,updated_at,source_agent,task_type,status,route,content,result,error,model,latency_ms,metadata_json)
               VALUES (?,COALESCE((SELECT created_at FROM local_tasks WHERE id=?),?),?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, task_id, current, current, source_agent, task_type, status, route, content, result, error, model, latency_ms, json.dumps(metadata or {}, ensure_ascii=False)),
        )


def process_queued_local_tasks(limit: int = 3, timeout_seconds: int = 180) -> dict[str, Any]:
    limit = clamp_int(limit, 3, 1, 20)
    processed = []
    with connect() as con:
        rows = con.execute(
            """SELECT * FROM local_tasks
               WHERE route='qwen' AND status='queued'
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    for row in rows:
        task_id = row['id']
        task_type = row['task_type']
        content = row['content']
        metadata = json.loads(row['metadata_json'] or '{}')
        record_local_task(task_id, row['source_agent'], task_type, 'qwen', 'running', content, metadata=metadata)
        try:
            worker = run_qwen_local_task(task_type, content, timeout_seconds)
            record_local_task(task_id, row['source_agent'], task_type, 'qwen', 'done', content, result=worker['result'], model=worker['model'], latency_ms=worker['latency_ms'], metadata=metadata)
            processed.append({'id': task_id, 'task_type': task_type, 'status': 'done', 'model': worker['model'], 'latency_ms': worker['latency_ms']})
        except Exception as exc:
            record_local_task(task_id, row['source_agent'], task_type, 'qwen', 'failed', content, error=str(exc), metadata=metadata)
            processed.append({'id': task_id, 'task_type': task_type, 'status': 'failed', 'error': str(exc)})
    return {'ok': True, 'processed': processed, 'count': len(processed)}


def embed_text(text: str) -> list[float]:
    data = ollama_json('/api/embeddings', {'model': EMBED_MODEL, 'prompt': text[:8000]}, timeout=60)
    vector = data.get('embedding') or []
    if not isinstance(vector, list) or not vector:
        raise RuntimeError('empty embedding')
    return [float(x) for x in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def upsert_embedding(con: sqlite3.Connection, memory_id: str, text: str) -> bool:
    try:
        vector = embed_text(text)
    except Exception:
        return False
    con.execute(
        'INSERT OR REPLACE INTO memory_embeddings(memory_id,model,dim,vector_json,updated_at) VALUES (?,?,?,?,?)',
        (memory_id, EMBED_MODEL, len(vector), json.dumps(vector), now()),
    )
    return True


def embed_all_missing(limit: int = 500) -> dict[str, int]:
    ok, failed = 0, 0
    with connect() as con:
        rows = con.execute(
            '''SELECT m.* FROM memories m
               LEFT JOIN memory_embeddings e ON e.memory_id=m.id AND e.model=?
               WHERE m.disabled=0 AND e.memory_id IS NULL
               ORDER BY m.updated_at DESC LIMIT ?''',
            (EMBED_MODEL, limit),
        ).fetchall()
        for row in rows:
            text = f"{row['subject']}\n{row['content']}\n{row['kind']} {' '.join(json.loads(row['tags_json'] or '[]'))}"
            if upsert_embedding(con, row['id'], text):
                ok += 1
            else:
                failed += 1
    return {'embedded': ok, 'failed': failed}


def sensitivity_allowed(memory: dict[str, Any], sensitivity: str) -> bool:
    level = str(memory.get('metadata', {}).get('sensitivity', 'shared')).lower()
    if level in ('secret', 'credential'):
        return False
    if sensitivity == 'public':
        return level == 'public'
    if sensitivity == 'shared':
        return level in ('public', 'shared', 'low')
    return level in ('public', 'shared', 'low', 'private', '')


def rebuild_fts(con: sqlite3.Connection) -> None:
    con.execute('DELETE FROM memories_fts')
    con.execute('DELETE FROM events_fts')
    for row in con.execute('SELECT * FROM memories'):
        con.execute(
            'INSERT INTO memories_fts(id,content,subject,tags,scope,kind,source_agent) VALUES (?,?,?,?,?,?,?)',
            (row['id'], row['content'], row['subject'], ' '.join(json.loads(row['tags_json'] or '[]')), row['scope'], row['kind'], row['source_agent']),
        )
    for row in con.execute('SELECT * FROM events'):
        con.execute(
            'INSERT INTO events_fts(id,summary,result,project,actor) VALUES (?,?,?,?,?)',
            (row['id'], row['summary'], row['result'], row['project'], row['actor']),
        )


def memory_metadata_line(memory: dict[str, Any]) -> str:
    tags = ', '.join(memory['tags'])
    expiry = f"; expires: `{memory['expires_at']}`" if memory.get('expires_at') else ''
    canonical = '' if memory.get('canonical') else '; legacy-kind: `true`'
    sensitivity = memory.get('metadata', {}).get('sensitivity', 'shared')
    return f"  - id: `{memory['id']}`; source: `{memory['source_agent']}`; importance: `{memory['importance']}`; sensitivity: `{sensitivity}`; tags: `{tags}`{expiry}{canonical}"


def export_markdown() -> Path:
    with connect() as con:
        rows = con.execute('SELECT * FROM memories WHERE disabled=0 ORDER BY importance DESC, updated_at DESC').fetchall()
        events = [dict(row) for row in con.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 200')]
    memories = [row_to_memory(row) for row in rows if not is_expired(row['expires_at'])]
    active_memories = [m for m in memories if m['kind'] != 'noise']
    noise_memories = [m for m in memories if m['kind'] == 'noise']
    lines = ['# Shared Agent Memory', '', f'Updated: {now()}', '', '## Active Memories by Layer', '']
    for kind in CANONICAL_KINDS:
        if kind == 'noise':
            continue
        layer_items = [m for m in active_memories if m['kind'] == kind]
        if not layer_items:
            continue
        lines.append(f"### {LAYER_TITLES.get(kind, kind.title())}")
        lines.append('')
        for memory in layer_items:
            lines.append(f"- **[{memory['kind']}/{memory['scope']}]** {memory['content']}")
            lines.append(memory_metadata_line(memory))
        lines.append('')
    legacy_items = [m for m in active_memories if m['kind'] not in CANONICAL_KINDS]
    if legacy_items:
        lines += ['### Legacy / Unclassified', '']
        for memory in legacy_items:
            lines.append(f"- **[{memory['kind']}/{memory['scope']}]** {memory['content']}")
            lines.append(memory_metadata_line(memory))
        lines.append('')
    lines += ['## Noise Archive', '', f'Noise entries are excluded from default context. Active noise entries: {len(noise_memories)}.', '']
    lines += ['## Recent Events', '']
    for event in events:
        lines.append(f"- **{event['created_at']}** `{event['actor']}` {event['summary']}")
        if event['result']:
            lines.append(f"  - result: {event['result']}")
    path = EXPORT_DIR / 'shared-memory.md'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def build_memory_query(q: str, scope: str, selected_kinds: list[str], include_noise: bool, include_expired: bool, limit: int) -> tuple[str, list[Any]]:
    params: list[Any] = []
    filters = ['disabled=0']
    if scope:
        filters.append('scope=?')
        params.append(scope)
    if selected_kinds:
        filters.append('kind IN (' + ','.join('?' for _ in selected_kinds) + ')')
        params.extend(selected_kinds)
    elif not include_noise:
        filters.append("kind != 'noise'")
    if not include_expired:
        filters.append('(expires_at IS NULL OR expires_at >= ?)')
        params.append(now())
    if q.strip():
        terms = [term for term in q.strip().replace('-', ' ').split() if term]
        if terms:
            clauses = []
            for term in terms:
                clauses.append('(content LIKE ? OR subject LIKE ? OR tags_json LIKE ? OR source_agent LIKE ? OR kind LIKE ? OR scope LIKE ?)')
                pattern = f"%{term}%"
                params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
            filters.append('(' + ' OR '.join(clauses) + ')')
    sql = f"SELECT * FROM memories WHERE {' AND '.join(filters)} ORDER BY importance DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    return sql, params


init_db()


@app.get('/health')
def health():
    with connect() as con:
        memories = con.execute('SELECT count(*) FROM memories').fetchone()[0]
        events = con.execute('SELECT count(*) FROM events').fetchone()[0]
        embeddings = con.execute('SELECT count(*) FROM memory_embeddings').fetchone()[0]
        local_tasks = con.execute('SELECT count(*) FROM local_tasks').fetchone()[0]
    return {'ok': True, 'version': app.version, 'memories': memories, 'events': events, 'embeddings': embeddings, 'local_tasks': local_tasks, 'db': str(DB_PATH), 'vault': str(VAULT_DIR), 'models': {'classifier': CLASSIFIER_MODEL, 'embedding': EMBED_MODEL, 'local_worker': CLASSIFIER_MODEL}}


@app.get('/api/layers')
def layers():
    now_value = now()
    with connect() as con:
        rows = con.execute(
            '''SELECT kind,
                      count(*) AS count,
                      sum(CASE WHEN disabled=0 THEN 1 ELSE 0 END) AS active,
                      sum(CASE WHEN disabled=0 AND expires_at IS NOT NULL AND expires_at < ? THEN 1 ELSE 0 END) AS expired
               FROM memories
               GROUP BY kind
               ORDER BY count DESC, kind ASC''',
            (now_value,),
        ).fetchall()
    seen = {row['kind'] for row in rows}
    result = []
    for row in rows:
        result.append({'kind': row['kind'], 'count': row['count'], 'active': row['active'] or 0, 'expired': row['expired'] or 0, 'canonical': row['kind'] in CANONICAL_KINDS})
    for kind in CANONICAL_KINDS:
        if kind not in seen:
            result.append({'kind': kind, 'count': 0, 'active': 0, 'expired': 0, 'canonical': True})
    return {'ok': True, 'layers': result, 'canonical_kinds': CANONICAL_KINDS}


@app.post('/api/local-task')
def local_task(payload: LocalTaskIn):
    decision = classify_local_task(payload.task_type, payload.content)
    task_id = str(uuid4())
    route = decision['route']
    task_type = decision['task_type']
    metadata = dict(payload.metadata)
    metadata['decision'] = decision
    if True:  # qwen disabled, always fallback to gpt
        record_local_task(task_id, payload.source_agent, task_type, route, 'escalate', payload.content, error=decision['reason'], metadata=metadata)
        return {'ok': True, 'id': task_id, 'route': 'gpt', 'status': 'escalate', 'reason': decision['reason'], 'fallback_required': True}
    record_local_task(task_id, payload.source_agent, task_type, route, 'queued', payload.content, metadata=metadata)
    if not payload.sync:
        return {'ok': True, 'id': task_id, 'route': 'qwen', 'status': 'queued'}
    try:
        worker = run_qwen_local_task(task_type, payload.content, payload.timeout_seconds)
        record_local_task(task_id, payload.source_agent, task_type, route, 'done', payload.content, result=worker['result'], model=worker['model'], latency_ms=worker['latency_ms'], metadata=metadata)
        return {'ok': True, 'id': task_id, 'route': 'qwen', 'status': 'done', **worker}
    except Exception as exc:
        record_local_task(task_id, payload.source_agent, task_type, 'gpt', 'fallback', payload.content, error=str(exc), metadata=metadata)
        return {'ok': True, 'id': task_id, 'route': 'gpt', 'status': 'fallback', 'reason': f'qwen_failed: {exc}', 'fallback_required': True}


@app.get('/api/local-task/{task_id}')
def get_local_task(task_id: str):
    with connect() as con:
        row = con.execute('SELECT * FROM local_tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'local task not found')
    item = dict(row)
    item['metadata'] = json.loads(item.pop('metadata_json') or '{}')
    return item


@app.post('/api/local-task/process')
def process_local_task_queue(limit: int = Query(3, ge=1, le=20), timeout_seconds: int = Query(180, ge=8, le=300)):
    return process_queued_local_tasks(limit, timeout_seconds)


@app.post('/api/memory')
def add_memory(payload: MemoryIn):
    created = now()
    expires_at = None
    if payload.ttl_days:
        expires_at = (now_dt() + timedelta(days=payload.ttl_days)).isoformat(timespec='seconds')
    metadata = dict(payload.metadata)
    if payload.kind not in CANONICAL_KINDS:
        metadata.setdefault('legacy_kind_warning', True)
    memory_id = str(uuid4())
    embedded = False
    with connect() as con:
        con.execute(
            '''INSERT INTO memories(id,created_at,updated_at,scope,kind,source_agent,subject,content,confidence,importance,tags_json,metadata_json,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (memory_id, created, created, payload.scope, payload.kind, payload.source_agent, payload.subject, payload.content, payload.confidence, payload.importance, json.dumps(payload.tags, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), expires_at),
        )
        con.execute('INSERT INTO memories_fts(id,content,subject,tags,scope,kind,source_agent) VALUES (?,?,?,?,?,?,?)', (memory_id, payload.content, payload.subject, ' '.join(payload.tags), payload.scope, payload.kind, payload.source_agent))
        embedded = upsert_embedding(con, memory_id, f"{payload.subject}\n{payload.content}\n{payload.kind} {' '.join(payload.tags)}")
    export_markdown()
    return {'ok': True, 'id': memory_id, 'canonical': payload.kind in CANONICAL_KINDS, 'embedded': embedded, 'warning': None if payload.kind in CANONICAL_KINDS else 'non-canonical kind accepted as legacy'}


@app.post('/api/classify')
def classify(payload: ClassifyIn):
    result = classify_memory_content(payload.content, payload.source_agent, payload.scope, payload.subject_hint, payload.tags_hint)
    return {'ok': True, 'classification': result, 'model': CLASSIFIER_MODEL}


@app.post('/api/memory/auto')
def add_memory_auto(payload: AutoMemoryIn):
    classification = classify_memory_content(payload.content, payload.source_agent, payload.scope, payload.subject_hint, payload.tags_hint)
    if not classification.get('should_save') and not payload.force:
        return {'ok': True, 'saved': False, 'classification': classification, 'reason': classification.get('reason', 'not worth saving')}
    metadata = {'auto_classified': True, 'classifier_model': CLASSIFIER_MODEL, 'original_length': len(payload.content), 'sensitivity': classification.get('sensitivity', 'shared'), 'classification_reason': classification.get('reason', '')}
    memory = MemoryIn(content=classification['summary'], scope=classification.get('scope') or payload.scope, kind=classification['kind'], source_agent=payload.source_agent, subject=classification['subject'], confidence=classification['confidence'], importance=classification['importance'], tags=classification['tags'], metadata=metadata, ttl_days=classification.get('ttl_days'))
    saved = add_memory(memory)
    return {'ok': True, 'saved': True, 'classification': classification, 'memory': saved}


@app.get('/api/memory/{memory_id}')
def get_memory(memory_id: str):
    with connect() as con:
        row = con.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'memory not found')
    return row_to_memory(row)


@app.patch('/api/memory/{memory_id}')
def update_memory(memory_id: str, payload: MemoryUpdate):
    fields, values = [], []
    if payload.content is not None:
        fields.append('content=?')
        values.append(payload.content)
    if payload.disabled is not None:
        fields.append('disabled=?')
        values.append(1 if payload.disabled else 0)
    if payload.confidence is not None:
        fields.append('confidence=?')
        values.append(payload.confidence)
    if payload.importance is not None:
        fields.append('importance=?')
        values.append(payload.importance)
    if payload.tags is not None:
        fields.append('tags_json=?')
        values.append(json.dumps(payload.tags, ensure_ascii=False))
    if payload.metadata is not None:
        fields.append('metadata_json=?')
        values.append(json.dumps(payload.metadata, ensure_ascii=False))
    if not fields:
        return {'ok': True, 'changed': 0}
    fields.append('updated_at=?')
    values.append(now())
    values.append(memory_id)
    with connect() as con:
        cursor = con.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id=?", values)
        if cursor.rowcount == 0:
            raise HTTPException(404, 'memory not found')
        rebuild_fts(con)
        if payload.content is not None:
            row = con.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
            upsert_embedding(con, memory_id, f"{row['subject']}\n{row['content']}\n{row['kind']} {' '.join(json.loads(row['tags_json'] or '[]'))}")
    export_markdown()
    return {'ok': True, 'changed': cursor.rowcount}


@app.post('/api/event')
def add_event(payload: EventIn):
    event_id = str(uuid4())
    created = now()
    with connect() as con:
        con.execute('INSERT INTO events(id,created_at,actor,project,summary,result,files_json,services_json,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)', (event_id, created, payload.actor, payload.project, payload.summary, payload.result, json.dumps(payload.files, ensure_ascii=False), json.dumps(payload.services, ensure_ascii=False), json.dumps(payload.metadata, ensure_ascii=False)))
        con.execute('INSERT INTO events_fts(id,summary,result,project,actor) VALUES (?,?,?,?,?)', (event_id, payload.summary, payload.result, payload.project, payload.actor))
    export_markdown()
    return {'ok': True, 'id': event_id}


@app.get('/api/search')
def search(q: str = Query('', min_length=0), scope: str = '', kind: str = '', kinds: str = '', limit: int = 10, include_noise: bool = False, include_expired: bool = False):
    limit = max(1, min(limit, 50))
    selected_kinds = parse_kinds(kind, kinds)
    with connect() as con:
        sql, params = build_memory_query(q, scope, selected_kinds, include_noise, include_expired, limit)
        rows = con.execute(sql, params).fetchall()
    return {'ok': True, 'query': q, 'kinds': selected_kinds, 'results': [row_to_memory(row) for row in rows]}


@app.get('/api/search/hybrid')
def hybrid_search(q: str = Query('', min_length=1), scope: str = '', kind: str = '', kinds: str = '', limit: int = 8, include_noise: bool = False, include_expired: bool = False, include_failure: bool = False, sensitivity: str = 'private'):
    limit = max(1, min(limit, 50))
    selected_kinds = parse_kinds(kind, kinds)
    if include_failure and 'failure' not in selected_kinds:
        selected_kinds.append('failure')
    fts_results = search(q=q, scope=scope, kinds=','.join(selected_kinds), limit=max(limit * 4, 20), include_noise=include_noise, include_expired=include_expired)['results']
    fts_rank = {item['id']: index for index, item in enumerate(fts_results)}
    vector_rank: dict[str, float] = {}
    vector_available = False
    try:
        query_vector = embed_text(q)
        vector_available = True
        embed_all_missing(200)
        with connect() as con:
            rows = con.execute('''SELECT m.*, e.vector_json FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id AND e.model=? WHERE m.disabled=0''', (EMBED_MODEL,)).fetchall()
        for row in rows:
            memory = row_to_memory(row)
            if selected_kinds and memory['kind'] not in selected_kinds:
                continue
            if not include_noise and memory['kind'] == 'noise':
                continue
            if not include_expired and memory['expired']:
                continue
            if scope and memory['scope'] != scope:
                continue
            if not sensitivity_allowed(memory, sensitivity):
                continue
            vector_rank[memory['id']] = cosine_similarity(query_vector, json.loads(row['vector_json'] or '[]'))
    except Exception:
        vector_available = False
    candidates: dict[str, dict[str, Any]] = {}
    for item in fts_results:
        if sensitivity_allowed(item, sensitivity):
            candidates[item['id']] = item
    if vector_rank:
        ids = list(vector_rank.keys())
        with connect() as con:
            placeholders = ','.join('?' for _ in ids)
            rows = con.execute(f'SELECT * FROM memories WHERE id IN ({placeholders})', ids).fetchall() if ids else []
        for row in rows:
            item = row_to_memory(row)
            if sensitivity_allowed(item, sensitivity):
                candidates[item['id']] = item
    max_fts_rank = max(1, len(fts_rank))
    scored = []
    for memory_id, item in candidates.items():
        fts_score = 1.0 - (fts_rank[memory_id] / max_fts_rank) if memory_id in fts_rank else 0.0
        vec_score = max(0.0, min(1.0, (vector_rank.get(memory_id, 0.0) + 1.0) / 2.0)) if vector_available else 0.0
        importance_score = item.get('importance', 5) / 10.0
        kind_bonus = 0.08 if item.get('kind') in ('preference', 'service_state', 'decision', 'success') else 0.0
        final_score = 0.45 * vec_score + 0.35 * fts_score + 0.15 * importance_score + kind_bonus
        enriched = dict(item)
        enriched['scores'] = {'final': final_score, 'vector': vec_score, 'fts': fts_score, 'importance': importance_score}
        scored.append(enriched)
    scored.sort(key=lambda item: item['scores']['final'], reverse=True)
    return {'ok': True, 'query': q, 'model': EMBED_MODEL, 'vector_available': vector_available, 'kinds': selected_kinds, 'results': scored[:limit]}


@app.get('/api/context')
def context(q: str = '', limit: int = 8, kind: str = '', kinds: str = '', mode: str = 'default', include_failure: bool = False, include_temporary: bool = False, include_noise: bool = False, include_expired: bool = False, hybrid: bool = True, sensitivity: str = 'private'):
    limit = max(1, min(limit, 50))
    selected_kinds = context_kinds(mode, kind, kinds, include_failure, include_temporary, include_noise)
    if hybrid and q.strip():
        results = hybrid_search(q=q, kinds=','.join(selected_kinds), limit=limit, include_noise=include_noise, include_expired=include_expired, include_failure=include_failure, sensitivity=sensitivity)['results']
    else:
        results = search(q=q, kinds=','.join(selected_kinds), limit=limit, include_noise=include_noise, include_expired=include_expired)['results']
        results = [item for item in results if sensitivity_allowed(item, sensitivity)]
    lines = [f'Shared long-term memory context (mode={mode}, kinds={",".join(selected_kinds)}):']
    for memory in results:
        expiry = f", expires={memory['expires_at']}" if memory.get('expires_at') else ''
        lines.append(f"- [{memory['kind']}/{memory['scope']}] {memory['content']} (source={memory['source_agent']}, importance={memory['importance']}, id={memory['id']}{expiry})")
    return {'ok': True, 'mode': mode, 'kinds': selected_kinds, 'context': '\n'.join(lines), 'results': results}


@app.post('/api/embeddings/reindex')
def reindex_embeddings(limit: int = 500):
    limit = max(1, min(limit, 5000))
    stats = embed_all_missing(limit)
    return {'ok': True, 'model': EMBED_MODEL, **stats}


@app.post('/api/expire')
def expire_memories():
    current = now()
    with connect() as con:
        cursor = con.execute('UPDATE memories SET disabled=1, updated_at=? WHERE disabled=0 AND expires_at IS NOT NULL AND expires_at < ?', (current, current))
        changed = cursor.rowcount
        rebuild_fts(con)
    export_markdown()
    return {'ok': True, 'disabled': changed}


@app.post('/api/reindex')
def reindex():
    with connect() as con:
        rebuild_fts(con)
    embed_stats = embed_all_missing(5000)
    path = export_markdown()
    return {'ok': True, 'export': str(path), 'embeddings': embed_stats}


@app.get('/export/shared-memory.md', response_class=PlainTextResponse)
def export_md():
    path = export_markdown()
    return path.read_text(encoding='utf-8')


@app.get('/', response_class=HTMLResponse)
def home():
    return '''<!doctype html><html><head><meta charset="utf-8"><title>Shared Agent Memory</title><style>body{font-family:system-ui;background:#0b1020;color:#e5e7eb;margin:40px}input,select,button{padding:10px;border-radius:8px;border:1px solid #334155;background:#111827;color:#e5e7eb}.card{background:#111827;border:1px solid #243244;border-radius:14px;padding:16px;margin:12px 0}a{color:#93c5fd}</style></head><body><h1>Shared Agent Memory</h1><p>Hermes + OpenClaw shared long-term memory service with qwen classification and hybrid FTS/vector recall.</p><input id=q placeholder="search memory" size=42><select id=kind><option value="">default</option><option>preference</option><option>fact</option><option>service_state</option><option>decision</option><option>success</option><option>failure</option><option>temporary</option><option>todo</option><option>noise</option></select><button onclick="go()">Hybrid Search</button><pre id=out></pre><p><a href="/api/layers">Layers</a> · <a href="/export/shared-memory.md">Markdown export</a></p><script>async function go(){const q=document.getElementById('q').value;const k=document.getElementById('kind').value;const r=await fetch('/api/search/hybrid?q='+encodeURIComponent(q)+'&kind='+encodeURIComponent(k));document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2)}</script></body></html>'''
