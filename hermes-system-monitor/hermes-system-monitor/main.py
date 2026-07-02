"""
FastAPI application — system monitoring backend.
Endpoints:
  GET /api/system/metrics/latest   – latest snapshot
  GET /api/system/metrics/history  – time-series history (range: 1h | 24h | 7d)
  GET /api/system/status           – uptime, hostname, OS info
  GET /api/system/realtime         – SSE stream (2 s push)
Static files served from /static on [REDACTED_IP]:9000.
"""

import asyncio
import json
import os
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psutil
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from collector import collector
from database import db

# ── Simple response cache ──────────────────────────────────────

import threading
from typing import Dict, Any, Tuple

class ResponseCache:
    """Simple TTL-based cache for API responses."""
    
    def __init__(self):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
    
    def get(self, key: str, ttl: float) -> Any:
        """Get cached value if not expired."""
        with self._lock:
            if key in self._cache:
                timestamp, value = self._cache[key]
                if time.time() - timestamp < ttl:
                    return value
        return None
    
    def set(self, key: str, value: Any):
        """Cache a value with current timestamp."""
        with self._lock:
            self._cache[key] = (time.time(), value)
    
    def clear(self):
        """Clear all cached values."""
        with self._lock:
            self._cache.clear()

cache = ResponseCache()


class HistoryPrebuildCache:
    """Background-built JSON cache for history endpoints.

    Keeps request handlers off the SQLite/query/JSON build path whenever possible.
    """

    def __init__(self, ranges: tuple[str, ...] = ("1h", "24h", "7d")):
        self._ranges = ranges
        self._payloads: Dict[str, Tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def get(self, range_name: str) -> bytes | None:
        with self._lock:
            item = self._payloads.get(range_name)
            return item[1] if item else None

    def set(self, range_name: str, payload: bytes):
        with self._lock:
            self._payloads[range_name] = (time.time(), payload)

    def age(self, range_name: str) -> float | None:
        with self._lock:
            item = self._payloads.get(range_name)
            return time.time() - item[0] if item else None


history_prebuild = HistoryPrebuildCache()


def _json_bytes(data: Any) -> bytes:
    """Render compact JSON once so hot endpoints can cache bytes, not just dicts."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _json_response_bytes(payload: bytes, max_age: int | None = None) -> Response:
    headers = {}
    if max_age is not None:
        headers["Cache-Control"] = f"public, max-age={max_age}, stale-while-revalidate=30"
    return Response(content=payload, media_type="application/json", headers=headers)

# ── App setup ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources and shut them down cleanly."""
    db.initialize()
    collector.start()
    persist_task = asyncio.create_task(_persist_loop())
    history_task = asyncio.create_task(_history_prebuild_loop())
    try:
        yield
    finally:
        persist_task.cancel()
        history_task.cancel()
        for task in (persist_task, history_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        collector.stop()


app = FastAPI(title="Hermes System Monitor", version="1.0.0", lifespan=lifespan)

# CORS – allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Application-level GZip is intentionally disabled. Nginx can compress static/API
# responses more efficiently; avoiding FastAPI GZip keeps history endpoints snappy.

class CacheControlStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith(".html"):
            response.headers.setdefault("Cache-Control", "no-cache")
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response


# Static files
STATIC_DIR = "/opt/hermes-system-monitor/static"
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", CacheControlStaticFiles(directory=STATIC_DIR), name="static")


# ── Background persistence ──────────────────────────────────────

async def _persist_loop():
    """Periodically persist collector snapshots to SQLite and run cleanup.
    Wrapped in exception guard so the loop never dies silently.
    """
    cleanup_counter = 0
    while True:
        try:
            data = collector.get_latest()
            if data:
                now = datetime.now(timezone.utc).isoformat()
                db.insert(data, timestamp=now)

            cleanup_counter += 1
            # Run cleanup roughly every 10 minutes (300 iterations at 2 s)
            if cleanup_counter >= 300:
                try:
                    db.cleanup()
                    db.maintenance()
                except Exception:
                    pass  # cleanup/maintenance failures are non-fatal
                cleanup_counter = 0
        except Exception as exc:
            print(f"[persist_loop] error: {exc}", flush=True)

        await asyncio.sleep(collector.interval)



async def _history_prebuild_loop():
    """Prebuild compact JSON history payloads off the request path."""
    # Build shortly after startup, then keep each range fresh at a calm cadence.
    cadences = {"1h": 5.0, "24h": 20.0, "7d": 30.0}
    last_build = {name: 0.0 for name in cadences}
    await asyncio.sleep(1.0)
    while True:
        now = time.monotonic()
        for range_name, cadence in cadences.items():
            if now - last_build.get(range_name, 0.0) < cadence:
                continue
            try:
                data = await asyncio.to_thread(db.get_history, range_name)
                payload = await asyncio.to_thread(_json_bytes, data)
                history_prebuild.set(range_name, payload)
                cache.set(f"history_json_{range_name}", payload)
                last_build[range_name] = now
            except Exception as exc:
                print(f"[history_prebuild:{range_name}] error: {exc}", flush=True)
        await asyncio.sleep(1.0)


# ── API Routes ──────────────────────────────────────────────────

@app.get("/api/system/metrics/latest")
async def metrics_latest():
    """Return the most recent system metrics snapshot."""
    cached = cache.get("latest_json", 1.0)
    if cached:
        return _json_response_bytes(cached, max_age=1)

    data = collector.get_latest()
    if not data:
        row = db.get_latest()
        if row:
            payload = _json_bytes(row)
            cache.set("latest_json", payload)
            return _json_response_bytes(payload, max_age=1)
        return JSONResponse(
            status_code=503,
            content={"error": "No metrics collected yet"},
        )

    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload = _json_bytes(data)
    cache.set("latest_json", payload)
    return _json_response_bytes(payload, max_age=1)


@app.get("/api/system/metrics/history")
async def metrics_history(range: str = Query("1h", pattern="^(1h|24h|7d)$")):
    """Return historical metrics with appropriate aggregation."""
    max_age = 4 if range == "1h" else 20

    prebuilt = history_prebuild.get(range)
    if prebuilt:
        return _json_response_bytes(prebuilt, max_age=max_age)

    # Startup fallback before the prebuilder has produced its first payload.
    ttl = 4.0 if range == "1h" else 20.0
    cache_key = f"history_json_{range}"
    cached = cache.get(cache_key, ttl)
    if cached:
        return _json_response_bytes(cached, max_age=max_age)

    data = await asyncio.to_thread(db.get_history, range)
    payload = await asyncio.to_thread(_json_bytes, data)
    cache.set(cache_key, payload)
    history_prebuild.set(range, payload)
    return _json_response_bytes(payload, max_age=max_age)


@app.get("/api/system/metrics/detailed")
async def metrics_detailed():
    """Return detailed metrics: per-core CPU + top processes."""
    data = collector.get_latest()
    if not data:
        row = db.get_latest()
        if not row:
            return JSONResponse(
                status_code=503,
                content={"error": "No metrics collected yet"},
            )
        data = row

    # Per-core CPU from collector
    per_cpu = data.get("cpu", {}).get("per_cpu", [])
    cores = data.get("cpu", {}).get("cores", len(per_cpu))

    # Reuse top-process data already collected by the background collector.
    # Avoid scanning all processes on every detailed endpoint request.
    top_processes = data.get("top_processes") or []

    return {
        "cpu_per_core": [
            {"core": i, "percent": v} for i, v in enumerate(per_cpu)
        ],
        "cpu_cores": cores,
        "top_processes": top_processes,
    }


@app.get("/api/system/status")
async def system_status():
    """Return basic system status information."""
    boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    now = datetime.now(timezone.utc)
    uptime_delta = now - boot_time

    # Format uptime
    days = uptime_delta.days
    hours, rem = divmod(uptime_delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s" if days > 0 else f"{hours}h {minutes}m {seconds}s"

    return {
        "uptime": uptime_str,
        "hostname": socket.gethostname(),
        "os": f"{os.uname().sysname} {os.uname().release}",
        "boot_time": boot_time.isoformat(),
    }


@app.get("/api/system/realtime")
async def realtime_stream():
    """SSE stream pushing system metrics every 2 seconds."""

    async def event_generator():
        yield "retry: 3000\n\n"
        last_sent = time.monotonic()
        while True:
            data = collector.get_latest()
            if data:
                data["timestamp"] = datetime.now(timezone.utc).isoformat()
                payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
                yield f"id: {int(time.time())}\nevent: metrics\ndata: {payload}\n\n"
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent > 15:
                yield ": heartbeat\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(collector.interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


# ── Root → Dashboard ────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(
        "/opt/hermes-system-monitor/static/index.html",
        headers={"Cache-Control": "no-cache"},
    )


# ── Health check ────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Entrypoint ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="[REDACTED_IP]",
        port=9000,
        log_level="info",
        access_log=False,
    )
