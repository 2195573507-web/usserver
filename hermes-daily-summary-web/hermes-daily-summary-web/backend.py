from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path('/opt/hermes-daily-summary-web')
STATIC_DIR = BASE_DIR / 'static'
DOCS_ROOT = Path('/root/obsidian-vault/资讯更新')
FILENAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})-(.+?)\.md$')
DATE_PREFIX_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})')

app = FastAPI(title='Hermes Daily Summary Web', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


def parse_summary_file(path: Path) -> dict:
    match = FILENAME_RE.match(path.name)
    date = match.group(1) if match else path.stem[:10]
    stat = path.stat()
    text = path.read_text(encoding='utf-8', errors='replace')
    title = ''
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            title = stripped.lstrip('#').strip()
            break
    if not title:
        title = f'{date} Hermes每日汇总'
    excerpt = ' '.join(line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#'))[:220]
    return {
        'date': date,
        'filename': path.name,
        'title': title,
        'size': stat.st_size,
        'updated_at': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
        'excerpt': excerpt,
    }


def docs_root() -> Path:
    return DOCS_ROOT


def list_summary_paths() -> list[Path]:
    root = docs_root()
    if not root.exists():
        return []
    return sorted(
        [p for p in root.rglob('*.md') if p.is_file()],
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )


def classify_doc(path: Path) -> tuple[str, str]:
    rel = path.relative_to(docs_root())
    parts = rel.parts
    category = parts[0] if len(parts) > 1 else '未分类'
    match = DATE_PREFIX_RE.match(path.name)
    date = match.group(1) if match else datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    return category, date


@app.get('/api/health')
def health():
    files = list_summary_paths()
    return {'ok': True, 'summary_dir': str(docs_root()), 'count': len(files), 'latest': files[0].name if files else None}


@app.get('/api/summaries')
def summaries(q: str = Query('', max_length=100), limit: int = Query(100, ge=1, le=500)):
    query = q.strip().lower()
    items = []
    for path in list_summary_paths():
        item = parse_summary_file(path)
        category, date = classify_doc(path)
        item['category'] = category
        item['date'] = date
        if query:
            haystack = f"{item['date']} {item['category']} {item['filename']} {item['title']} {item['excerpt']}".lower()
            if query not in haystack:
                continue
        items.append(item)
        if len(items) >= limit:
            break
    return {'ok': True, 'count': len(items), 'items': items}


@app.get('/api/summaries/{date}')
def summary_by_date(date: str):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        raise HTTPException(400, 'invalid date')
    for path in list_summary_paths():
        if path.name.startswith(date):
            item = parse_summary_file(path)
            category, _ = classify_doc(path)
            item['category'] = category
            item['content'] = path.read_text(encoding='utf-8', errors='replace')
            return {'ok': True, 'summary': item}
    raise HTTPException(404, 'summary not found')


@app.get('/')
def root():
    return FileResponse(str(STATIC_DIR / 'index.html'))


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('backend:app', host='[REDACTED_IP]', port=9500, log_level='info')
