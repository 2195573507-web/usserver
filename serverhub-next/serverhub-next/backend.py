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
from urllib.parse import urlencode
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
SERVER_DOMAIN = "serverhub-next.local"
SERVER_IP = "[REDACTED_IP]"
app = FastAPI(title="ServerHub Next", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], timeout: int = 6) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def load_services() -> list[dict[str, Any]]:
    raw = yaml.safe_load(SERVICES_FILE.read_text(encoding="utf-8")) if SERVICES_FILE.exists() else {}
    services = raw.get("services", []) if isinstance(raw, dict) else []
    result = []
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
        result.append(svc)
    return result


def unit_active(unit: str | None) -> str:
    if not unit:
        return "unknown"
    code, out, _ = run(["systemctl", "is-active", unit], timeout=4)
    return out if code == 0 else "inactive"


def port_open(port: int | None) -> bool | None:
    if not port:
        return None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex(("[REDACTED_IP]", int(port))) == 0


async def get_json(url: str, timeout: float = 4.0) -> dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def probe_http(url: str | None) -> dict[str, Any]:
    if not url:
        return {"ok": None, "status_code": None, "elapsed_ms": None, "error": None}
    start = time.perf_counter()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=1.4, follow_redirects=True) as client:
            resp = await client.get(url)
        return {"ok": resp.status_code < 500, "status_code": resp.status_code, "elapsed_ms": int((time.perf_counter() - start) * 1000), "error": None}
    except Exception as exc:
        return {"ok": False, "status_code": None, "elapsed_ms": int((time.perf_counter() - start) * 1000), "error": str(exc)[:180]}


async def enrich(svc: dict[str, Any]) -> dict[str, Any]:
    systemd = unit_active(svc.get("unit"))
    http = await probe_http(svc.get("local_url"))
    status = "online"
    reasons: list[str] = []
    if systemd not in ("active", "unknown"):
        status = "offline"
        reasons.append(f"systemd={systemd}")
    if svc.get("port") and port_open(svc.get("port")) is False:
        status = "offline"
        reasons.append("port closed")
    if http["ok"] is False:
        status = "degraded" if status == "online" else status
        reasons.append(http.get("error") or f"HTTP {http.get('status_code')}")
    return {**svc, "systemd": systemd, "http": http, "status": status, "reasons": reasons, "checked_at": now_iso()}


async def state() -> dict[str, Any]:
    services = await asyncio.gather(*(enrich(s) for s in load_services()))
    metrics = {"cpu": psutil.cpu_percent(0.05), "mem": psutil.virtual_memory().percent, "disk": shutil.disk_usage("/").used / shutil.disk_usage("/").total * 100, "uptime": int(time.time() - psutil.boot_time())}
    summary = {"online": sum(1 for s in services if s["status"] == "online"), "degraded": sum(1 for s in services if s["status"] == "degraded"), "offline": sum(1 for s in services if s["status"] == "offline"), "total": len(services)}
    return {"server": {"domain": SERVER_DOMAIN, "ip": SERVER_IP, "generated_at": now_iso()}, "summary": summary, "metrics": metrics, "services": services}


@app.api_route("/", methods=["GET", "HEAD"])
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {"ok": True, "service": "serverhub-next", "generated_at": now_iso()}


@app.get("/api/services")
async def api_services() -> dict[str, Any]:
    return {"services": load_services(), "generated_at": now_iso()}


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    return await state()


@app.get("/api/metrics")
async def api_metrics() -> dict[str, Any]:
    m = {"cpu": psutil.cpu_percent(0.05), "mem": psutil.virtual_memory().percent, "disk": shutil.disk_usage("/").used / shutil.disk_usage("/").total * 100, "uptime": int(time.time() - psutil.boot_time())}
    return {"metrics": m, "generated_at": now_iso()}


@app.get("/api/recall")
async def api_recall(q: str, mode: str = "default", limit_memory: int = 5, limit_obsidian: int = 5) -> dict[str, Any]:
    params = urlencode({
        "q": q[:300],
        "mode": mode,
        "limit_memory": max(0, min(int(limit_memory or 5), 10)),
        "limit_obsidian": max(0, min(int(limit_obsidian or 5), 10)),
    })
    try:
        data = await get_json(f"http://[REDACTED_IP]:19410/api/recall?{params}", timeout=8.0)
        return {"ok": True, "data": data, "generated_at": now_iso()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240], "generated_at": now_iso()}


@app.get("/api/provider-health")
async def api_provider_health() -> dict[str, Any]:
    try:
        data = await get_json("http://[REDACTED_IP]:19420/api/summary", timeout=4.0)
        return {"ok": True, "data": data, "generated_at": now_iso()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240], "generated_at": now_iso()}


@app.get("/api/logs/{unit}")
async def api_logs(unit: str, lines: int = 120) -> dict[str, Any]:
    lines = max(20, min(int(lines or 120), 500))
    code, out, err = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=6)
    return {"unit": unit, "lines": (out if code == 0 else err or out).splitlines()[-lines:], "generated_at": now_iso()}


@app.get("/api/ops/{action}/{unit}")
async def ops_disabled(action: str, unit: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "write operations disabled", "action": action, "unit": unit})


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="[REDACTED_IP]", port=9600, log_level="info")
