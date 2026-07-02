from __future__ import annotations

import asyncio
import html
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI(title='Server AI Workbench')

BASE_DIR = Path(__file__).resolve().parent
SERVICES_FILE = BASE_DIR / 'services.yml'
SERVERHUB_FRONTEND = Path('/opt/serverhub/frontend')
SERVER_IP = '[REDACTED_IP]'
SERVER_DOMAIN = 'server.zmjjkkk.fun'
HERMES_API = 'http://[REDACTED_IP]:9100'
MONITOR_API = 'http://[REDACTED_IP]:9000'
KNOWLEDGE_API = 'http://[REDACTED_IP]:9200'
MEMORY_API = 'http://[REDACTED_IP]:9400'
LOG_DIR = Path('/www/logs')

if SERVERHUB_FRONTEND.exists():
    app.mount('/serverhub-assets', StaticFiles(directory=str(SERVERHUB_FRONTEND)), name='serverhub-assets')

DEFAULT_PROJECTS = [
    {'name': 'Hermes Gateway', 'unit': 'hermes-gateway', 'port': None, 'path': '/opt/hermes-agent', 'group': 'AI / Agents', 'url': None, 'desc': 'QQBot/消息平台接入与 Hermes 对话运行时', 'icon': '💬', 'tags': ['hermes', 'gateway'], 'exposure': 'internal', 'pinned': False},
    {'name': 'Agent 控制台', 'unit': 'hermes-dashboard-web', 'port': 9100, 'path': '/opt/hermes-dashboard', 'group': 'AI / Agents', 'url': 'http://agent.zmjjkkk.fun/', 'desc': '模型配置、会话、token、成本、cache 与 agent 控制台', 'icon': '🤖', 'tags': ['ai', 'hermes'], 'exposure': 'protected', 'pinned': True},
    {'name': 'OpenClaw Gateway', 'unit': 'openclaw-gateway', 'port': 18789, 'path': '/opt/openclaw', 'group': 'AI / Agents', 'url': None, 'desc': 'OpenClaw 网关与工具运行时', 'icon': '🐾', 'tags': ['ai', 'openclaw'], 'exposure': 'local-only', 'pinned': True},
    {'name': 'System Monitor', 'unit': 'hermes-system-monitor', 'port': 9000, 'path': '/opt/hermes-system-monitor', 'group': '系统监控', 'url': 'http://monitor.zmjjkkk.fun/', 'desc': 'CPU、内存、磁盘、网络与历史指标', 'icon': '📈', 'tags': ['monitor', 'system'], 'exposure': 'protected', 'pinned': True},
    {'name': '知识库', 'unit': 'obsidian-knowledge-web', 'port': 9200, 'path': '/opt/obsidian-knowledge-web', 'group': '知识与记忆', 'url': 'http://notes.zmjjkkk.fun/', 'desc': 'Obsidian vault 文件与知识图谱浏览', 'icon': '📚', 'tags': ['notes', 'knowledge'], 'exposure': 'protected', 'pinned': True},
    {'name': 'Shared Agent Memory', 'unit': 'shared-agent-memory', 'port': 9400, 'path': '/opt/shared-agent-memory', 'group': '知识与记忆', 'url': None, 'desc': 'Hermes/OpenClaw 共享记忆服务', 'icon': '🧠', 'tags': ['memory'], 'exposure': 'local-only', 'pinned': True},
    {'name': '文件浏览', 'unit': 'server-file-web', 'port': 9300, 'path': '/opt/server-file-web', 'group': '文件与数据', 'url': 'http://files.zmjjkkk.fun/', 'desc': '服务器文件结构浏览', 'icon': '🗂️', 'tags': ['files'], 'exposure': 'protected', 'pinned': True},
    {'name': 'Ollama', 'unit': 'ollama', 'port': 11434, 'path': '/www/data/ollama', 'group': '系统监控', 'url': None, 'desc': '本地模型服务', 'icon': '🦙', 'tags': ['ollama', 'local'], 'exposure': 'local-only', 'pinned': False},
    {'name': 'nginx', 'unit': 'nginx', 'port': 80, 'path': '/etc/nginx', 'group': '入口与代理', 'url': 'http://server.zmjjkkk.fun/', 'desc': '公网入口与反向代理', 'icon': '🌐', 'tags': ['nginx', 'proxy'], 'exposure': 'public', 'pinned': False},
    {'name': 'Server Home', 'unit': 'server-home', 'port': 8000, 'path': '/opt/server-home', 'group': '入口与代理', 'url': 'http://server.zmjjkkk.fun/', 'desc': '当前服务器 AI 工作台', 'icon': '🏠', 'tags': ['home', 'dashboard'], 'exposure': 'public', 'pinned': False},
]

LOG_SOURCES = {
    'hermes': {'title': 'Hermes errors', 'path': LOG_DIR / 'hermes' / 'errors.log'},
    'openclaw': {'title': 'OpenClaw log', 'path': LOG_DIR / 'openclaw' / 'openclaw.log'},
    'nginx': {'title': 'nginx error', 'path': LOG_DIR / 'nginx' / 'error.log'},
    'server-home': {'title': 'server-home journal', 'unit': 'server-home'},
    'monitor': {'title': 'system-monitor journal', 'unit': 'hermes-system-monitor'},
}

OPS_UNITS = ['hermes-gateway', 'hermes-dashboard-web', 'openclaw-gateway', 'hermes-system-monitor', 'obsidian-knowledge-web', 'shared-agent-memory', 'server-file-web', 'server-home', 'nginx']
EXPOSURE_LABELS = {'public': '公网', 'protected': '受保护', 'local-only': '本地', 'internal': '内部'}
EXPOSURE_ORDER = {'public': 0, 'protected': 1, 'local-only': 2, 'internal': 3}


def esc(value) -> str:
    return html.escape('' if value is None else str(value))


def js(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace('</', '<\\/')


def normalize_service(item: dict) -> dict:
    service = dict(item or {})
    service.setdefault('name', service.get('unit', 'unknown'))
    service.setdefault('unit', service['name'])
    service.setdefault('icon', '▫️')
    service.setdefault('group', '其他')
    service.setdefault('url', None)
    service.setdefault('health_url', None)
    service.setdefault('port', None)
    service.setdefault('path', '-')
    service.setdefault('desc', '')
    service.setdefault('tags', [])
    service.setdefault('exposure', 'internal')
    service.setdefault('pinned', False)
    if isinstance(service.get('tags'), str):
        service['tags'] = [t.strip() for t in service['tags'].split(',') if t.strip()]
    return service


def load_services() -> list[dict]:
    if not SERVICES_FILE.exists():
        return [normalize_service(x) for x in DEFAULT_PROJECTS]
    try:
        raw = yaml.safe_load(SERVICES_FILE.read_text(encoding='utf-8')) or {}
        items = raw.get('services', raw if isinstance(raw, list) else [])
        return [normalize_service(x) for x in items]
    except Exception:
        return [normalize_service(x) for x in DEFAULT_PROJECTS]


def public_service_view(service: dict) -> dict:
    allowed = ['name', 'unit', 'icon', 'group', 'url', 'port', 'path', 'desc', 'tags', 'exposure', 'pinned', 'systemd', 'enabled', 'online', 'status', 'status_label', 'http_status', 'elapsed_ms', 'http_error', 'checked_at', 'health_url']
    return {k: service.get(k) for k in allowed if k in service}


def run(cmd: list[str], timeout: int = 6) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, '', str(exc)


def unit_state(unit: str) -> str:
    code, out, _ = run(['systemctl', 'is-active', unit], timeout=4)
    return out if code == 0 else 'unknown'


def unit_enabled(unit: str) -> str:
    code, out, _ = run(['systemctl', 'is-enabled', unit], timeout=4)
    return out if code == 0 else 'unknown'


def read_tail(path: Path, limit: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(errors='replace').splitlines()[-limit:]
    except Exception:
        return []


def journal_tail(unit: str, limit: int = 80) -> list[str]:
    code, out, err = run(['journalctl', '-u', unit, '-n', str(limit), '--no-pager', '-o', 'short-iso'], timeout=3)
    if code != 0:
        return [err or out or 'journalctl failed']
    return out.splitlines()


async def fetch_json(url: str, timeout: float = 1.2):
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code >= 500:
                return None
            return resp.json()
    except Exception:
        return None


async def probe_project(project: dict, http: bool = True) -> dict:
    result = normalize_service(project)
    result['systemd'] = unit_state(result['unit']) if result.get('unit') else 'unknown'
    result['enabled'] = 'unknown'
    result['online'] = result['systemd'] == 'active'
    result['status'] = 'online' if result['online'] else 'offline'
    result['status_label'] = '正常' if result['online'] else '离线'
    result['http_status'] = None
    result['elapsed_ms'] = None
    result['http_error'] = None
    result['checked_at'] = datetime.now(timezone.utc).isoformat()
    health_url = result.get('health_url') or result.get('url')
    if http and health_url:
        start = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(timeout=1.2, follow_redirects=True) as client:
                resp = await client.get(health_url)
            result['http_status'] = resp.status_code
            if resp.status_code >= 500:
                result['online'] = False
                result['status'] = 'offline'
                result['status_label'] = '离线'
            elif resp.status_code >= 400:
                result['status'] = 'degraded' if result['online'] else 'offline'
                result['status_label'] = '异常' if result['online'] else '离线'
        except Exception as exc:
            result['http_error'] = str(exc)[:140]
            if result['online']:
                result['status'] = 'degraded'
                result['status_label'] = '降级'
            else:
                result['status'] = 'offline'
                result['status_label'] = '离线'
        result['elapsed_ms'] = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    return result


async def collect_state(full: bool = True) -> dict:
    services = load_services()
    projects = await asyncio.gather(*(probe_project(p, http=full) for p in services))
    hermes_overview, hermes_config, hermes_sessions, system_metrics, knowledge_status, memory_health = await asyncio.gather(
        fetch_json(f'{HERMES_API}/api/overview', timeout=1.0),
        fetch_json(f'{HERMES_API}/api/config', timeout=1.0),
        fetch_json(f'{HERMES_API}/api/sessions?limit=8&offset=0', timeout=1.0) if full else asyncio.sleep(0, result={}),
        fetch_json(f'{MONITOR_API}/api/system/metrics/latest', timeout=1.0) if full else asyncio.sleep(0, result={}),
        fetch_json(f'{KNOWLEDGE_API}/api/status', timeout=1.0) if full else asyncio.sleep(0, result={}),
        fetch_json(f'{MEMORY_API}/health', timeout=1.0) if full else asyncio.sleep(0, result={}),
    )
    online_count = sum(1 for p in projects if p.get('status') == 'online')
    degraded_count = sum(1 for p in projects if p.get('status') == 'degraded')
    offline_count = sum(1 for p in projects if p.get('status') == 'offline')
    return {
        'server': SERVER_IP,
        'domain': SERVER_DOMAIN,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'projects': [public_service_view(p) for p in projects],
        'summary': {'total': len(projects), 'online': online_count, 'degraded': degraded_count, 'offline': offline_count},
        'hermes': {'overview': hermes_overview or {}, 'config': hermes_config or {}, 'sessions': hermes_sessions or {}, 'status': {}},
        'system': {'status': {}, 'metrics': system_metrics or {}},
        'knowledge': {'status': knowledge_status or {}, 'memory': memory_health or {}},
        'alerts': alert_summary(),
    }


async def collect_fast_state() -> dict:
    return await collect_state(full=False)


def alert_summary() -> dict:
    keywords = ('error', 'failed', '502', '429', 'traceback', 'exception', 'forbidden')
    result = {}
    for key in ('hermes', 'openclaw', 'nginx'):
        source = LOG_SOURCES[key]
        lines = read_tail(source['path'], 120)
        result[key] = [line for line in lines if any(k in line.lower() for k in keywords)][-10:]
    return result


def nav(active: str) -> str:
    items = [('/', '总览'), ('/apps', '项目地图'), ('/ai', 'AI'), ('/knowledge', '知识记忆'), ('/system', '系统'), ('/logs', '日志'), ('/ops', '运维')]
    return ''.join(f"<a class='{'active' if label == active else ''}' href='{href}'>{label}</a>" for href, label in items)


CSS = r'''

:root{
  color-scheme:dark;
  --bg:#070b12;--bg2:#0d1320;--panel:#111827;--panel2:#172033;--card:#121b2c;--line:#283349;--line2:#3b4b67;
  --text:#e7edf7;--muted:#91a1b8;--soft:#cbd5e1;--accent:#67e8f9;--accent2:#8b5cf6;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;
  --public:#60a5fa;--protected:#a78bfa;--local:#34d399;--internal:#94a3b8;--shadow:0 24px 80px rgba(0,0,0,.34);
  --radius:22px;--tap:44px;
}
*{box-sizing:border-box}html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}body{margin:0;background:radial-gradient(circle at 15% 5%,rgba(103,232,249,.14),transparent 34%),radial-gradient(circle at 85% 8%,rgba(139,92,246,.16),transparent 34%),linear-gradient(180deg,var(--bg),var(--bg2));color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;overflow-x:hidden}a{color:inherit;text-decoration:none}button,input{font:inherit}.wrap{width:min(1380px,100%);margin:0 auto;padding:calc(18px + env(safe-area-inset-top)) max(14px,env(safe-area-inset-left)) calc(44px + env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-right))}.top{position:sticky;top:0;z-index:20;display:flex;gap:18px;align-items:center;justify-content:space-between;margin:-6px -4px 18px;padding:8px 4px;background:linear-gradient(180deg,rgba(7,11,18,.94),rgba(7,11,18,.70),transparent);backdrop-filter:blur(14px)}.brand{min-width:0}.brand h1{margin:0;font-size:clamp(22px,3vw,28px);letter-spacing:-.04em;white-space:nowrap}.brand p{margin:5px 0 0;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.nav a{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:9px 13px;border:1px solid var(--line);border-radius:999px;color:var(--soft);background:rgba(17,24,39,.72);backdrop-filter:blur(12px);white-space:nowrap}.nav a.active,.nav a:hover{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#07111f;border-color:transparent}.hero,.panel,.card{background:linear-gradient(180deg,rgba(18,27,44,.92),rgba(15,23,42,.92));border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}.hero{padding:clamp(18px,3vw,30px);margin-bottom:16px;position:relative;overflow:hidden}.hero:before{content:'';position:absolute;inset:-1px;background:linear-gradient(135deg,rgba(103,232,249,.13),rgba(139,92,246,.10),transparent);pointer-events:none}.hero>*{position:relative}.hero-grid{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(280px,.95fr);gap:18px;align-items:center}.portal-hero{min-height:calc(100svh - 128px);display:grid;align-items:center}.eyebrow{color:var(--accent);font-weight:800;font-size:12px;text-transform:uppercase;letter-spacing:.12em}.headline{font-size:clamp(32px,6vw,58px);line-height:1.02;letter-spacing:-.07em;margin:8px 0 10px}.subtitle{color:var(--muted);font-size:clamp(15px,2.5vw,17px);line-height:1.65;max-width:720px}.searchbox{display:flex;align-items:center;gap:10px;margin-top:18px;background:rgba(7,11,18,.68);border:1px solid var(--line2);border-radius:18px;padding:12px 14px;min-height:48px}.searchbox input{all:unset;flex:1;min-width:0;color:var(--text);font-size:16px}.searchbox kbd{border:1px solid var(--line2);border-bottom-width:2px;border-radius:8px;color:var(--muted);font-size:12px;padding:3px 7px}.shortcut-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:22px}.shortcut{position:relative;display:flex;min-height:126px;flex-direction:column;justify-content:space-between;padding:18px;border:1px solid var(--line);border-radius:22px;background:linear-gradient(145deg,rgba(103,232,249,.16),rgba(139,92,246,.11) 42%,rgba(15,23,42,.82));overflow:hidden;transition:transform .16s ease,border-color .16s ease}.shortcut:hover{transform:translateY(-2px);border-color:rgba(103,232,249,.62)}.shortcut:after{content:'↗';position:absolute;right:16px;top:12px;color:rgba(231,237,247,.58);font-size:18px}.shortcut-icon{font-size:30px}.shortcut-title{font-size:19px;font-weight:900;letter-spacing:-.04em;margin-top:10px}.shortcut-desc{color:var(--muted);font-size:13px;margin-top:4px;line-height:1.35}.status-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:16px}.status-chip{display:flex;align-items:center;gap:8px;min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:16px;background:rgba(7,11,18,.38);color:var(--soft);font-size:13px}.status-chip span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.advanced-links{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.stat{background:rgba(23,32,51,.72);border:1px solid var(--line);border-radius:18px;padding:16px}.k{color:var(--muted);font-size:12px}.v{font-size:clamp(24px,5vw,32px);font-weight:850;letter-spacing:-.04em;margin-top:5px}.panel{padding:18px;margin:16px 0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:14px}.section-title{font-size:18px;margin:0;letter-spacing:-.03em}.muted{color:var(--muted)}.small{font-size:13px;line-height:1.5}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(275px,100%),1fr));gap:14px}.pinned-grid{grid-template-columns:repeat(auto-fill,minmax(min(310px,100%),1fr))}.card{padding:17px;box-shadow:none;transition:transform .15s ease,border-color .15s ease,background .15s ease}.card:hover{transform:translateY(-2px);border-color:var(--line2);background:linear-gradient(180deg,rgba(22,34,55,.96),rgba(17,27,44,.96))}.card-top{display:flex;gap:12px;align-items:flex-start;justify-content:space-between}.service-title{display:flex;gap:10px;align-items:center;font-size:17px;font-weight:850;min-width:0}.service-title span:last-child{min-width:0;overflow-wrap:anywhere}.icon{display:grid;place-items:center;width:42px;height:42px;border-radius:14px;background:rgba(103,232,249,.10);border:1px solid var(--line);font-size:22px;flex:0 0 auto}.status-dot{width:10px;height:10px;border-radius:999px;background:var(--bad);box-shadow:0 0 0 5px rgba(251,113,133,.10);flex:0 0 auto}.status-dot.ok{background:var(--ok);box-shadow:0 0 0 5px rgba(52,211,153,.10)}.status-dot.warn{background:var(--warn);box-shadow:0 0 0 5px rgba(251,191,36,.10)}.desc{min-height:42px;color:var(--muted);line-height:1.52;margin:12px 0;overflow-wrap:anywhere}.pill-row{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.pill{display:inline-flex;align-items:center;gap:5px;max-width:100%;border:1px solid var(--line);border-radius:999px;padding:5px 9px;font-size:12px;color:var(--soft);background:rgba(15,23,42,.72);overflow-wrap:anywhere}.pill.ok{border-color:rgba(52,211,153,.45);color:#bbf7d0}.pill.bad{border-color:rgba(251,113,133,.45);color:#fecdd3}.pill.warn{border-color:rgba(251,191,36,.45);color:#fde68a}.pill.public{color:#bfdbfe;border-color:rgba(96,165,250,.45)}.pill.protected{color:#ddd6fe;border-color:rgba(167,139,250,.45)}.pill.local-only{color:#bbf7d0;border-color:rgba(52,211,153,.45)}.pill.internal{color:#cbd5e1;border-color:rgba(148,163,184,.45)}.meta{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted);line-height:1.6;background:rgba(7,11,18,.44);border:1px dashed var(--line);border-radius:14px;padding:10px;margin:10px 0;overflow:auto}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}.btn{display:inline-flex;align-items:center;justify-content:center;min-height:var(--tap);padding:9px 13px;border:1px solid var(--line2);border-radius:13px;color:var(--soft);background:rgba(15,23,42,.78);cursor:pointer;touch-action:manipulation}.btn:hover{border-color:var(--accent);color:white}.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#07111f;font-weight:800}.btn.danger{border-color:rgba(251,113,133,.55);color:#fecdd3}.row{display:grid;grid-template-columns:1.5fr 1fr 1fr 1.2fr;gap:8px;border-bottom:1px solid var(--line);padding:10px 0;overflow-wrap:anywhere}.lines{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.55;color:#cbd5e1;background:rgba(7,11,18,.55);border:1px solid var(--line);border-radius:16px;padding:14px;max-height:620px;overflow:auto}.input{min-height:var(--tap);border:1px solid var(--line2);border-radius:12px;background:rgba(7,11,18,.65);color:var(--text);padding:0 12px;margin-right:8px}.empty{display:none;text-align:center;color:var(--muted);padding:30px}.quick{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.group-tabs{display:flex;gap:8px;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px}.group-tabs button{white-space:nowrap}.hidden{display:none!important}
@media(max-width:900px){.top{align-items:flex-start;flex-direction:column}.nav{overflow-x:auto;flex-wrap:nowrap;justify-content:flex-start;width:100%;padding:0 2px 6px;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}.nav a{flex-shrink:0}.hero-grid{grid-template-columns:1fr}.portal-hero{min-height:auto}.status-strip{grid-template-columns:repeat(2,minmax(0,1fr))}.grid,.pinned-grid{grid-template-columns:1fr}.row{grid-template-columns:1fr}.btn{min-height:46px}.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){:root{--radius:18px}.wrap{padding:calc(14px + env(safe-area-inset-top)) 12px calc(32px + env(safe-area-inset-bottom))}.top{margin:0 -2px 12px}.brand p{max-width:92vw}.hero{padding:18px}.headline{letter-spacing:-.055em}.subtitle{font-size:14px}.shortcut-grid{grid-template-columns:1fr;gap:10px;margin-top:18px}.shortcut{min-height:94px;padding:15px;display:grid;grid-template-columns:auto 1fr;column-gap:12px;align-items:center}.shortcut-icon{font-size:28px;grid-row:1/3}.shortcut-title{font-size:18px;margin:0}.shortcut-desc{margin:3px 22px 0 0}.status-strip{grid-template-columns:1fr}.advanced-links .btn,.actions .btn{flex:1 1 auto}.stats{grid-template-columns:1fr}.section-head{align-items:flex-start;flex-direction:column}.input{width:100%;margin:0 0 8px 0}.searchbox kbd{display:none}.panel{padding:15px}.card{padding:15px}.lines{max-height:68vh}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}.card:hover,.shortcut:hover{transform:none}}
'''


def page(title: str, active: str, body: str, extra_script: str = '') -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>{esc(title)} · {SERVER_DOMAIN}</title><style>{CSS}</style></head><body><div class="wrap"><div class="top"><div class="brand"><h1>{esc(title)}</h1><p>{SERVER_DOMAIN} · {SERVER_IP}</p></div><div class="nav">{nav(active)}</div></div>{body}</div>{extra_script}</body></html>''')


def status_class(ok: bool | str) -> str:
    if ok == 'degraded':
        return 'warn'
    if ok == 'offline':
        return 'bad'
    return 'ok' if ok else 'bad'


def exposure_pill(exposure: str) -> str:
    label = EXPOSURE_LABELS.get(exposure, exposure or '未知')
    return f"<span class='pill {esc(exposure)}'>🔒 {esc(label)}</span>"


def service_search_text(p: dict) -> str:
    values = [p.get('name'), p.get('unit'), p.get('group'), p.get('desc'), p.get('exposure'), str(p.get('port') or '')]
    values.extend(p.get('tags') or [])
    return ' '.join(str(x) for x in values if x).lower()


def project_card(p: dict, show_ops: bool = False, compact: bool = False) -> str:
    state = p.get('status') or ('online' if p.get('online') else 'offline')
    ok = state == 'online'
    dot_class = 'ok' if state == 'online' else 'warn' if state == 'degraded' else ''
    status = f"{p.get('status_label') or ('正常' if ok else '离线')} · {p.get('systemd', 'unknown')}"
    if p.get('http_status'):
        status += f" · HTTP {p.get('http_status')}"
    if p.get('elapsed_ms') is not None:
        status += f" · {p.get('elapsed_ms')}ms"
    actions = []
    if p.get('url'):
        actions.append(f"<a class='btn primary' href='{esc(p['url'])}' target='_blank' rel='noopener'>打开</a>")
    actions.append(f"<a class='btn' href='/apps#{quote(p['unit'])}'>详情</a>")
    if show_ops:
        actions.append(f"<a class='btn danger' href='/api/ops/restart/{quote(p['unit'])}?confirm=RESTART' target='_blank' onclick=\"return confirm('确认重启 {esc(p['unit'])}？')\">重启</a>")
    tag_html = ''.join(f"<span class='pill'>#{esc(tag)}</span>" for tag in (p.get('tags') or [])[:4])
    port_path = f"port={esc(p.get('port') or '-')}<br>path={esc(p.get('path') or '-')}"
    health = f"health={esc(p.get('health_url') or p.get('url') or '-')}"
    meta = '' if compact else f"<div class='meta'>unit={esc(p.get('unit'))}<br>{port_path}<br>{health}</div>"
    error = f"<div class='pill-row'><span class='pill bad'>⚠ {esc(p.get('http_error'))}</span></div>" if p.get('http_error') else ''
    return f'''<article class="card service-card" id="{esc(p.get('unit'))}" data-search="{esc(service_search_text(p))}" data-group="{esc(p.get('group'))}" data-exposure="{esc(p.get('exposure'))}">
      <div class="card-top"><div class="service-title"><span class="icon">{esc(p.get('icon','▫️'))}</span><span>{esc(p.get('name'))}</span></div><span class="status-dot {dot_class}" title="{esc(status)}"></span></div>
      <div class="desc">{esc(p.get('desc'))}</div>
      <div class="pill-row"><span class="pill">{esc(p.get('group'))}</span>{exposure_pill(p.get('exposure'))}<span class="pill {status_class(state)}">{esc(status)}</span></div>
      <div class="pill-row">{tag_html}</div>{error}{meta}<div class="actions">{''.join(actions)}</div>
    </article>'''


def render_groups(projects: list[dict], show_ops: bool = False) -> str:
    group_names = []
    for p in projects:
        if p['group'] not in group_names:
            group_names.append(p['group'])
    parts = []
    for group in group_names:
        cards = ''.join(project_card(p, show_ops=show_ops) for p in projects if p['group'] == group)
        parts.append(f"<section class='panel group-panel' data-group-panel='{esc(group)}'><div class='section-head'><h2 class='section-title'>{esc(group)}</h2><span class='muted small'>{sum(1 for p in projects if p['group']==group)} 个服务</span></div><div class='grid'>{cards}</div></section>")
    return ''.join(parts)


SEARCH_SCRIPT = r'''
<script>
const searchInput = document.getElementById('serviceSearch');
const empty = document.getElementById('emptyState');
function filterCards(){
  if(!searchInput) return;
  const q = searchInput.value.trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll('.service-card').forEach(card => {
    const ok = !q || card.dataset.search.includes(q);
    card.classList.toggle('hidden', !ok);
    if(ok) visible++;
  });
  document.querySelectorAll('.group-panel').forEach(panel => {
    const has = Array.from(panel.querySelectorAll('.service-card')).some(c => !c.classList.contains('hidden'));
    panel.classList.toggle('hidden', !has);
  });
  if(empty) empty.style.display = visible ? 'none' : 'block';
}
if(searchInput){
  searchInput.addEventListener('input', filterCards);
  window.addEventListener('keydown', e => {
    if(e.key === '/' && document.activeElement !== searchInput){ e.preventDefault(); searchInput.focus(); }
    if(e.key === 'Escape'){ searchInput.value=''; filterCards(); searchInput.blur(); }
  });
}
</script>
'''


@app.api_route('/favicon.ico', methods=['GET', 'HEAD'])
async def favicon():
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='16' fill='#0d1320'/><text x='32' y='42' font-size='34' text-anchor='middle'>🏠</text></svg>"""
    return Response(svg, media_type='image/svg+xml')


@app.api_route('/apple-touch-icon.png', methods=['GET', 'HEAD'])
@app.api_route('/apple-touch-icon-precomposed.png', methods=['GET', 'HEAD'])
async def apple_touch_icon():
    return await favicon()


@app.get('/api/health')
async def api_health():
    return {'ok': True, 'service': 'server-home', 'generated_at': datetime.now(timezone.utc).isoformat(), 'services_file': str(SERVICES_FILE), 'services': len(load_services())}


@app.get('/api/services')
async def api_services():
    return {'services': [public_service_view(p) for p in load_services()]}


@app.get('/api/state')
async def api_state():
    return await collect_state(full=True)


@app.get('/api/logs/{kind}')
async def api_logs(kind: str, lines: int = 120, q: str = ''):
    source = LOG_SOURCES.get(kind)
    if not source:
        return JSONResponse(status_code=404, content={'error': 'unknown log source'})
    lines = max(10, min(int(lines or 120), 800))
    rows = journal_tail(source['unit'], lines) if source.get('unit') else read_tail(source['path'], lines)
    if q:
        rows = [line for line in rows if q.lower() in line.lower()]
    return {'kind': kind, 'title': source['title'], 'lines': rows}


@app.get('/api/ops/restart/{unit}')
async def api_restart(unit: str, confirm: str = ''):
    return JSONResponse(status_code=403, content={'error': 'write operations disabled in ServerHub read-only phase', 'unit': unit})


@app.get('/api/status')
async def api_status_compat():
    return await collect_state(full=True)


@app.get('/api/service/{unit}/restart')
async def api_restart_compat(unit: str, confirm: str = ''):
    return await api_restart(unit, confirm)


@app.get('/', response_class=HTMLResponse)
async def home():
    frontend_index = SERVERHUB_FRONTEND / 'index.html'
    if frontend_index.exists():
        return FileResponse(frontend_index)
    state = await collect_fast_state()
    projects = sorted(state['projects'], key=lambda p: (not p.get('pinned'), EXPOSURE_ORDER.get(p.get('exposure'), 9), p.get('name') or ''))
    pinned = [p for p in projects if p.get('pinned')]
    public_count = sum(1 for p in projects if p.get('exposure') == 'public')
    by_name = {p.get('name'): p for p in projects}
    shortcuts = [
        {'name': 'Agent 控制台', 'desc': '对话、模型与 Agent 管理', 'icon': '🤖', 'url': 'http://agent.zmjjkkk.fun/'},
        {'name': 'System Monitor', 'label': '系统监控', 'desc': 'CPU / 内存 / 磁盘 / 网络', 'icon': '📈', 'url': 'http://monitor.zmjjkkk.fun/'},
        {'name': '知识库', 'desc': 'Obsidian 笔记与知识图谱', 'icon': '📚', 'url': 'http://notes.zmjjkkk.fun/'},
        {'name': '文件浏览', 'desc': '服务器文件快速浏览', 'icon': '🗂️', 'url': 'http://files.zmjjkkk.fun/'},
    ]
    shortcut_html = []
    status_html = []
    for item in shortcuts:
        project = by_name.get(item['name'], {})
        label = item.get('label') or item['name']
        state_name = project.get('status') or ('online' if project.get('online') else 'offline')
        dot_class = 'ok' if state_name == 'online' else 'warn' if state_name == 'degraded' else ''
        status_label = project.get('status_label') or ('正常' if state_name == 'online' else '离线')
        shortcut_html.append(f"""<a class='shortcut' href='{esc(item['url'])}' target='_blank' rel='noopener'>
          <span class='shortcut-icon'>{esc(item['icon'])}</span><span><div class='shortcut-title'>{esc(label)}</div><div class='shortcut-desc'>{esc(item['desc'])}</div></span>
        </a>""")
        status_html.append(f"<div class='status-chip'><span class='status-dot {dot_class}'></span><span>{esc(label)} · {esc(status_label)}</span></div>")
    body = f"""
    <section class='hero portal-hero'>
      <div class='hero-grid'>
        <div>
          <div class='eyebrow'>Server Portal</div>
          <h2 class='headline'>至秦的服务器</h2>
          <p class='subtitle'>常用 Web 服务放在第一屏；高级状态、日志和运维入口收在下面。手机、平板和桌面都可以直接打开。</p>
          <div class='shortcut-grid'>{''.join(shortcut_html)}</div>
          <div class='status-strip'>{''.join(status_html)}</div>
          <div class='advanced-links'><a class='btn' href='/apps'>项目地图</a><a class='btn' href='/logs'>日志</a><a class='btn' href='/ops'>运维</a><a class='btn' href='/ai'>AI 状态</a><a class='btn' href='/system'>系统详情</a></div>
        </div>
        <div class='stats'>
          <div class='stat'><div class='k'>服务总数</div><div class='v'>{esc(state['summary']['total'])}</div></div>
          <div class='stat'><div class='k'>服务正常</div><div class='v'>{esc(state['summary']['online'])}</div></div>
          <div class='stat'><div class='k'>公网入口</div><div class='v'>{esc(public_count)}</div></div>
          <div class='stat'><div class='k'>降级 / 离线</div><div class='v'>{esc(state['summary'].get('degraded', 0))} / {esc(state['summary'].get('offline', 0))}</div></div>
        </div>
      </div>
    </section>
    <section class='panel'><div class='section-head'><div><h2 class='section-title'>快速搜索服务</h2><div class='muted small'>搜索服务名、端口、标签或分组；适合需要进入高级页面时使用。</div></div></div><label class='searchbox'>🔎 <input id='serviceSearch' placeholder='搜索：openclaw / 9100 / memory / monitor / local-only ...' autocomplete='off'><kbd>/</kbd></label></section>
    <section class='panel'><div class='section-head'><h2 class='section-title'>置顶服务</h2><span class='muted small'>核心入口与 AI / 监控 / 知识服务</span></div><div class='grid pinned-grid'>{''.join(project_card(p, compact=True) for p in pinned)}</div></section>
    <div id='emptyState' class='empty panel'>没有匹配的服务</div>
    {render_groups(projects)}
    """
    return page('服务器门户', '总览', body, SEARCH_SCRIPT)


def render_alerts(alerts: dict) -> str:
    parts = []
    for kind, rows in alerts.items():
        content = '<div class="muted">暂无关键异常</div>' if not rows else f"<div class='lines'>{esc(chr(10).join(rows))}</div>"
        parts.append(f"<div class='card'><div class='service-title'><span class='icon'>🧾</span><span>{esc(kind)}</span></div>{content}<div class='actions'><a class='btn' href='/logs?kind={quote(kind)}'>查看日志</a></div></div>")
    return f"<div class='grid'>{''.join(parts)}</div>"


@app.get('/apps', response_class=HTMLResponse)
async def apps_page():
    state = await collect_fast_state()
    projects = sorted(state['projects'], key=lambda p: (p.get('group') or '', not p.get('pinned'), p.get('name') or ''))
    body = f'''<section class="hero"><div class="eyebrow">Service Map</div><h2 class="headline">项目地图</h2><p class="subtitle">按分组查看所有服务。搜索支持服务名、unit、端口、路径、标签与安全级别。</p><label class="searchbox">🔎 <input id="serviceSearch" placeholder="搜索服务 / unit / tag / 端口" autocomplete="off"><kbd>/</kbd></label></section><div id="emptyState" class="empty panel">没有匹配的服务</div>{render_groups(projects)}'''
    return page('项目地图', '项目地图', body, SEARCH_SCRIPT)


@app.get('/ai', response_class=HTMLResponse)
async def ai_page():
    state = await collect_state(full=True)
    h = state['hermes']
    overview = h['overview'] if isinstance(h['overview'], dict) else {}
    config = h['config'] if isinstance(h['config'], dict) else {}
    model = config.get('model', {}) if isinstance(config, dict) else {}
    sessions = h['sessions'].get('sessions', []) if isinstance(h['sessions'], dict) else []
    session_rows = ''.join(f"<div class='row'><div>{esc(s.get('title') or '未命名')}</div><div>{esc(s.get('model'))}</div><div>{esc(s.get('source'))}</div><div class='meta'>{esc(s.get('started_at'))}</div></div>" for s in sessions) or '<div class="muted">暂无会话</div>'
    cards = ''.join(project_card(p) for p in state['projects'] if 'ai' in [str(t).lower() for t in (p.get('tags') or [])] or 'AI' in p.get('group',''))
    body = f'''<section class="hero"><div class="stats"><div class="stat"><div class="k">Provider</div><div class="v">{esc(model.get('provider', 'n/a'))}</div></div><div class="stat"><div class="k">Model</div><div class="v">{esc(model.get('default', 'n/a'))}</div></div><div class="stat"><div class="k">Cache Hit</div><div class="v">{esc(overview.get('cache_hit_rate', 'n/a'))}%</div></div><div class="stat"><div class="k">Cost</div><div class="v">{esc(overview.get('total_cost', 'n/a'))}</div></div></div></section><section class="panel"><div class="section-head"><h2 class="section-title">AI 服务</h2></div><div class="grid">{cards}</div></section><section class="panel"><h2 class="section-title">最近会话</h2>{session_rows}<div class="actions"><a class="btn primary" href="http://agent.zmjjkkk.fun/" target="_blank">打开 Agent 控制台</a><a class="btn" href="/logs?kind=hermes">Hermes 日志</a><a class="btn" href="/logs?kind=openclaw">OpenClaw 日志</a></div></section>'''
    return page('AI 中枢', 'AI', body)


@app.get('/knowledge', response_class=HTMLResponse)
async def knowledge_page():
    state = await collect_state(full=True)
    status = state['knowledge']['status'] if isinstance(state['knowledge']['status'], dict) else {}
    memory = state['knowledge']['memory'] if isinstance(state['knowledge']['memory'], dict) else {}
    cards = ''.join(project_card(p) for p in state['projects'] if p['group'] == '知识与记忆')
    body = f'''<section class="hero"><div class="stats"><div class="stat"><div class="k">Knowledge</div><div class="v">{esc(status.get('status', status.get('ok', 'n/a')))}</div></div><div class="stat"><div class="k">Memory</div><div class="v">{esc(memory.get('status', memory.get('ok', 'n/a')))}</div></div><div class="stat"><div class="k">Vault</div><div class="v">Obsidian</div></div></div></section><section class="panel"><h2 class="section-title">知识与记忆服务</h2><div class="grid">{cards}</div><div class="actions"><a class="btn primary" href="http://notes.zmjjkkk.fun/" target="_blank">打开知识库</a><a class="btn" href="/api/state" target="_blank">查看记忆健康</a></div></section>'''
    return page('知识与记忆', '知识记忆', body)


@app.get('/system', response_class=HTMLResponse)
async def system_page():
    state = await collect_state(full=True)
    metrics = state['system']['metrics'] if isinstance(state['system']['metrics'], dict) else {}
    port_lines = [f":{p['port']} {p['name']} ({p['unit']}) [{p.get('exposure')}]" for p in state['projects'] if p.get('port')]
    cards = ''.join(project_card(p) for p in state['projects'] if p['group'] in ('系统监控', '入口与代理'))
    cpu_data = metrics.get('cpu') if isinstance(metrics.get('cpu'), dict) else {}
    mem_data = metrics.get('memory') if isinstance(metrics.get('memory'), dict) else {}
    disk_data = metrics.get('disk') if isinstance(metrics.get('disk'), dict) else {}
    cpu = metrics.get('cpu_percent') or cpu_data.get('percent') or 'n/a'
    mem = metrics.get('memory_percent') or mem_data.get('percent') or 'n/a'
    disk = metrics.get('disk_percent') or disk_data.get('percent') or 'n/a'
    body = f'''<section class="hero"><div class="stats"><div class="stat"><div class="k">CPU</div><div class="v">{esc(cpu)}%</div></div><div class="stat"><div class="k">Memory</div><div class="v">{esc(mem)}%</div></div><div class="stat"><div class="k">Disk</div><div class="v">{esc(disk)}%</div></div></div></section><section class="panel"><h2 class="section-title">系统服务</h2><div class="grid">{cards}</div></section><section class="panel"><h2 class="section-title">监听端口</h2><div class="lines">{esc(chr(10).join(port_lines) if port_lines else '暂无端口数据')}</div></section>'''
    return page('系统状态', '系统', body)


@app.get('/logs', response_class=HTMLResponse)
async def logs_page(kind: str = 'hermes', q: str = '', lines: int = 160):
    if kind not in LOG_SOURCES:
        kind = 'hermes'
    data = await api_logs(kind, lines, q)
    rows = data['lines'] if isinstance(data, dict) else []
    tabs = ''.join(f"<a class='btn {'primary' if key == kind else ''}' href='/logs?kind={quote(key)}'>{esc(src['title'])}</a>" for key, src in LOG_SOURCES.items())
    body = f'''<section class="panel"><h2 class="section-title">日志源</h2><div class="actions">{tabs}</div><form style="margin-top:12px" method="get"><input type="hidden" name="kind" value="{esc(kind)}"><input class="input" name="q" value="{esc(q)}" placeholder="搜索 error / 502 / 429 / Traceback"><input class="input" name="lines" value="{esc(lines)}" size="5"><button class="btn primary" type="submit">过滤</button></form></section><section class="panel"><h2 class="section-title">{esc(data['title'] if isinstance(data, dict) else kind)}</h2><div class="lines">{esc(chr(10).join(rows) if rows else '暂无日志')}</div></section>'''
    return page('统一日志', '日志', body)


@app.get('/ops', response_class=HTMLResponse)
async def ops_page():
    state = await collect_fast_state()
    rows = []
    for p in state['projects']:
        if p['unit'] not in OPS_UNITS:
            continue
        log_kind = 'hermes' if 'hermes' in p['unit'] else 'openclaw' if 'openclaw' in p['unit'] else 'nginx' if p['unit'] == 'nginx' else 'server-home'
        rows.append(f"<div class='card'><div class='service-title'><span class='icon'>{esc(p.get('icon','▫️'))}</span><span>{esc(p['name'])}</span></div><p><span class='pill {status_class(p['online'])}'>{esc(p['systemd'])}</span><span class='pill'>{esc(p['unit'])}</span>{exposure_pill(p.get('exposure'))}</p><div class='actions'><a class='btn' href='/logs?kind={log_kind}'>日志</a><a class='btn danger' href='/api/ops/restart/{quote(p['unit'])}?confirm=RESTART' target='_blank' onclick=\"return confirm('确认重启 {esc(p['unit'])}？')\">重启</a></div></div>")
    body = f"<section class='panel'><h2 class='section-title'>危险操作区</h2><p class='muted'>重启操作只放在这里，并要求 confirm=RESTART 与浏览器二次确认。</p><div class='grid'>{''.join(rows)}</div></section>"
    return page('运维操作', '运维', body)


@app.get('/service/{unit}', response_class=HTMLResponse)
async def service_compat(unit: str):
    return await apps_page()
