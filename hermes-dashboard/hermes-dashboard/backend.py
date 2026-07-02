"""
Hermes Dashboard Backend — 直接读取 state.db，提供 REST API
端口: 9100
"""

import json
import os
import sqlite3
import subprocess
import signal
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Config ──────────────────────────────────────────────────────
STATE_DB = os.path.expanduser("~/.hermes/state.db")
CONFIG_YAML = os.path.expanduser("~/.hermes/config.yaml")
OPENCLAW_CONFIG_JSON = os.path.expanduser(os.environ.get("OPENCLAW_CONFIG_JSON", "~/.openclaw/openclaw.json"))
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "openclaw")
HERMES_PYTHON = os.environ.get("HERMES_PYTHON", "/opt/hermes-agent/.venv/bin/python")
HERMES_GATEWAY_CMD = [HERMES_PYTHON, "-m", "hermes_cli.main", "gateway", "run"]
HERMES_LOG_PATH = os.path.expanduser(os.environ.get("HERMES_LOG_PATH", "~/.hermes/logs/gateway.log"))
OPENCLAW_LOG_PATH = os.path.expanduser(os.environ.get("OPENCLAW_LOG_PATH", "~/.openclaw/logs/gateway.log"))
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/root/obsidian-vault"))
STATIC_DIR = "/opt/hermes-dashboard/static"
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title="Hermes Dashboard", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── DB helpers ──────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_one(sql, params=()):
    db = get_db()
    try:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else {}
    finally:
        db.close()


def fetch_all(sql, params=()):
    db = get_db()
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


# ── API Routes ──────────────────────────────────────────────────

@app.get("/api/overview")
async def overview():
    """总览 KPI 数据"""
    row = fetch_one("""
        SELECT
            COUNT(*) AS sessions_count,
            COALESCE(SUM(message_count), 0) AS messages_count,
            COALESCE(SUM(input_tokens), 0) AS total_input,
            COALESCE(SUM(output_tokens), 0) AS total_output,
            COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read,
            COALESCE(SUM(cache_write_tokens), 0) AS total_cache_write,
            COALESCE(SUM(reasoning_tokens), 0) AS total_reasoning,
            COALESCE(SUM(estimated_cost_usd), 0) AS total_cost,
            COUNT(DISTINCT model) AS models_count,
            COUNT(DISTINCT source) AS platforms_count,
            COALESCE(CAST(julianday('now') - julianday(MIN(datetime(started_at, 'unixepoch'))) AS INTEGER), 0) AS active_days
        FROM sessions
    """)
    if not row:
        return {}
    row["total_tokens"] = row["total_input"] + row["total_output"]
    row["cache_hit_rate"] = round(
        row["total_cache_read"] * 100.0 / max(row["total_cache_read"] + row["total_input"], 1), 1
    )
    return row


@app.get("/api/token-trend")
async def token_trend(days: int = Query(7, ge=1, le=90)):
    """Token 趋势（按天聚合）"""
    rows = fetch_all("""
        SELECT
            date(datetime(started_at, 'unixepoch')) AS day,
            COALESCE(SUM(input_tokens), 0) AS input,
            COALESCE(SUM(output_tokens), 0) AS output,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write
        FROM sessions
        WHERE started_at >= strftime('%s', 'now', ?)
        GROUP BY day
        ORDER BY day ASC
    """, (f"-{days} days",))

    result = {"dates": [], "input": [], "output": [], "cache_read": [], "cache_write": []}
    for r in rows:
        result["dates"].append(r["day"])
        result["input"].append(r["input"])
        result["output"].append(r["output"])
        result["cache_read"].append(r["cache_read"])
        result["cache_write"].append(r["cache_write"])
    return result


@app.get("/api/cache-hit-rate")
async def cache_hit_rate(days: int = Query(7, ge=1, le=90)):
    """缓存命中率"""
    # Overall rate
    row = fetch_one(f"""
        SELECT
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
            COALESCE(SUM(input_tokens), 0) AS total_input
        FROM sessions
        WHERE started_at >= strftime('%s', 'now', '-{days} days')
    """)
    overall = round(row["cache_read"] * 100.0 / max(row["cache_read"] + row["total_input"], 1), 1) if row else 0

    # By model
    by_model = fetch_all(f"""
        SELECT
            model,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
            COALESCE(SUM(input_tokens), 0) AS total_input,
            ROUND(COALESCE(SUM(cache_read_tokens), 0) * 100.0 /
                NULLIF(COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(input_tokens), 0), 0), 1) AS rate
        FROM sessions
        WHERE model IS NOT NULL AND model != '' AND started_at >= strftime('%s', 'now', '-{days} days')
        GROUP BY model
        ORDER BY SUM(cache_read_tokens) DESC
    """)
    by_model = [{"model": r["model"], "rate": r["rate"] or 0, "cache_read": r["cache_read"], "total_input": r["total_input"]} for r in by_model]

    # Trend
    trend = fetch_all(f"""
        SELECT
            date(datetime(started_at, 'unixepoch')) AS day,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
            COALESCE(SUM(input_tokens), 0) AS total_input,
            ROUND(COALESCE(SUM(cache_read_tokens), 0) * 100.0 /
                NULLIF(COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(input_tokens), 0), 0), 1) AS rate
        FROM sessions
        WHERE started_at >= strftime('%s', 'now', '-{days} days')
        GROUP BY day ORDER BY day ASC
    """)
    trend_data = [{"date": r["day"], "rate": r["rate"] or 0} for r in trend]

    return {"overall_rate": overall, "by_model": by_model, "trend": trend_data}


@app.get("/api/cost-trend")
async def cost_trend(days: int = Query(7, ge=1, le=365)):
    """每日成本趋势"""
    rows = fetch_all(f"""
        SELECT
            date(datetime(started_at, 'unixepoch')) AS day,
            COALESCE(SUM(estimated_cost_usd), 0) AS cost,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COUNT(*) AS sessions
        FROM sessions
        WHERE started_at >= strftime('%s', 'now', '-{days} days')
        GROUP BY day
        ORDER BY day ASC
    """)
    return {
        "dates": [r["day"] for r in rows],
        "cost": [round(r["cost"] or 0, 6) for r in rows],
        "input_tokens": [r["input_tokens"] for r in rows],
        "output_tokens": [r["output_tokens"] for r in rows],
        "sessions": [r["sessions"] for r in rows],
    }


@app.get("/api/models-usage")
async def models_usage(days: int = Query(30, ge=1, le=365)):
    """按模型用量汇总"""
    rows = fetch_all(f"""
        SELECT
            model,
            COUNT(*) AS sessions,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
            MAX(datetime(started_at, 'unixepoch')) AS last_used
        FROM sessions
        WHERE model IS NOT NULL AND model != '' AND started_at >= strftime('%s', 'now', '-{days} days')
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """)
    return rows


@app.get("/api/platforms-usage")
async def platforms_usage(days: int = Query(30, ge=1, le=365)):
    """按平台用量汇总"""
    rows = fetch_all(f"""
        SELECT
            source,
            COUNT(*) AS sessions,
            COALESCE(SUM(message_count), 0) AS messages,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM sessions
        WHERE source IS NOT NULL AND started_at >= strftime('%s', 'now', '-{days} days')
        GROUP BY source
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """)
    return rows


@app.get("/api/sessions")
async def sessions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """会话列表"""
    rows = fetch_all("""
        SELECT id, title, source, model,
               datetime(started_at, 'unixepoch') AS started_at,
               message_count, input_tokens, output_tokens,
               cache_read_tokens, estimated_cost_usd
        FROM sessions
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))

    total = fetch_one("SELECT COUNT(*) AS cnt FROM sessions")
    return {"sessions": rows, "total": total.get("cnt", 0)}


@app.get("/api/config")
async def config():
    """读取 config.yaml"""
    try:
        with open(CONFIG_YAML) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {"error": "config.yaml not found"}

    return {
        "model": {
            "default": cfg.get("model", {}).get("default", ""),
            "provider": cfg.get("model", {}).get("provider", ""),
            "base_url": cfg.get("model", {}).get("base_url", ""),
        },
        "gateway": {
            "platforms": list(cfg.get("gateway", {}).get("platforms", {}).keys()) if cfg.get("gateway", {}).get("platforms") else [],
        },
        "agent": {
            "max_turns": cfg.get("agent", {}).get("max_turns", ""),
        },
        "raw": cfg,
    }


@app.put("/api/config/model")
async def config_model_update(body: dict):
    """更新 model 段的配置并写回 config.yaml"""
    try:
        with open(CONFIG_YAML) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Cannot read config.yaml"})

    allowed = {"model", "provider", "base_url"}
    updates = {k: v for k, v in body.items() if k in allowed and v is not None}

    if not updates:
        return JSONResponse(status_code=400, content={"error": "No valid fields to update"})

    # Update the model section
    model = cfg.setdefault("model", {})
    for k, v in updates.items():
        model[k] = str(v)

    # Write back
    try:
        with open(CONFIG_YAML, "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Cannot write config.yaml: {e}"})

    return {"status": "ok", "updated": updates}


# ── Provider CRUD helpers ────────────────────────────────────────

def _read_config() -> dict:
    with open(CONFIG_YAML) as f:
        return yaml.safe_load(f) or {}

def _write_config(cfg: dict):
    with open(CONFIG_YAML, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

def _mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]


def _run_cmd(cmd: list[str], timeout: int = 5) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _pgrep(pattern: str) -> list[int]:
    try:
        result = _run_cmd(["pgrep", "-f", pattern], timeout=3)
        return [int(x) for x in result.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _port_pid(port: int, name_hint: str = "") -> int | None:
    try:
        result = _run_cmd(["ss", "-ltnp", f"( sport = :{port} )"], timeout=3)
        if name_hint and name_hint not in result.stdout:
            return None
        import re
        match = re.search(r"pid=(\d+)", result.stdout)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _tail_log(path: str, lines: int = 80) -> list[str]:
    try:
        if not os.path.exists(path):
            return []
        result = _run_cmd(["tail", "-n", str(lines), path], timeout=3)
        return result.stdout.split("\n")
    except Exception:
        return []


def _kill_pattern(pattern: str) -> None:
    for pid in _pgrep(pattern):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(1)
    for pid in _pgrep(pattern):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass



def _model_id_from_item(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("id") or item.get("name") or item.get("model")
        return str(value) if value else ""
    return ""


def _extract_model_ids(body) -> list[str]:
    """Extract model ids from OpenAI/OneAPI/Ollama/custom model-list shapes."""
    raw_items = []
    if isinstance(body, dict):
        for key in ("data", "models", "model", "items", "results"):
            value = body.get(key)
            if isinstance(value, list):
                raw_items = value
                break
        if not raw_items and isinstance(body.get("data"), dict):
            nested = body["data"].get("models") or body["data"].get("items")
            if isinstance(nested, list):
                raw_items = nested
    elif isinstance(body, list):
        raw_items = body
    ids = []
    seen = set()
    for item in raw_items:
        mid = _model_id_from_item(item).strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    return ids


async def _fetch_models_from_provider(base_url: str, api_key: str = "") -> dict:
    import httpx
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return {"status": "error", "connected": False, "error": "Base URL not configured"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    candidates = []
    if base_url.endswith("/v1"):
        candidates = [f"{base_url}/models", f"{base_url[:-3]}/api/tags"]
    else:
        candidates = [f"{base_url}/models", f"{base_url}/v1/models", f"{base_url}/api/tags"]
    last = {"status": "error", "connected": False, "error": "not tried"}
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=headers)
                raw_text = resp.text[:2000] if resp.text else ""
                if resp.status_code == 401:
                    last = {"status": "error", "connected": False, "http_status": 401, "endpoint": url, "error": "认证失败 — API Key 无效"}
                    continue
                if not (200 <= resp.status_code < 300):
                    last = {"status": "error", "connected": resp.status_code < 500, "http_status": resp.status_code, "endpoint": url, "error": f"HTTP {resp.status_code}"}
                    continue
                try:
                    body = resp.json()
                except Exception:
                    body = None
                models = _extract_model_ids(body)
                return {
                    "status": "ok",
                    "connected": True,
                    "http_status": resp.status_code,
                    "endpoint": url,
                    "models": models,
                    "models_count": len(models),
                    "raw_sample": None if models else raw_text,
                }
            except Exception as exc:
                last = {"status": "error", "connected": False, "endpoint": url, "error": str(exc)}
    return last


def _read_hermes_summary() -> dict:
    cfg = _read_config()
    model_cfg = cfg.get("model", {}) or {}
    providers = cfg.get("providers", {}) or {}
    return {
        "config_path": CONFIG_YAML,
        "state_db": STATE_DB,
        "model": model_cfg.get("default", model_cfg.get("model", "")),
        "provider": model_cfg.get("provider", ""),
        "base_url": model_cfg.get("base_url", ""),
        "providers_count": len(providers),
        "platforms": list((cfg.get("gateway", {}) or {}).get("platforms", {}).keys()),
    }


def _read_openclaw_summary() -> dict:
    cfg = _read_openclaw_config()
    providers = ((cfg.get("models", {}) or {}).get("providers", {}) or {})
    channels = cfg.get("channels", {}) or {}
    gateway = cfg.get("gateway", {}) or {}
    return {
        "config_path": OPENCLAW_CONFIG_JSON,
        "model": ((cfg.get("agents", {}) or {}).get("defaults", {}) or {}).get("model", ""),
        "providers_count": len(providers),
        "providers": list(providers.keys()),
        "channels": [name for name, value in channels.items() if not isinstance(value, dict) or value.get("enabled", True)],
        "port": gateway.get("port", 18789),
        "mode": gateway.get("mode", "local"),
        "auth_mode": gateway.get("auth", {}).get("mode", ""),
    }

@app.get("/api/config/providers")
async def config_providers():
    """列出所有已配置的 Provider（API Key 脱敏）"""
    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    result = {}
    for name, pcfg in providers.items():
        if isinstance(pcfg, dict):
            api_key = pcfg.get("apiKey", pcfg.get("api_key", ""))
            base_url = pcfg.get("baseUrl", pcfg.get("base_url", ""))
            result[name] = {
                "apiKey": api_key,
                "apiKeyMasked": _mask_key(api_key),
                "baseUrl": base_url,
                "enabled": pcfg.get("enabled", True),
                "models": pcfg.get("models", []),
            }
    return {
        "providers": result,
        "activeProvider": cfg.get("model", {}).get("provider", ""),
    }


@app.post("/api/config/providers")
async def config_provider_add(body: dict):
    """新增一个 Provider（写入 api_key + base_url）"""
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Provider name is required"})

    cfg = _read_config()
    providers = cfg.setdefault("providers", {})
    providers[name] = {
        "apiKey": (body.get("apiKey") or body.get("api_key") or "").strip(),
        "baseUrl": (body.get("baseUrl") or body.get("base_url") or "").strip(),
    }
    _write_config(cfg)
    return {"status": "ok", "name": name}


@app.put("/api/config/providers/{name}")
async def config_provider_update(name: str, body: dict):
    """更新 Provider 的 api_key 和/或 base_url"""
    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    if name not in providers or not isinstance(providers[name], dict):
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})

    for field in ("apiKey", "api_key", "baseUrl", "base_url"):
        if body.get(field) is not None:
            target = "apiKey" if field.startswith("api") else "baseUrl"
            providers[name][target] = str(body[field]).strip()
    _write_config(cfg)
    return {"status": "ok", "name": name}


@app.delete("/api/config/providers/{name}")
async def config_provider_delete(name: str):
    """删除一个 Provider"""
    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    if name not in providers:
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})

    del providers[name]
    # If the deleted provider was the active one, clear the reference
    if cfg.get("model", {}).get("provider") == name:
        cfg["model"]["provider"] = ""
    _write_config(cfg)
    return {"status": "ok", "name": name}


@app.put("/api/config/switch-provider")
async def config_switch_provider(body: dict):
    """切换当前活跃的 Provider"""
    provider = (body.get("provider") or "").strip()
    if not provider:
        return JSONResponse(status_code=400, content={"error": "Provider name is required"})

    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    if provider not in providers:
        return JSONResponse(status_code=404, content={"error": f"Provider '{provider}' not found"})

    # Update the model section to point to this provider
    pcfg = providers[provider]
    model = cfg.setdefault("model", {})
    model["provider"] = provider
    base_url = pcfg.get("base_url") or pcfg.get("baseUrl") or ""
    if base_url:
        model["base_url"] = base_url
    _write_config(cfg)
    return {
        "status": "ok",
        "active_provider": provider,
        "base_url": base_url,
    }


@app.post("/api/config/providers/{name}/test")
async def config_provider_test(name: str):
    """测试 Provider 连接并获取模型列表。"""
    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    pcfg = providers.get(name)
    if not pcfg or not isinstance(pcfg, dict):
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})
    api_key = pcfg.get("apiKey", pcfg.get("api_key", ""))
    base_url = pcfg.get("baseUrl", pcfg.get("base_url", ""))
    result = await _fetch_models_from_provider(base_url, api_key)
    if result.get("status") == "ok" and result.get("models"):
        pcfg["models"] = result["models"]
        _write_config(cfg)
    return result


@app.put("/api/config/providers/{name}/toggle")
async def config_provider_toggle(name: str):
    """切换 Provider 启用/禁用状态"""
    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    if name not in providers or not isinstance(providers[name], dict):
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})

    current = providers[name].get("enabled", True)
    providers[name]["enabled"] = not current
    _write_config(cfg)
    return {"status": "ok", "name": name, "enabled": not current}


@app.put("/api/config/providers/{name}/models")
async def config_provider_models(name: str, body: dict):
    """设置 Provider 的模型白名单（空列表 = 全部可用）"""
    cfg = _read_config()
    providers = cfg.get("providers", {}) or {}
    if name not in providers or not isinstance(providers[name], dict):
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})

    models = body.get("models", [])
    if not isinstance(models, list):
        return JSONResponse(status_code=400, content={"error": "models must be a list"})

    providers[name]["models"] = models
    _write_config(cfg)
    return {"status": "ok", "name": name, "models": models, "count": len(models)}


@app.get("/api/status")
async def status():
    """系统状态 — 真实数据"""
    # Gateway status
    import subprocess
    gw_running = False
    try:
        result = subprocess.run(["pgrep", "-f", "hermes.*gateway"], capture_output=True, text=True, timeout=3)
        gw_running = bool(result.stdout.strip())
    except Exception:
        pass

    # Active sessions (last 1 hour — 10min is too narrow)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    active = fetch_one("SELECT COUNT(*) AS cnt FROM sessions WHERE started_at >= ?", (cutoff,))

    # Real platforms from config (top-level platform keys)
    KNOWN_PLATFORM_KEYS = ["telegram", "discord", "qqbot", "whatsapp", "mattermost", "matrix",
                            "slack", "wechat", "x_search", "line", "signal"]
    platforms = []
    try:
        cfg = _read_config()
        detected = set()
        for key in KNOWN_PLATFORM_KEYS:
            pcfg = cfg.get(key)
            if pcfg is not None:
                detected.add(key)
                if isinstance(pcfg, dict):
                    enabled = pcfg.get("enabled", True)
                else:
                    enabled = bool(pcfg)
                platforms.append({"name": key, "enabled": enabled})
        # Also check gateway platforms if any
        gw_plats = cfg.get("gateway", {}).get("platforms", {})
        for name, pcfg in gw_plats.items():
            if name not in detected:
                platforms.append({
                    "name": name,
                    "enabled": pcfg.get("enabled", False) if isinstance(pcfg, dict) else True,
                })
        # Additional: detect platforms from active session sources
        rows = fetch_all("SELECT DISTINCT source FROM sessions WHERE source IS NOT NULL AND source != ''")
        for r in rows:
            src = r.get("source", "").strip()
            if src and src not in detected and src not in ("cron", "subagent"):
                platforms.append({"name": src, "enabled": True, "from_session": True})
    except Exception:
        pass

    # Cron jobs count — read from jobs.json
    cron_count = 0
    cron_jobs_list = []
    cron_file = os.path.expanduser("~/.hermes/cron/jobs.json")
    try:
        if os.path.isfile(cron_file):
            import json as _json
            with open(cron_file) as f:
                jdata = _json.load(f)
            all_jobs = jdata.get("jobs", []) if isinstance(jdata, dict) else []
            enabled_jobs = [j for j in all_jobs if isinstance(j, dict) and j.get("enabled", True)]
            cron_count = len(enabled_jobs)
            for j in enabled_jobs[:20]:  # top 20
                sched = j.get("schedule", "")
                if isinstance(sched, dict):
                    sched = sched.get("display", sched.get("expr", str(sched)))
                cron_jobs_list.append({
                    "name": j.get("name", j.get("id", "")),
                    "schedule": sched,
                    "enabled": True,
                })
    except Exception:
        pass

    # Actual sessions count
    total_sessions = fetch_one("SELECT COUNT(*) AS cnt FROM sessions")

    # System uptime
    uptime_seconds = 0
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = int(float(f.readline().split()[0]))
    except Exception:
        pass

    return {
        "gateway_running": gw_running,
        "platforms": platforms,
        "active_sessions": active.get("cnt", 0),
        "cron_jobs_count": cron_count,
        "cron_jobs": cron_jobs_list,
        "total_sessions": total_sessions.get("cnt", 0),
        "uptime": uptime_seconds,
    }



def _http_json_health(url: str, timeout: float = 1.5) -> dict:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data["reachable"] = True
        return data
    except Exception as exc:
        return {"ok": False, "reachable": False, "error": str(exc)[:160]}

def _ollama_summary() -> dict:
    try:
        req = urllib.request.Request("http://[REDACTED_IP]:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name") or m.get("model") for m in data.get("models", [])]
        return {"reachable": True, "models": models, "has_qwen": any("qwen" in str(m).lower() for m in models), "has_embed": any("nomic-embed" in str(m).lower() for m in models)}
    except Exception as exc:
        return {"reachable": False, "models": [], "has_qwen": False, "has_embed": False, "error": str(exc)[:160]}


@app.get("/api/agents")
async def agents_overview():
    """服务器智能体总览：Hermes + OpenClaw 并列状态。"""
    hermes_pids = _pgrep("hermes_cli.main gateway")
    openclaw_port = _read_openclaw_summary().get("port", 18789)
    openclaw_pid = _port_pid(int(openclaw_port), "openclaw") or ( _pgrep("openclaw.*gateway") or [None] )[0]
    hermes_summary = _read_hermes_summary()
    openclaw_summary = _read_openclaw_summary()
    shared_memory = _http_json_health("http://[REDACTED_IP]:9400/health")
    ollama = _ollama_summary()
    return {
        "agents": [
            {
                "id": "hermes",
                "name": "Hermes Agent",
                "description": "主 Hermes 助手、QQ/本地入口与定时任务运行环境",
                "running": bool(hermes_pids),
                "pid": hermes_pids[0] if hermes_pids else None,
                "kind": "primary-agent",
                "config_path": hermes_summary["config_path"],
                "log_path": HERMES_LOG_PATH,
                "model": hermes_summary.get("model"),
                "provider": hermes_summary.get("provider"),
                "providers_count": hermes_summary.get("providers_count", 0),
                "platforms": hermes_summary.get("platforms", []),
                "metrics": {
                    "sessions": fetch_one("SELECT COUNT(*) AS cnt FROM sessions").get("cnt", 0),
                    "active_sessions_1h": fetch_one("SELECT COUNT(*) AS cnt FROM sessions WHERE started_at >= ?", ((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp(),)).get("cnt", 0),
                },
            },
            {
                "id": "openclaw",
                "name": "OpenClaw",
                "description": "独立 OpenClaw 网关与 QQ Bot 智能体",
                "running": openclaw_pid is not None,
                "pid": openclaw_pid,
                "kind": "secondary-agent",
                "port": openclaw_summary.get("port", 18789),
                "mode": openclaw_summary.get("mode", "local"),
                "config_path": openclaw_summary["config_path"],
                "log_path": OPENCLAW_LOG_PATH,
                "model": openclaw_summary.get("model"),
                "providers_count": openclaw_summary.get("providers_count", 0),
                "providers": openclaw_summary.get("providers", []),
                "platforms": openclaw_summary.get("channels", []),
            },
        ],
        "server": {
            "dashboard_port": 9100,
            "monitor_port": 9000,
            "openclaw_port": openclaw_summary.get("port", 18789),
            "shared_memory": shared_memory,
            "ollama": ollama,
            "services": {
                "dashboard": {"running": bool(_port_pid(9100))},
                "monitor": {"running": bool(_port_pid(9000))},
                "shared_memory": {"running": bool(_port_pid(9400))},
                "openclaw": {"running": openclaw_pid is not None},
            },
        },
    }


@app.get("/api/agents/{agent_id}/logs")
async def agent_logs(agent_id: str, lines: int = 80):
    if agent_id == "hermes":
        return {"logs": _tail_log(HERMES_LOG_PATH, lines), "path": HERMES_LOG_PATH}
    if agent_id == "openclaw":
        return {"logs": _tail_log(OPENCLAW_LOG_PATH, lines), "path": OPENCLAW_LOG_PATH}
    return JSONResponse(status_code=404, content={"error": "unknown agent"})


@app.post("/api/agents/{agent_id}/restart")
async def agent_restart(agent_id: str):
    if agent_id == "hermes":
        _kill_pattern("hermes_cli.main gateway")
        os.makedirs(os.path.dirname(HERMES_LOG_PATH), exist_ok=True)
        with open(HERMES_LOG_PATH, "a") as log_f:
            subprocess.Popen(HERMES_GATEWAY_CMD, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)
        return {"status": "ok", "message": "Hermes Gateway 重启中"}
    if agent_id == "openclaw":
        return await openclaw_restart()
    return JSONResponse(status_code=404, content={"error": "unknown agent"})


@app.post("/api/agents/{agent_id}/stop")
async def agent_stop(agent_id: str):
    if agent_id == "hermes":
        _kill_pattern("hermes_cli.main gateway")
        return {"status": "ok", "message": "Hermes Gateway 已停止"}
    if agent_id == "openclaw":
        return await openclaw_stop()
    return JSONResponse(status_code=404, content={"error": "unknown agent"})


# ── OpenClaw Integration ──────────────────────────────────────────

def _read_openclaw_config() -> dict:
    """读取 ~/.openclaw/openclaw.json"""
    try:
        with open(OPENCLAW_CONFIG_JSON) as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def _write_openclaw_config(cfg: dict) -> bool:
    """写回 ~/.openclaw/openclaw.json"""
    try:
        os.makedirs(os.path.dirname(OPENCLAW_CONFIG_JSON), exist_ok=True)
        with open(OPENCLAW_CONFIG_JSON, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

def _mask_secret(s: str) -> str:
    """掩码敏感字段"""
    if not s or len(s) <= 8:
        return "***"
    return s[:4] + "..." + s[-4:]


@app.get("/api/openclaw/config")
async def openclaw_config():
    """读取 OpenClaw 完整配置（敏感字段脱敏）"""
    cfg = _read_openclaw_config()
    if not cfg:
        return {"configured": False, "config": {}}

    result = {
        "configured": True,
        "config": {
            "gateway": cfg.get("gateway", {}),
            "plugins": cfg.get("plugins", {}),
            "channels": cfg.get("channels", {}),
            "agents": cfg.get("agents", {}),
            "models": cfg.get("models", {}),
        },
    }

    # 脱敏密文
    providers = result["config"]["models"].get("providers", {})
    for pname, pcfg in providers.items():
        if isinstance(pcfg, dict) and pcfg.get("apiKey"):
            pcfg["apiKey"] = _mask_secret(pcfg["apiKey"])
            pcfg["_hasKey"] = True

    channels = result["config"].get("channels", {})
    for cname, ccfg in channels.items():
        if isinstance(ccfg, dict) and ccfg.get("clientSecret"):
            ccfg["clientSecret"] = _mask_secret(ccfg["clientSecret"])
            ccfg["_hasSecret"] = True

    return result


@app.put("/api/openclaw/config")
async def openclaw_config_update(body: dict):
    """更新 OpenClaw 子段（只改传了的字段）"""
    cfg = _read_openclaw_config()
    if not cfg:
        cfg = {"gateway": {"mode": "local", "port": 18789}}

    allowed_sections = {"gateway", "plugins", "channels", "models", "agents"}
    unchanged = {}

    for section in allowed_sections:
        if section in body:
            cfg[section] = body[section]
            unchanged[section] = True

    if not unchanged:
        return {"status": "error", "error": "没有有效的配置段"}

    ok = _write_openclaw_config(cfg)
    if not ok:
        return {"status": "error", "error": "写入配置文件失败"}

    return {"status": "ok", "updated": list(unchanged.keys())}


@app.get("/api/openclaw/status")
async def openclaw_status():
    """OpenClaw Gateway 运行状态"""
    port_open = False
    pid = None
    try:
        r = subprocess.run(
            ["ss", "-ltnp", "( sport = :18789 )"],
            capture_output=True, text=True, timeout=3
        )
        # 标准输出中如果有 openclaw 的行说明在运行
        if "openclaw" in r.stdout:
            port_open = True
            for line in r.stdout.split("\n"):
                if "openclaw" in line:
                    # 提取 PID
                    import re
                    m = re.search(r'pid=(\d+)', line)
                    if m:
                        pid = int(m.group(1))
                    break
    except Exception:
        pass

    # 也查进程
    if not pid:
        try:
            r = subprocess.run(
                ["pgrep", "-f", "openclaw.*gateway"],
                capture_output=True, text=True, timeout=3
            )
            if r.stdout.strip():
                pid = int(r.stdout.strip().split("\n")[0])
                port_open = True
        except Exception:
            pass

    return {
        "running": pid is not None,
        "pid": pid,
        "port": 18789,
        "port_open": port_open,
    }


@app.post("/api/openclaw/restart")
async def openclaw_restart():
    """重启 OpenClaw Gateway"""
    try:
        # 先杀旧进程
        subprocess.run(
            ["pkill", "-f", "openclaw.*gateway"],
            capture_output=True, timeout=5
        )
        # 等端口释放
        time.sleep(2)
    except Exception:
        pass

    # 启动新进程（后台）
    try:
        log_path = os.path.expanduser("~/.openclaw/logs/gateway.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as log_f:
            subprocess.Popen(
                [OPENCLAW_CLI, "gateway", "--port", "18789", "--verbose"],
                stdout=log_f, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return {"status": "ok", "message": "OpenClaw Gateway 重启中..."}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/openclaw/stop")
async def openclaw_stop():
    """停止 OpenClaw Gateway"""
    try:
        subprocess.run(
            ["pkill", "-f", "openclaw.*gateway"],
            capture_output=True, timeout=5
        )
        time.sleep(1)
        return {"status": "ok", "message": "OpenClaw Gateway 已停止"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/openclaw/logs")
async def openclaw_logs(lines: int = 50):
    """获取 OpenClaw Gateway 日志尾部"""
    log_path = os.path.expanduser("~/.openclaw/logs/gateway.log")
    try:
        r = subprocess.run(
            ["tail", "-n", str(lines), log_path],
            capture_output=True, text=True, timeout=3
        )
        return {"logs": r.stdout.split("\n")}
    except Exception as e:
        return {"logs": [], "error": str(e)}



@app.post("/api/openclaw/config/providers/{name}/test")
async def openclaw_config_provider_test(name: str):
    """测试 OpenClaw Provider 连接并把可用模型写回配置。"""
    cfg = _read_openclaw_config()
    providers = ((cfg.get("models", {}) or {}).get("providers", {}) or {})
    pcfg = providers.get(name)
    if not pcfg or not isinstance(pcfg, dict):
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})
    api_key = pcfg.get("apiKey", pcfg.get("api_key", ""))
    base_url = pcfg.get("baseUrl", pcfg.get("base_url", ""))
    result = await _fetch_models_from_provider(base_url, api_key)
    if result.get("status") == "ok" and result.get("models"):
        pcfg["models"] = [{"id": mid, "name": mid} for mid in result["models"]]
        _write_openclaw_config(cfg)
    return result

# ── Knowledge Vault / Obsidian ───────────────────────────────────

def _vault_file_info(path: Path) -> dict:
    rel = path.relative_to(OBSIDIAN_VAULT).as_posix()
    stat = path.stat()
    try:
        text = path.read_text(errors="replace")
    except Exception:
        text = ""
    title = path.stem
    for line in text.splitlines()[:20]:
        if line.startswith("# "):
            title = line.lstrip("# ").strip() or title
            break
    category = rel.split("/", 1)[0] if "/" in rel else "root"
    return {
        "path": rel,
        "title": title,
        "category": category,
        "bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "preview": text[:240].replace("\n", " ").strip(),
    }


@app.get("/api/knowledge/files")
async def knowledge_files(q: str = "", category: str = "", limit: int = Query(200, ge=1, le=1000)):
    """列出服务器本地 Obsidian vault 中的 Markdown 文件。"""
    if not OBSIDIAN_VAULT.exists():
        return {"vault": str(OBSIDIAN_VAULT), "files": [], "count": 0, "categories": []}
    q_lower = (q or "").lower().strip()
    files = []
    categories = set()
    for path in OBSIDIAN_VAULT.rglob("*.md"):
        if not path.is_file():
            continue
        info = _vault_file_info(path)
        categories.add(info["category"])
        if category and info["category"] != category:
            continue
        hay = f"{info['path']} {info['title']} {info['preview']}".lower()
        if q_lower and q_lower not in hay:
            continue
        files.append(info)
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"vault": str(OBSIDIAN_VAULT), "files": files[:limit], "count": len(files), "categories": sorted(categories)}


@app.get("/api/knowledge/file")
async def knowledge_file(path: str):
    """读取 vault 内单个 Markdown 文件。"""
    target = (OBSIDIAN_VAULT / path).resolve()
    try:
        target.relative_to(OBSIDIAN_VAULT.resolve())
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid path"})
    if not target.exists() or target.suffix.lower() != ".md":
        return JSONResponse(status_code=404, content={"error": "file not found"})
    text = target.read_text(errors="replace")
    info = _vault_file_info(target)
    return {"file": info, "content": text}


@app.get("/api/knowledge/graph")
async def knowledge_graph(limit: int = Query(500, ge=1, le=2000)):
    """生成 vault Markdown wikilink/markdown-link 知识图谱。"""
    import re
    if not OBSIDIAN_VAULT.exists():
        return {"nodes": [], "links": []}
    md_files = list(OBSIDIAN_VAULT.rglob("*.md"))[:limit]
    by_stem = {p.stem: p.relative_to(OBSIDIAN_VAULT).as_posix() for p in md_files}
    nodes = []
    links = []
    indeg = {}
    for path in md_files:
        rel = path.relative_to(OBSIDIAN_VAULT).as_posix()
        category = rel.split("/", 1)[0] if "/" in rel else "root"
        nodes.append({"id": rel, "name": path.stem, "category": category, "symbolSize": 12})
    node_ids = {n["id"] for n in nodes}
    for path in md_files:
        rel = path.relative_to(OBSIDIAN_VAULT).as_posix()
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        refs = []
        refs += re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", text)
        refs += re.findall(r"\[[^\]]+\]\(([^)]+\.md)\)", text)
        for ref in refs:
            ref = ref.strip().replace("%20", " ")
            target = by_stem.get(Path(ref).stem)
            if not target:
                candidate = (path.parent / ref).resolve()
                try:
                    target = candidate.relative_to(OBSIDIAN_VAULT.resolve()).as_posix()
                except Exception:
                    target = None
            if target and target in node_ids and target != rel:
                links.append({"source": rel, "target": target})
                indeg[target] = indeg.get(target, 0) + 1
    for node in nodes:
        node["symbolSize"] = min(34, 10 + indeg.get(node["id"], 0) * 2)
    categories = sorted({n["category"] for n in nodes})
    return {"nodes": nodes, "links": links, "categories": categories, "vault": str(OBSIDIAN_VAULT)}


# ── Static & Root ───────────────────────────────────────────────
try:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
except Exception:
    pass


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ── Entrypoint ──────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="[REDACTED_IP]", port=9100, log_level="info")
