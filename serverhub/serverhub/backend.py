from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SERVICES_FILE = APP_DIR / "services.yml"
DOCS_DIR = APP_DIR / "docs"
SERVER_DOMAIN = "server.zmjjkkk.fun"
SERVER_IP = "[REDACTED_IP]"
BACKUP_ROOT = Path("/root/serverhub-migration/backup")
OPENCLAW_HEALTH = "http://[REDACTED_IP]:18789/health"
HERMES_DASHBOARD = "http://[REDACTED_IP]:9100"
MEMORY_HEALTH = "http://[REDACTED_IP]:9400/health"

app = FastAPI(title="ServerHub", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], timeout: int = 6) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def load_services() -> list[dict[str, Any]]:
    raw = yaml.safe_load(SERVICES_FILE.read_text(encoding="utf-8")) if SERVICES_FILE.exists() else {}
    services = raw.get("services", []) if isinstance(raw, dict) else []
    normalized = []
    for item in services:
        svc = dict(item or {})
        svc.setdefault("id", re.sub(r"[^a-z0-9-]+", "-", str(svc.get("name", "service")).lower()).strip("-"))
        svc.setdefault("name", svc["id"])
        svc.setdefault("unit", None)
        svc.setdefault("url", None)
        svc.setdefault("local_url", None)
        svc.setdefault("port", None)
        svc.setdefault("path", "-")
        svc.setdefault("desc", "")
        svc.setdefault("group", "其他")
        svc.setdefault("exposure", "internal")
        svc.setdefault("pinned", False)
        svc.setdefault("tags", [])
        if isinstance(svc["tags"], str):
            svc["tags"] = [x.strip() for x in svc["tags"].split(",") if x.strip()]
        normalized.append(svc)
    return normalized


def unit_active(unit: str | None) -> str:
    if not unit:
        return "unknown"
    code, out, _ = run(["systemctl", "is-active", unit], timeout=4)
    return out if code == 0 and out else "inactive"


def unit_enabled(unit: str | None) -> str:
    if not unit:
        return "unknown"
    code, out, _ = run(["systemctl", "is-enabled", unit], timeout=4)
    return out if code == 0 and out else "unknown"


def port_listening(port: int | None) -> bool | None:
    if not port:
        return None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex(("[REDACTED_IP]", int(port))) == 0


async def http_probe(url: str | None, timeout: float = 1.4) -> dict[str, Any]:
    if not url:
        return {"ok": None, "status_code": None, "elapsed_ms": None, "error": None}
    start = time.perf_counter()
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
        elapsed = int((time.perf_counter() - start) * 1000)
        ok = 200 <= resp.status_code < 500
        return {"ok": ok, "status_code": resp.status_code, "elapsed_ms": elapsed, "error": None}
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return {"ok": False, "status_code": None, "elapsed_ms": elapsed, "error": str(exc)[:180]}


async def enrich_service(svc: dict[str, Any]) -> dict[str, Any]:
    active = unit_active(svc.get("unit"))
    enabled = unit_enabled(svc.get("unit"))
    listening = port_listening(svc.get("port"))
    probe = await http_probe(svc.get("local_url"))
    status = "online"
    reasons = []
    if active not in ("active", "unknown"):
        status = "offline"
        reasons.append(f"systemd={active}")
    if listening is False:
        status = "offline"
        reasons.append("port closed")
    if probe["ok"] is False:
        status = "degraded" if status == "online" else status
        reasons.append(probe.get("error") or f"HTTP {probe.get('status_code')}")
    if svc.get("exposure") == "local-only" and svc.get("url"):
        status = "degraded" if status == "online" else status
        reasons.append("local-only has public URL")
    return {
        **svc,
        "systemd": active,
        "enabled": enabled,
        "listening": listening,
        "http": probe,
        "status": status,
        "reasons": reasons,
        "checked_at": now_iso(),
    }


async def collect_services() -> list[dict[str, Any]]:
    return list(await asyncio.gather(*(enrich_service(svc) for svc in load_services())))


def metrics_summary() -> dict[str, Any]:
    boot_time = datetime.fromtimestamp(psutil.boot_time(), timezone.utc)
    disk_root = shutil.disk_usage("/")
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "cpu_count": psutil.cpu_count(),
        "loadavg": list(load),
        "memory": {"total": memory.total, "used": memory.used, "percent": memory.percent, "available": memory.available},
        "swap": {"total": swap.total, "used": swap.used, "percent": swap.percent},
        "disk": {"total": disk_root.total, "used": disk_root.used, "free": disk_root.free, "percent": round(disk_root.used / disk_root.total * 100, 1)},
        "boot_time": boot_time.isoformat(),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


def cron_summary() -> dict[str, Any]:
    cron_dir = Path("/root/.hermes/cron")
    jobs = []
    if cron_dir.exists():
        for path in sorted(cron_dir.glob("*.json"))[:200]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                jobs.append({
                    "id": data.get("id") or path.stem,
                    "name": data.get("name") or data.get("prompt", "")[:36] or path.stem,
                    "schedule": data.get("schedule"),
                    "paused": bool(data.get("paused")),
                    "deliver": data.get("deliver"),
                })
            except Exception:
                continue
    return {"hermes_jobs": jobs, "count": len(jobs), "cron_dir": str(cron_dir)}


def backup_summary() -> dict[str, Any]:
    entries = []
    if BACKUP_ROOT.exists():
        for path in sorted(BACKUP_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
            if path.is_dir():
                entries.append({"name": path.name, "path": str(path), "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
    return {"root": str(BACKUP_ROOT), "recent": entries, "count": len(entries)}


def security_summary(services: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = []
    local_with_url = [svc["name"] for svc in services if svc.get("exposure") == "local-only" and svc.get("url")]
    if local_with_url:
        warnings.append({"level": "high", "title": "local-only 服务存在公网 URL", "detail": ", ".join(local_with_url)})
    open_ports = [svc for svc in services if svc.get("listening") is True and svc.get("exposure") in ("public", "protected")]
    local_ports = [svc for svc in services if svc.get("listening") is True and svc.get("exposure") == "local-only"]
    if any(svc.get("id") == "openclaw" and svc.get("port") != 18789 for svc in services):
        warnings.append({"level": "medium", "title": "OpenClaw 端口异常", "detail": "检查 OpenClaw Gateway 暴露策略"})
    return {
        "warnings": warnings,
        "public_or_protected_ports": [{"name": x["name"], "port": x.get("port"), "exposure": x.get("exposure")} for x in open_ports],
        "local_only_ports": [{"name": x["name"], "port": x.get("port")} for x in local_ports],
        "write_operations": "disabled",
    }


def read_tail(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return [f"{path} not found"]
    try:
        return path.read_text(errors="replace").splitlines()[-limit:]
    except Exception as exc:
        return [str(exc)]


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> Response:
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='18' fill='#09090b'/><path d='M18 34h28M22 24h20M24 44h16' stroke='#a7f3d0' stroke-width='5' stroke-linecap='round'/></svg>"""
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {"ok": True, "service": "serverhub", "domain": SERVER_DOMAIN, "generated_at": now_iso()}


@app.get("/api/services")
async def api_services() -> dict[str, Any]:
    services = await collect_services()
    return {"services": services, "generated_at": now_iso()}


@app.get("/api/metrics/summary")
async def api_metrics() -> dict[str, Any]:
    return {"metrics": metrics_summary(), "generated_at": now_iso()}


@app.get("/api/tasks/summary")
async def api_tasks() -> dict[str, Any]:
    return {"tasks": cron_summary(), "generated_at": now_iso()}


@app.get("/api/backups/summary")
async def api_backups() -> dict[str, Any]:
    return {"backups": backup_summary(), "generated_at": now_iso()}


@app.get("/api/security/summary")
async def api_security() -> dict[str, Any]:
    services = await collect_services()
    return {"security": security_summary(services), "generated_at": now_iso()}


@app.get("/api/agents/summary")
async def api_agents() -> dict[str, Any]:
    import httpx

    data: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=1.4) as client:
            resp = await client.get(f"{HERMES_DASHBOARD}/api/agents")
            data["dashboard"] = resp.json() if resp.status_code < 500 else {"error": resp.status_code}
    except Exception as exc:
        data["dashboard"] = {"error": str(exc)[:180]}
    try:
        async with httpx.AsyncClient(timeout=1.2) as client:
            resp = await client.get(OPENCLAW_HEALTH)
            data["openclaw"] = resp.json() if resp.status_code < 500 else {"error": resp.status_code}
    except Exception as exc:
        data["openclaw"] = {"error": str(exc)[:180]}
    return {"agents": data, "generated_at": now_iso()}


@app.get("/api/health/all")
@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    services = await collect_services()
    metrics = metrics_summary()
    status_counts = {"online": 0, "degraded": 0, "offline": 0}
    for svc in services:
        status_counts[svc["status"]] = status_counts.get(svc["status"], 0) + 1
    return {
        "server": {"domain": SERVER_DOMAIN, "ip": SERVER_IP, "generated_at": now_iso()},
        "summary": {"total": len(services), **status_counts},
        "services": services,
        "metrics": metrics,
        "tasks": cron_summary(),
        "backups": backup_summary(),
        "security": security_summary(services),
    }


@app.get("/api/logs/{source}")
async def api_logs(source: str, lines: int = 120, q: str = "") -> dict[str, Any]:
    lines = max(20, min(int(lines or 120), 800))
    source_map = {
        "serverhub": {"unit": "server-home"},
        "monitor": {"unit": "hermes-system-monitor"},
        "agents": {"unit": "hermes-dashboard-web"},
        "openclaw": {"unit": "openclaw-gateway"},
        "nginx": {"unit": "nginx"},
        "memory": {"unit": "shared-agent-memory"},
    }
    meta = source_map.get(source)
    if not meta:
        raise HTTPException(status_code=404, detail="unknown log source")
    code, out, err = run(["journalctl", "-u", meta["unit"], "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=5)
    rows = (out if code == 0 else err or out).splitlines()
    if q:
        rows = [row for row in rows if q.lower() in row.lower()]
    return {"source": source, "unit": meta["unit"], "lines": rows[-lines:], "generated_at": now_iso()}


@app.get("/api/ops/{operation}/{unit}")
async def api_ops_disabled(operation: str, unit: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "write operations are disabled in ServerHub public console", "operation": operation, "unit": unit})


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str) -> FileResponse:
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="api not found")
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="[REDACTED_IP]", port=8000, log_level="info")
