"""
System metrics collector using psutil.
Runs in a background thread, collecting every 2 seconds.
"""

import json
import os
import re
import socket
import ssl
import subprocess
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import psutil


class SystemCollector:
    """Collects system metrics periodically in a background thread."""

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._latest: dict = {}
        self._lock = threading.Lock()
        self._api_probe_cache = {"updated_at": 0.0, "items": [], "summary": {"ok": 0, "warn": 0, "bad": 0}, "baseline": {}}
        self._api_probe_lock = threading.Lock()
        self._api_latency_history = {}
        self._sections = self._default_sections()
        self._sections_lock = threading.Lock()
        self._section_refreshing = set()
        self._section_last_refresh = {"medium": 0.0, "slow": 0.0}

    @staticmethod
    def _get_monitor_api_key() -> str:
        """Return an API key for read-only model probes without exposing it in metrics."""
        for name in ("HERMES_MONITOR_API_KEY", "HERMES_API_KEY", "OPENAI_API_KEY", "API_KEY"):
            value = os.environ.get(name, "").strip()
            if value:
                return value
        for path in ("/opt/hermes-docker/config.yaml",):
            try:
                text = Path(path).read_text()
                match = re.search(r"(?m)^\s*api_key\s*:\s*['\"]?([^'\"\s#]+)", text)
                if match:
                    return match.group(1).strip()
            except Exception:
                pass
        return ""

    @classmethod
    def _auth_headers(cls) -> dict:
        headers = {"User-Agent": "hermes-system-monitor/1.0"}
        api_key = cls._get_monitor_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _probe_api_links(self) -> dict:
        """Probe local/external OpenAI-compatible API entrypoints with auth when available."""
        now = time.time()
        with self._api_probe_lock:
            if now - self._api_probe_cache.get("updated_at", 0) < 30:
                return dict(self._api_probe_cache)

            targets = [
                {"name": "local18888", "url": "http://[REDACTED_IP]:18888/v1/models", "kind": "local"},
                {"name": "apius", "url": "https://apius.zmjjkkk.fun/v1/models", "kind": "external"},
                {"name": "subus", "url": "https://subus.zmjjkkk.fun/v1/models", "kind": "external"},
            ]
            items = []
            for target in targets:
                start = time.perf_counter()
                code = 0
                error = ""
                try:
                    req = Request(target["url"], headers=self._auth_headers())
                    try:
                        with urlopen(req, timeout=8) as resp:
                            code = getattr(resp, "status", 0)
                            resp.read(256)
                    except Exception as exc:
                        # urllib raises HTTPError for 401/403; those are valid reachability signals.
                        code = getattr(exc, "code", 0) or 0
                        if not code:
                            error = type(exc).__name__
                except Exception as exc:
                    error = type(exc).__name__
                latency_ms = round((time.perf_counter() - start) * 1000, 1)
                ok = code in (200, 401, 403)
                warn = ok and latency_ms > (40 if target["kind"] == "local" else 400)
                bad = (not ok) or latency_ms > (100 if target["kind"] == "local" else 1200)

                hist = self._api_latency_history.setdefault(target["name"], [])
                hist.append(latency_ms)
                if len(hist) > 120:
                    del hist[:-120]
                avg_ms = round(sum(hist) / len(hist), 1) if hist else latency_ms
                degradation = round(latency_ms / avg_ms, 2) if avg_ms else 1.0

                items.append({
                    "name": target["name"],
                    "url": target["url"],
                    "kind": target["kind"],
                    "status_code": code,
                    "latency_ms": latency_ms,
                    "baseline_avg_ms": avg_ms,
                    "degradation_ratio": degradation,
                    "ok": ok and not bad,
                    "state": "bad" if bad else "warn" if warn or degradation >= 2.0 else "ok",
                    "error": error,
                })

            summary = {
                "ok": sum(1 for x in items if x["state"] == "ok"),
                "warn": sum(1 for x in items if x["state"] == "warn"),
                "bad": sum(1 for x in items if x["state"] == "bad"),
                "count": len(items),
            }
            worst = max(items, key=lambda x: (x["state"] == "bad", x["state"] == "warn", x["latency_ms"]), default={})
            baseline = {
                "window_samples": max((len(v) for v in self._api_latency_history.values()), default=0),
                "worst_name": worst.get("name"),
                "worst_latency_ms": worst.get("latency_ms"),
                "worst_degradation_ratio": worst.get("degradation_ratio"),
            }
            self._api_probe_cache = {"updated_at": now, "items": items, "summary": summary, "baseline": baseline}
            return dict(self._api_probe_cache)

    @staticmethod
    def _get_network_rates(prev: dict, interval: float) -> dict:
        """Calculate network RX/TX rates in Mbps compared to previous snapshot."""
        current = psutil.net_io_counters()
        rx_rate = tx_rate = 0.0
        if prev:
            delta_rx = current.bytes_recv - prev["bytes_recv"]
            delta_tx = current.bytes_sent - prev["bytes_sent"]
            # Convert bytes/s to Mbps
            rx_rate = round((delta_rx * 8) / (interval * 1_000_000), 4)
            tx_rate = round((delta_tx * 8) / (interval * 1_000_000), 4)
        return {
            "rx_rate_mbps": rx_rate,
            "tx_rate_mbps": tx_rate,
            "bytes_recv": current.bytes_recv,
            "bytes_sent": current.bytes_sent,
        }

    @staticmethod
    def _get_disk_io_rates(prev: dict, interval: float) -> dict:
        """Calculate disk throughput, IOPS, and await from cumulative counters."""
        current = psutil.disk_io_counters()
        read_rate = write_rate = read_iops = write_iops = await_ms = 0.0
        if current and prev:
            delta_read = max(0, current.read_bytes - prev.get("read_bytes", 0))
            delta_write = max(0, current.write_bytes - prev.get("write_bytes", 0))
            delta_reads = max(0, current.read_count - prev.get("read_count", 0))
            delta_writes = max(0, current.write_count - prev.get("write_count", 0))
            delta_read_ms = max(0, current.read_time - prev.get("read_time", 0))
            delta_write_ms = max(0, current.write_time - prev.get("write_time", 0))
            ops = delta_reads + delta_writes
            read_rate = round(delta_read / interval / (1024 ** 2), 3)
            write_rate = round(delta_write / interval / (1024 ** 2), 3)
            read_iops = round(delta_reads / interval, 2)
            write_iops = round(delta_writes / interval, 2)
            await_ms = round((delta_read_ms + delta_write_ms) / ops, 2) if ops else 0.0
        return {
            "read_rate_mbps": read_rate,
            "write_rate_mbps": write_rate,
            "read_iops": read_iops,
            "write_iops": write_iops,
            "await_ms": await_ms,
            "read_bytes": current.read_bytes if current else 0,
            "write_bytes": current.write_bytes if current else 0,
            "read_count": current.read_count if current else 0,
            "write_count": current.write_count if current else 0,
            "read_time": current.read_time if current else 0,
            "write_time": current.write_time if current else 0,
        }

    @staticmethod
    def _read_psi_file(path: str) -> dict:
        values = {"some_avg10": 0.0, "some_avg60": 0.0, "some_avg300": 0.0, "full_avg10": 0.0, "full_avg60": 0.0, "full_avg300": 0.0}
        try:
            for line in Path(path).read_text().splitlines():
                parts = line.split()
                if not parts:
                    continue
                prefix = parts[0]
                for part in parts[1:]:
                    key, _, value = part.partition("=")
                    if key.startswith("avg"):
                        values[f"{prefix}_{key}"] = float(value)
        except Exception:
            pass
        return values

    @classmethod
    def _get_psi_pressure(cls) -> dict:
        return {
            "cpu": cls._read_psi_file("/proc/pressure/cpu"),
            "io": cls._read_psi_file("/proc/pressure/io"),
            "memory": cls._read_psi_file("/proc/pressure/memory"),
        }

    @staticmethod
    def _get_tcp_retrans(prev: dict, interval: float) -> dict:
        current = {"retrans_segs": 0, "out_segs": 0}
        try:
            lines = Path("/proc/net/snmp").read_text().splitlines()
            for i, line in enumerate(lines):
                if line.startswith("Tcp:") and i + 1 < len(lines) and lines[i + 1].startswith("Tcp:"):
                    keys = line.split()[1:]
                    vals = lines[i + 1].split()[1:]
                    data = dict(zip(keys, vals))
                    current["retrans_segs"] = int(data.get("RetransSegs", 0))
                    current["out_segs"] = int(data.get("OutSegs", 0))
                    break
        except Exception:
            pass
        delta_retrans = max(0, current["retrans_segs"] - prev.get("retrans_segs", 0)) if prev else 0
        delta_out = max(0, current["out_segs"] - prev.get("out_segs", 0)) if prev else 0
        rate = round(delta_retrans / interval, 3) if interval else 0.0
        ratio = round(delta_retrans / delta_out * 100, 4) if delta_out else 0.0
        return {**current, "retrans_rate": rate, "retrans_ratio": ratio, "delta_retrans": delta_retrans, "delta_out": delta_out}

    @staticmethod
    def _get_systemd_failed_units() -> dict:
        try:
            out = subprocess.check_output(["systemctl", "--failed", "--no-legend", "--no-pager"], stderr=subprocess.DEVNULL, timeout=3, text=True)
            units = []
            for line in out.splitlines():
                clean = line.strip().lstrip("●").strip()
                if not clean:
                    continue
                parts = clean.split(None, 4)
                units.append({"unit": parts[0], "state": parts[2] if len(parts) > 2 else "failed", "description": parts[4] if len(parts) > 4 else ""})
            return {"count": len(units), "units": units[:12], "ok": len(units) == 0}
        except Exception as exc:
            return {"count": 0, "units": [], "ok": True, "error": type(exc).__name__}

    @staticmethod
    def _get_active_interface() -> str:
        """Return the name of the first non-lo, up network interface."""
        stats = psutil.net_if_stats()
        for name, stat in stats.items():
            if name != "lo" and stat.isup:
                return name
        return ""

    @staticmethod
    def _get_top_processes(limit: int = 5) -> list:
        """Return the top N processes by CPU usage."""
        procs = []
        for p in sorted(
            psutil.process_iter(["name", "pid", "cpu_percent", "memory_percent"]),
            key=lambda p: p.info["cpu_percent"] or 0,
            reverse=True,
        )[:limit]:
            procs.append({
                "name": p.info["name"],
                "pid": p.info["pid"],
                "cpu_percent": round(p.info["cpu_percent"] or 0, 1),
                "memory_percent": round(p.info["memory_percent"] or 0, 1),
            })
        return procs

    @staticmethod
    def _get_swap() -> dict:
        """Return swap memory stats."""
        sw = psutil.swap_memory()
        return {
            "percent": round(sw.percent, 1),
            "used_gb": round(sw.used / (1024 ** 3), 2),
            "total_gb": round(sw.total / (1024 ** 3), 2),
        }

    @staticmethod
    def _get_conntrack() -> dict:
        """Return conntrack table usage percent."""
        try:
            with open("/proc/sys/net/netfilter/nf_conntrack_count") as f:
                count = int(f.read().strip())
            with open("/proc/sys/net/netfilter/nf_conntrack_max") as f:
                maximum = int(f.read().strip())
            return {"count": count, "max": maximum, "percent": round(count / maximum * 100, 1) if maximum else 0}
        except Exception:
            return {"count": 0, "max": 0, "percent": 0}

    @staticmethod
    def _get_fd_usage() -> dict:
        """Return file descriptor usage."""
        try:
            with open("/proc/sys/fs/file-nr") as f:
                allocated, _, maximum = f.read().split()
            allocated, maximum = int(allocated), int(maximum)
            return {"allocated": allocated, "max": maximum, "percent": round(allocated / maximum * 100, 3) if maximum else 0}
        except Exception:
            return {"allocated": 0, "max": 0, "percent": 0}

    _oom_count = 0
    _oom_last_check = 0.0
    _oom_lock = threading.Lock()

    _proxy_cache = {"status_code": 0, "latency_ms": 0, "reachable": False}
    _proxy_last_check = 0.0
    _proxy_lock = threading.Lock()

    _docker_cache = {
        "available": False,
        "root_dir": "",
        "root_ok": False,
        "build_cache_gb": None,
        "build_cache_reclaimable_gb": None,
        "containers": [],
        "healthy_count": 0,
        "running_count": 0,
        "expected_count": 4,
        "ok": False,
    }
    _docker_last_check = 0.0
    _docker_lock = threading.Lock()
    _docker_resource_cache = {"available": False, "containers": [], "total_cpu_percent": 0.0, "total_mem_mb": 0.0, "updated_at": 0.0, "stale": True}
    _docker_resource_last_check = 0.0
    _docker_resource_lock = threading.Lock()

    _growth_cache = {"paths": [], "updated_at": 0.0}
    _growth_last_check = 0.0
    _growth_prev_sizes = {}
    _growth_lock = threading.Lock()

    _freshness_cache = {"items": [], "stale_count": 0, "ok": False}
    _freshness_last_check = 0.0
    _freshness_lock = threading.Lock()

    _dependency_cache = {"targets": [], "ok": False}
    _dependency_last_check = 0.0
    _dependency_lock = threading.Lock()

    _self_healing_cache = {"events": [], "last_cleanup": None, "released_gb_today": 0.0, "ok": True}
    _self_healing_last_check = 0.0
    _self_healing_lock = threading.Lock()

    _http_error_cache = {"status_2xx": 0, "status_4xx": 0, "status_5xx": 0, "status_other": 0, "sample_lines": 0, "updated_at": 0.0}
    _http_error_last_check = 0.0
    _http_error_lock = threading.Lock()

    _network_totals_path = Path("/opt/hermes-system-monitor/network_totals.json")
    _network_totals_state = None
    _network_totals_lock = threading.Lock()

    _service_cache = {"items": [], "ok": False, "updated_at": 0.0}
    _service_last_check = 0.0
    _service_lock = threading.Lock()

    _db_cache = {"items": [], "total_db_mb": 0.0, "total_wal_mb": 0.0, "ok": True, "updated_at": 0.0}
    _db_last_check = 0.0
    _db_lock = threading.Lock()

    _sub2api_io_cache = {"rx_rate_kbps": 0.0, "tx_rate_kbps": 0.0, "total_rx_mb": 0.0, "total_tx_mb": 0.0, "ok": False, "updated_at": 0.0}
    _sub2api_io_last_check = 0.0
    _sub2api_io_lock = threading.Lock()
    _sub2api_pid = None
    _sub2api_pid_last_check = 0.0
    _sub2api_io_prev = {}

    @classmethod
    def _get_oom_count(cls) -> int:
        """Return total OOM kill count from dmesg, cached for 5 minutes."""
        now = time.time()
        with cls._oom_lock:
            if now - cls._oom_last_check < 300:
                return cls._oom_count
            try:
                out = subprocess.check_output(
                    ["dmesg"], stderr=subprocess.DEVNULL, timeout=3, text=True
                )
                cls._oom_count = len(re.findall(r"Out of memory|oom-kill|Killed process", out, re.I))
            except Exception:
                pass
            cls._oom_last_check = now
            return cls._oom_count

    @classmethod
    def _get_proxy_latency(cls) -> dict:
        """Quick latency check to the AI proxy, cached for 60s."""
        now = time.time()
        with cls._proxy_lock:
            if now - cls._proxy_last_check < 60:
                return dict(cls._proxy_cache)
            try:
                cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time", "5"]
                api_key = cls._get_monitor_api_key()
                if api_key:
                    cmd.extend(["-H", f"Authorization: Bearer {api_key}"])
                cmd.append("https://pool.gptstore.club/v1/models")
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=6, text=True).strip()
                code, _, latency = out.partition(" ")
                cls._proxy_cache = {
                    "status_code": int(code) if code.isdigit() else 0,
                    "latency_ms": round(float(latency) * 1000, 0) if latency else 0,
                    "reachable": code.isdigit() and int(code) > 0,
                }
            except Exception:
                cls._proxy_cache = {"status_code": 0, "latency_ms": 0, "reachable": False}
            cls._proxy_last_check = now
            return dict(cls._proxy_cache)


    @staticmethod
    def _parse_size_to_gb(value: str) -> Optional[float]:
        """Parse Docker size strings such as 2.4GB or 800MB into GB."""
        if not value:
            return None
        match = re.search(r"([0-9.]+)\s*([KMGT]I?B)", value.strip(), re.I)
        if not match:
            return None
        number = float(match.group(1))
        unit = match.group(2).upper().replace("IB", "B")
        factors = {"KB": 1 / (1024 ** 2), "MB": 1 / 1024, "GB": 1, "TB": 1024}
        return round(number * factors.get(unit, 1), 3)

    @classmethod
    def _get_docker_status(cls) -> dict:
        """Return Docker root, cache size, and key container health, cached for 60s."""
        now = time.time()
        with cls._docker_lock:
            if now - cls._docker_last_check < 60:
                return dict(cls._docker_cache)
            expected = {"sub2api", "upstream-hub-standalone", "sub2api-postgres", "sub2api-redis"}
            status = dict(cls._docker_cache)
            status.update({"available": False, "containers": [], "healthy_count": 0, "running_count": 0, "ok": False})
            try:
                root_dir = subprocess.check_output(
                    ["docker", "info", "--format", "{{.DockerRootDir}}"],
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    text=True,
                ).strip()
                status["available"] = True
                status["root_dir"] = root_dir
                status["root_ok"] = root_dir == "/www/docker"

                ps_out = subprocess.check_output(
                    ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    text=True,
                )
                containers = []
                for line in ps_out.splitlines():
                    parts = line.split("\t", 2)
                    if len(parts) != 3:
                        continue
                    name, raw_status, image = parts
                    lower = raw_status.lower()
                    running = lower.startswith("up")
                    healthy = "healthy" in lower or (running and "health" not in lower)
                    containers.append({
                        "name": name,
                        "status": raw_status,
                        "image": image,
                        "running": running,
                        "healthy": healthy,
                        "expected": name in expected,
                    })
                expected_containers = [c for c in containers if c["expected"]]
                status["containers"] = containers
                status["running_count"] = sum(1 for c in expected_containers if c["running"])
                status["healthy_count"] = sum(1 for c in expected_containers if c["healthy"])
                status["expected_count"] = len(expected)

                df_out = subprocess.check_output(
                    ["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"],
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    text=True,
                )
                for line in df_out.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3 and parts[0].lower() == "build cache":
                        status["build_cache_gb"] = cls._parse_size_to_gb(parts[1])
                        status["build_cache_reclaimable_gb"] = cls._parse_size_to_gb(parts[2])

                status["ok"] = bool(status["root_ok"] and status["healthy_count"] == len(expected))
            except Exception:
                status["available"] = False
            cls._docker_cache = status
            cls._docker_last_check = now
            return dict(status)

    @classmethod
    def _get_path_growth(cls) -> dict:
        """Return current sizes and GB/hour growth for key disk paths, cached for 60s."""
        now = time.time()
        with cls._growth_lock:
            if now - cls._growth_last_check < 60:
                return dict(cls._growth_cache)
            paths = ["/", "/www", "/www/docker", "/www/data/containerd"]
            result = []
            for path in paths:
                try:
                    if path in ("/", "/www"):
                        usage = psutil.disk_usage(path)
                        size_bytes = usage.used
                    else:
                        if not os.path.exists(path):
                            continue
                        out = subprocess.check_output(
                            ["du", "-sbx", path],
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            text=True,
                        ).split()[0]
                        size_bytes = int(out)
                    prev = cls._growth_prev_sizes.get(path)
                    growth_gb_per_hour = 0.0
                    if prev:
                        prev_bytes, prev_time = prev
                        elapsed = max(1.0, now - prev_time)
                        growth_gb_per_hour = ((size_bytes - prev_bytes) / (1024 ** 3)) * (3600 / elapsed)
                    cls._growth_prev_sizes[path] = (size_bytes, now)
                    result.append({
                        "path": path,
                        "used_gb": round(size_bytes / (1024 ** 3), 2),
                        "growth_gb_per_hour": round(growth_gb_per_hour, 3),
                    })
                except Exception:
                    continue
            cls._growth_cache = {"paths": result, "updated_at": now}
            cls._growth_last_check = now
            return dict(cls._growth_cache)


    @staticmethod
    def _file_age_item(name: str, path: str, warn_after_seconds: int) -> dict:
        """Return age information for a file path."""
        try:
            mtime = os.path.getmtime(path)
            age = max(0, time.time() - mtime)
            return {
                "name": name,
                "path": path,
                "age_seconds": round(age, 0),
                "age_minutes": round(age / 60, 1),
                "fresh": age <= warn_after_seconds,
                "exists": True,
            }
        except Exception:
            return {
                "name": name,
                "path": path,
                "age_seconds": None,
                "age_minutes": None,
                "fresh": False,
                "exists": False,
            }

    @staticmethod
    def _newest_file_age_item(name: str, paths: list[str], warn_after_seconds: int) -> dict:
        """Return age information for the newest existing path in a set."""
        existing = [p for p in paths if os.path.exists(p)]
        if not existing:
            return {"name": name, "path": paths[0] if paths else "", "age_seconds": None, "age_minutes": None, "fresh": False, "exists": False}
        newest = max(existing, key=lambda p: os.path.getmtime(p))
        item = SystemCollector._file_age_item(name, newest, warn_after_seconds)
        item["paths_checked"] = paths
        return item

    @classmethod
    def _get_data_freshness(cls) -> dict:
        """Return freshness of key state/data files, cached for 60s."""
        now = time.time()
        with cls._freshness_lock:
            if now - cls._freshness_last_check < 60:
                return dict(cls._freshness_cache)
            items = [
                cls._file_age_item("OpenClaw state", "/root/.openclaw/state/openclaw.sqlite-wal", 300),
                cls._file_age_item("Hermes state", "/root/.hermes/state.db-wal", 900),
                cls._file_age_item("Hermes cron", "/root/.hermes/cron/.tick.lock", 900),
                cls._file_age_item("Monitor DB", "/opt/hermes-system-monitor/metrics.db-wal", 300),
            ]
            items.append(cls._newest_file_age_item(
                "Shared memory DB",
                [
                    "/opt/shared-agent-memory/data/memory.sqlite-wal",
                    "/opt/shared-agent-memory/data/memory.sqlite-shm",
                    "/opt/shared-agent-memory/data/memory.sqlite",
                ],
                86400,
            ))
            stale_count = sum(1 for item in items if not item.get("fresh"))
            cls._freshness_cache = {"items": items, "stale_count": stale_count, "ok": stale_count == 0, "updated_at": now}
            cls._freshness_last_check = now
            return dict(cls._freshness_cache)

    @classmethod
    def _probe_http_quality(cls, name: str, url: str) -> dict:
        """Probe DNS, TCP, TLS, and HTTP timing using curl write-out."""
        parsed = urlparse(url)
        result = {"name": name, "url": url, "ok": False, "status_code": 0, "dns_ms": None, "connect_ms": None, "tls_ms": None, "total_ms": None}
        try:
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            dns_start = time.time()
            socket.getaddrinfo(host, port)
            result["dns_ms"] = round((time.time() - dns_start) * 1000, 0)
            cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_connect} %{time_appconnect} %{time_total}", "--max-time", "8"]
            api_key = cls._get_monitor_api_key()
            if api_key and "/v1/" in url:
                cmd.extend(["-H", f"Authorization: Bearer {api_key}"])
            cmd.append(url)
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10, text=True).strip()
            code, connect, appconnect, total = out.split()
            result["status_code"] = int(code) if code.isdigit() else 0
            result["connect_ms"] = round(float(connect) * 1000, 0)
            tls_value = float(appconnect)
            result["tls_ms"] = round(tls_value * 1000, 0) if tls_value else 0
            result["total_ms"] = round(float(total) * 1000, 0)
            result["ok"] = 200 <= result["status_code"] < 500
        except Exception:
            pass
        return result

    @classmethod
    def _get_external_dependency_quality(cls) -> dict:
        """Return network quality probes for external dependencies, cached for 5 minutes."""
        now = time.time()
        with cls._dependency_lock:
            if now - cls._dependency_last_check < 300:
                return dict(cls._dependency_cache)
            targets = [
                cls._probe_http_quality("Subus models", "https://subus.zmjjkkk.fun/v1/models"),
                cls._probe_http_quality("GPTStore models", "https://pool.gptstore.club/v1/models"),
            ]
            ok = all(target.get("ok") for target in targets)
            cls._dependency_cache = {"targets": targets, "ok": ok, "updated_at": now}
            cls._dependency_last_check = now
            return dict(cls._dependency_cache)

    @staticmethod
    def _parse_cleanup_log(path: Path) -> tuple[Optional[dict], float]:
        """Parse Docker cleanup logs and return last event plus total reclaimed GB."""
        if not path.exists():
            return None, 0.0
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            return None, 0.0
        total_released = 0.0
        last_event = None
        current_started = None
        before_used = None
        after_used = None
        for line in text.splitlines():
            if "docker low-risk cleanup start" in line:
                match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
                current_started = match.group(1) if match else None
                before_used = None
                after_used = None
            elif line.startswith("Total:"):
                value = SystemCollector._parse_size_to_gb(line.partition("Total:")[2].strip()) or 0.0
                total_released += value
                if current_started:
                    last_event = {"time": current_started, "released_gb": round(value, 3)}
            elif line.startswith("/dev/"):
                parts = line.split()
                if len(parts) >= 4:
                    used = SystemCollector._parse_size_to_gb(parts[2])
                    if before_used is None:
                        before_used = used
                    else:
                        after_used = used
                        if last_event and before_used is not None and after_used is not None:
                            last_event["system_used_before_gb"] = before_used
                            last_event["system_used_after_gb"] = after_used
        return last_event, round(total_released, 3)

    @classmethod
    def _get_self_healing_events(cls) -> dict:
        """Return recent self-healing/cleanup activity, cached for 60s."""
        now = time.time()
        with cls._self_healing_lock:
            if now - cls._self_healing_last_check < 60:
                return dict(cls._self_healing_cache)
            today = time.strftime("%Y%m%d", time.gmtime())
            log_path = Path(f"/www/logs/docker-cleanup/cleanup-{today}.log")
            last_cleanup, released = cls._parse_cleanup_log(log_path)
            events = []
            if last_cleanup:
                events.append({"type": "docker-cleanup", **last_cleanup})
            try:
                out = subprocess.check_output(
                    ["systemctl", "show", "docker-low-risk-cleanup.timer", "-p", "ActiveState", "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime"],
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    text=True,
                ).splitlines()
                values = {}
                for line in out:
                    key, _, value = line.partition("=")
                    values[key] = value
                timer = {
                    "active_state": values.get("ActiveState", "unknown"),
                    "last_trigger": values.get("LastTriggerUSec", ""),
                    "next_trigger": values.get("NextElapseUSecRealtime", ""),
                }
            except Exception:
                timer = {"active_state": "unknown", "last_trigger": "", "next_trigger": ""}
            ok = timer.get("active_state") == "active"
            cls._self_healing_cache = {"events": events, "last_cleanup": last_cleanup, "released_gb_today": released, "timer": timer, "ok": ok, "updated_at": now}
            cls._self_healing_last_check = now
            return dict(cls._self_healing_cache)


    @classmethod
    def _get_http_status_counts(cls) -> dict:
        """Return recent Nginx status-code class counts from the tail of access.log."""
        now = time.time()
        with cls._http_error_lock:
            if now - cls._http_error_last_check < 30:
                return dict(cls._http_error_cache)
            counts = {"status_2xx": 0, "status_4xx": 0, "status_5xx": 0, "status_other": 0, "sample_lines": 0, "updated_at": now}
            try:
                out = subprocess.check_output(
                    ["tail", "-n", "600", "/www/logs/nginx/access.log"],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    text=True,
                )
                for line in out.splitlines():
                    match = re.search(r'"\S+\s+\S+\s+HTTP/[^\"]+"\s+(\d{3})\b', line)
                    if not match:
                        continue
                    counts["sample_lines"] += 1
                    code = int(match.group(1))
                    if 200 <= code < 300:
                        counts["status_2xx"] += 1
                    elif 400 <= code < 500:
                        counts["status_4xx"] += 1
                    elif 500 <= code < 600:
                        counts["status_5xx"] += 1
                    else:
                        counts["status_other"] += 1
            except Exception:
                pass
            cls._http_error_cache = counts
            cls._http_error_last_check = now
            return dict(cls._http_error_cache)


    @staticmethod
    def _bytes_to_gb(value: int) -> float:
        return round((value or 0) / (1024 ** 3), 3)

    @classmethod
    def _load_network_totals_state(cls) -> dict:
        if cls._network_totals_state is not None:
            return cls._network_totals_state
        default = {"day": "", "month": "", "day_rx_base": 0, "day_tx_base": 0, "month_rx_base": 0, "month_tx_base": 0, "day_rx_peak_mbps": 0.0, "day_tx_peak_mbps": 0.0, "month_rx_peak_mbps": 0.0, "month_tx_peak_mbps": 0.0}
        try:
            if cls._network_totals_path.exists():
                data = json.loads(cls._network_totals_path.read_text())
                default.update(data if isinstance(data, dict) else {})
        except Exception:
            pass
        cls._network_totals_state = default
        return cls._network_totals_state

    @classmethod
    def _get_network_totals(cls, rx_bytes: int, tx_bytes: int, rx_rate_mbps: float, tx_rate_mbps: float) -> dict:
        """Return boot/day/month cumulative network totals and daily/monthly peaks."""
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        with cls._network_totals_lock:
            st = cls._load_network_totals_state()
            changed = False
            if st.get("day") != day:
                st.update({"day": day, "day_rx_base": rx_bytes, "day_tx_base": tx_bytes, "day_rx_peak_mbps": 0.0, "day_tx_peak_mbps": 0.0})
                changed = True
            if st.get("month") != month:
                st.update({"month": month, "month_rx_base": rx_bytes, "month_tx_base": tx_bytes, "month_rx_peak_mbps": 0.0, "month_tx_peak_mbps": 0.0})
                changed = True
            st["day_rx_peak_mbps"] = max(float(st.get("day_rx_peak_mbps") or 0), float(rx_rate_mbps or 0))
            st["day_tx_peak_mbps"] = max(float(st.get("day_tx_peak_mbps") or 0), float(tx_rate_mbps or 0))
            st["month_rx_peak_mbps"] = max(float(st.get("month_rx_peak_mbps") or 0), float(rx_rate_mbps or 0))
            st["month_tx_peak_mbps"] = max(float(st.get("month_tx_peak_mbps") or 0), float(tx_rate_mbps or 0))
            changed = True
            try:
                cls._network_totals_path.parent.mkdir(parents=True, exist_ok=True)
                cls._network_totals_path.write_text(json.dumps(st, ensure_ascii=False, indent=2))
            except Exception:
                pass
            return {
                "boot_rx_gb": cls._bytes_to_gb(rx_bytes),
                "boot_tx_gb": cls._bytes_to_gb(tx_bytes),
                "today_rx_gb": cls._bytes_to_gb(max(0, rx_bytes - int(st.get("day_rx_base") or 0))),
                "today_tx_gb": cls._bytes_to_gb(max(0, tx_bytes - int(st.get("day_tx_base") or 0))),
                "month_rx_gb": cls._bytes_to_gb(max(0, rx_bytes - int(st.get("month_rx_base") or 0))),
                "month_tx_gb": cls._bytes_to_gb(max(0, tx_bytes - int(st.get("month_tx_base") or 0))),
                "day_rx_peak_mbps": round(float(st.get("day_rx_peak_mbps") or 0), 3),
                "day_tx_peak_mbps": round(float(st.get("day_tx_peak_mbps") or 0), 3),
                "month_rx_peak_mbps": round(float(st.get("month_rx_peak_mbps") or 0), 3),
                "month_tx_peak_mbps": round(float(st.get("month_tx_peak_mbps") or 0), 3),
            }

    @classmethod
    def _get_docker_resource_status(cls) -> dict:
        """Return per-container resource stats, cached so docker stats never blocks every 2s tick."""
        now = time.time()
        with cls._docker_resource_lock:
            if now - cls._docker_resource_last_check < 10:
                return dict(cls._docker_resource_cache)
            previous = dict(cls._docker_resource_cache)
            try:
                out = subprocess.check_output([
                    "docker", "stats", "--no-stream", "--format",
                    "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}"
                ], stderr=subprocess.DEVNULL, timeout=3, text=True)
            except Exception:
                previous["available"] = False
                previous["stale"] = True
                previous["updated_at"] = cls._docker_resource_last_check or now
                cls._docker_resource_cache = previous
                cls._docker_resource_last_check = now
                return dict(cls._docker_resource_cache)
            containers = []
            total_cpu = 0.0
            total_mem_mb = 0.0
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                name, cpu_s, mem_s, net_s, block_s = parts[:5]
                try:
                    cpu = float(cpu_s.replace("%", "").strip())
                except Exception:
                    cpu = 0.0
                mem_used = mem_s.split("/")[0].strip()
                mem_gb = cls._parse_size_to_gb(mem_used) or 0.0
                mem_mb = mem_gb * 1024
                total_cpu += cpu
                total_mem_mb += mem_mb
                containers.append({"name": name, "cpu_percent": round(cpu, 2), "mem_mb": round(mem_mb, 1), "net_io": net_s, "block_io": block_s})
            cls._docker_resource_cache = {"available": True, "containers": containers, "total_cpu_percent": round(total_cpu, 2), "total_mem_mb": round(total_mem_mb, 1), "updated_at": now, "stale": False}
            cls._docker_resource_last_check = now
            return dict(cls._docker_resource_cache)

    @staticmethod
    def _probe_local_http(name: str, url: str) -> dict:
        result = {"name": name, "url": url, "ok": False, "status_code": 0, "latency_ms": None}
        try:
            out = subprocess.check_output(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time", "4", url], stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
            code, _, total = out.partition(" ")
            result["status_code"] = int(code) if code.isdigit() else 0
            result["latency_ms"] = round(float(total) * 1000, 0) if total else None
            result["ok"] = 200 <= result["status_code"] < 500
        except Exception:
            pass
        return result

    @classmethod
    def _get_service_health(cls) -> dict:
        now = time.time()
        with cls._service_lock:
            if now - cls._service_last_check < 30:
                return dict(cls._service_cache)
            units = ["docker.service", "nginx.service", "openclaw-gateway.service", "hermes-gateway.service", "hermes-dashboard-web.service", "hermes-system-monitor.service", "shared-agent-memory.service", "ollama.service", "server-observability-index.timer", "openclaw-memory-maintenance.timer"]
            items = []
            states = {}
            try:
                out = subprocess.check_output(["systemctl", "show", *units, "-p", "Id", "-p", "ActiveState", "--value"], stderr=subprocess.DEVNULL, timeout=4, text=True)
                lines = [line.strip() for line in out.splitlines()]
                current = None
                for line in lines:
                    if not line:
                        continue
                    if line in units:
                        current = line
                    elif current:
                        states[current] = line
                        current = None
            except Exception:
                states = {}
            for unit in units:
                state = states.get(unit)
                if not state:
                    try:
                        state = subprocess.check_output(["systemctl", "is-active", unit], stderr=subprocess.DEVNULL, timeout=1, text=True).strip()
                    except Exception:
                        state = "unknown"
                items.append({"name": unit, "type": "systemd", "ok": state == "active", "state": state})
            probes = [
                cls._probe_local_http("monitor", "http://[REDACTED_IP]:9000/api/system/status"),
                cls._probe_local_http("dashboard", "http://[REDACTED_IP]:9100/api/status"),
                cls._probe_local_http("shared-memory", "http://[REDACTED_IP]:9400/health"),
                cls._probe_local_http("sub2api", "http://[REDACTED_IP]:8080/"),
                cls._probe_local_http("upstream-hub", "http://[REDACTED_IP]:8420/"),
            ]
            for p in probes:
                p["type"] = "http"
                items.append(p)
            ok_count = sum(1 for i in items if i.get("ok"))
            cls._service_cache = {"items": items, "ok_count": ok_count, "total": len(items), "ok": ok_count == len(items), "updated_at": now}
            cls._service_last_check = now
            return dict(cls._service_cache)

    @staticmethod
    def _db_file_item(name: str, path: str) -> dict:
        p = Path(path)
        wal = Path(path + "-wal")
        shm = Path(path + "-shm")
        item = {"name": name, "path": path, "exists": p.exists(), "db_mb": 0.0, "wal_mb": 0.0, "shm_mb": 0.0, "ok": p.exists()}
        try:
            if p.exists(): item["db_mb"] = round(p.stat().st_size / (1024 ** 2), 2)
            if wal.exists(): item["wal_mb"] = round(wal.stat().st_size / (1024 ** 2), 2)
            if shm.exists(): item["shm_mb"] = round(shm.stat().st_size / (1024 ** 2), 2)
            item["ok"] = item["exists"] and item["wal_mb"] < 64
        except Exception:
            item["ok"] = False
        return item

    @classmethod
    def _get_database_status(cls) -> dict:
        now = time.time()
        with cls._db_lock:
            if now - cls._db_last_check < 60:
                return dict(cls._db_cache)
            items = [
                cls._db_file_item("Hermes state", "/root/.hermes/state.db"),
                cls._db_file_item("Monitor metrics", "/opt/hermes-system-monitor/metrics.db"),
                cls._db_file_item("OpenClaw memory", "/root/.openclaw/memory/main.sqlite"),
                cls._db_file_item("OpenClaw state", "/root/.openclaw/state/openclaw.sqlite"),
                cls._db_file_item("Shared memory", "/opt/shared-agent-memory/data/memory.sqlite"),
                cls._db_file_item("Upstream hub", "/opt/upstream-hub-standalone/data/upstream-hub.db"),
                cls._db_file_item("Observability", "/opt/server-observability-index/observability.sqlite"),
            ]
            pg = {"name": "Sub2API Postgres", "exists": False, "db_mb": None, "wal_mb": None, "ok": False}
            try:
                out = subprocess.check_output(["docker", "exec", "sub2api-postgres", "sh", "-lc", "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc \"select pg_database_size(current_database());\""], stderr=subprocess.DEVNULL, timeout=6, text=True).strip()
                pg["exists"] = True
                pg["db_mb"] = round(int(out) / (1024 ** 2), 2) if out.isdigit() else None
                pg["ok"] = True
            except Exception:
                pass
            items.append(pg)
            total_db = sum(float(i.get("db_mb") or 0) for i in items)
            total_wal = sum(float(i.get("wal_mb") or 0) for i in items)
            cls._db_cache = {"items": items, "total_db_mb": round(total_db, 2), "total_wal_mb": round(total_wal, 2), "ok": all(i.get("ok") for i in items), "updated_at": now}
            cls._db_last_check = now
            return dict(cls._db_cache)

    @classmethod
    def _get_postgres_connections(cls) -> dict:
        """Return PostgreSQL active and max connection counts."""
        result = {"active": None, "max": None, "ok": False}
        try:
            # Get active connections
            active_out = subprocess.check_output(
                ["docker", "exec", "sub2api-postgres", "sh", "-lc",
                 "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc \"SELECT count(*) FROM pg_stat_activity;\""],
                stderr=subprocess.DEVNULL, timeout=3, text=True
            ).strip()
            result["active"] = int(active_out) if active_out.isdigit() else None
            
            # Get max connections
            max_out = subprocess.check_output(
                ["docker", "exec", "sub2api-postgres", "sh", "-lc",
                 "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc \"SHOW max_connections;\""],
                stderr=subprocess.DEVNULL, timeout=3, text=True
            ).strip()
            result["max"] = int(max_out) if max_out.isdigit() else None
            result["ok"] = True
        except Exception:
            pass
        return result

    @classmethod
    def _get_sub2api_netdev_text(cls, now: float) -> str:
        """Read sub2api /proc/net/dev without docker exec on every fast tick."""
        if not cls._sub2api_pid or now - cls._sub2api_pid_last_check > 60:
            try:
                out = subprocess.check_output(
                    ["docker", "inspect", "--format", "{{.State.Pid}}", "sub2api"],
                    stderr=subprocess.DEVNULL, timeout=2, text=True
                ).strip()
                cls._sub2api_pid = int(out) if out.isdigit() and int(out) > 0 else None
            except Exception:
                cls._sub2api_pid = None
            cls._sub2api_pid_last_check = now
        if cls._sub2api_pid:
            path = Path(f"/proc/{cls._sub2api_pid}/net/dev")
            try:
                return path.read_text()
            except Exception:
                cls._sub2api_pid = None
        # Fallback for unusual container namespace/PID cases.
        return subprocess.check_output(
            ["docker", "exec", "sub2api", "cat", "/proc/net/dev"],
            stderr=subprocess.DEVNULL, timeout=3, text=True
        )

    @classmethod
    def _get_sub2api_io(cls, interval: float) -> dict:
        """Return Sub2API container real-time network I/O rates (KB/s), calculated from /proc/net/dev inside the container."""
        now = time.time()
        with cls._sub2api_io_lock:
            rx_bytes = 0
            tx_bytes = 0
            try:
                out = cls._get_sub2api_netdev_text(now)
                for line in out.splitlines():
                    if "eth0:" in line or "enp" in line or "wlan" in line:
                        parts = line.split()
                        # eth0: rx_bytes rx_packets ... tx_bytes tx_packets ...
                        idx = parts.index(next(p for p in parts if p.endswith(':')))
                        rx_bytes = int(parts[idx + 1])
                        tx_bytes = int(parts[idx + 9])
                        break
            except Exception:
                cls._sub2api_io_cache["ok"] = False
                cls._sub2api_io_cache["updated_at"] = now
                cls._sub2api_io_last_check = now
                return dict(cls._sub2api_io_cache)

            prev = cls._sub2api_io_prev
            rx_rate = tx_rate = 0.0
            if prev and interval > 0:
                delta_rx = max(0, rx_bytes - prev.get("rx_bytes", 0))
                delta_tx = max(0, tx_bytes - prev.get("tx_bytes", 0))
                rx_rate = round(delta_rx / interval / 1024, 2)  # KB/s
                tx_rate = round(delta_tx / interval / 1024, 2)  # KB/s

            cls._sub2api_io_prev = {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}
            cls._sub2api_io_cache = {
                "rx_rate_kbps": rx_rate,
                "tx_rate_kbps": tx_rate,
                "total_rx_mb": round(rx_bytes / (1024 ** 2), 2),
                "total_tx_mb": round(tx_bytes / (1024 ** 2), 2),
                "ok": True,
                "updated_at": now,
            }
            cls._sub2api_io_last_check = now
            return dict(cls._sub2api_io_cache)

    @staticmethod
    def _default_sections() -> dict:
        """Default slower-section values used before background refresh finishes."""
        return {
            "top_processes": [],
            "oom_count": 0,
            "proxy": {"status_code": 0, "latency_ms": 0, "reachable": False},
            "docker": dict(SystemCollector._docker_cache),
            "path_growth": {"paths": [], "updated_at": 0.0},
            "data_freshness": {"items": [], "stale_count": 0, "ok": False},
            "dependency_quality": {"targets": [], "ok": False},
            "self_healing": {"events": [], "last_cleanup": None, "released_gb_today": 0.0, "ok": True},
            "docker_resources": dict(SystemCollector._docker_resource_cache),
            "service_health": {"items": [], "ok": False, "updated_at": 0.0},
            "database_status": {"items": [], "total_db_mb": 0.0, "total_wal_mb": 0.0, "ok": True, "updated_at": 0.0},
            "api_links": {"updated_at": 0.0, "items": [], "summary": {"ok": 0, "warn": 0, "bad": 0}, "baseline": {}},
            "http_errors": {"status_2xx": 0, "status_4xx": 0, "status_5xx": 0, "status_other": 0, "sample_lines": 0, "updated_at": 0.0},
            "psi_pressure": {},
            "systemd_failed": {"count": 0, "units": [], "ok": True},
            "postgres_connections": {"active": None, "max": None, "ok": False},
        }

    def _sections_snapshot(self) -> dict:
        with self._sections_lock:
            # Shallow copy is enough; callers never mutate nested dicts intentionally.
            return dict(self._sections)

    def _store_sections(self, updates: dict):
        with self._sections_lock:
            self._sections.update(updates)

    def _refresh_section_worker(self, name: str):
        try:
            if name == "medium":
                updates = {
                    "top_processes": self._get_top_processes(),
                    "oom_count": self._get_oom_count(),
                    "proxy": self._get_proxy_latency(),
                    "docker": self._get_docker_status(),
                    "docker_resources": self._get_docker_resource_status(),
                    "service_health": self._get_service_health(),
                    "api_links": self._probe_api_links(),
                    "http_errors": self._get_http_status_counts(),
                    "psi_pressure": self._get_psi_pressure(),
                    "systemd_failed": self._get_systemd_failed_units(),
                    "postgres_connections": self._get_postgres_connections(),
                }
            elif name == "slow":
                updates = {
                    "path_growth": self._get_path_growth(),
                    "data_freshness": self._get_data_freshness(),
                    "dependency_quality": self._get_external_dependency_quality(),
                    "self_healing": self._get_self_healing_events(),
                    "database_status": self._get_database_status(),
                }
            else:
                return
            self._store_sections(updates)
            self._section_last_refresh[name] = time.time()
        except Exception as exc:
            print(f"[collector:{name}] refresh error: {exc}", flush=True)
        finally:
            with self._sections_lock:
                self._section_refreshing.discard(name)

    def _maybe_refresh_sections(self, force: bool = False):
        """Kick slower collectors in background so fast 2s snapshots never block."""
        now = time.time()
        due = []
        if force or now - self._section_last_refresh.get("medium", 0.0) >= 10:
            due.append("medium")
        if force or now - self._section_last_refresh.get("slow", 0.0) >= 60:
            due.append("slow")
        for name in due:
            with self._sections_lock:
                if name in self._section_refreshing:
                    continue
                self._section_refreshing.add(name)
            threading.Thread(target=self._refresh_section_worker, args=(name,), daemon=True).start()

    def collect(self) -> dict:
        """Collect a fast snapshot and merge slower background-refreshed sections."""
        self._maybe_refresh_sections()
        sections = self._sections_snapshot()

        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        cpu = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
        cores = psutil.cpu_count(logical=True)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        data_disk = psutil.disk_usage("/www")
        load = os.getloadavg()
        interface = self._get_active_interface()
        swap = self._get_swap()
        conntrack = self._get_conntrack()
        fd_usage = self._get_fd_usage()
        boot_time = psutil.boot_time()
        api_links = sections.get("api_links") or self._default_sections()["api_links"]

        return {
            "cpu": {
                "percent": round(cpu, 1),
                "per_cpu": [round(x, 1) for x in per_cpu],
                "cores": cores,
            },
            "memory": {
                "percent": round(mem.percent, 1),
                "used_gb": round(mem.used / (1024 ** 3), 2),
                "total_gb": round(mem.total / (1024 ** 3), 2),
            },
            "swap": swap,
            "disk": {
                "percent": round(disk.percent, 1),
                "used_gb": round(disk.used / (1024 ** 3), 2),
                "total_gb": round(disk.total / (1024 ** 3), 2),
                "mount": "/",
                "read_rate_mbps": 0.0,
                "write_rate_mbps": 0.0,
                "read_iops": 0.0,
                "write_iops": 0.0,
                "await_ms": 0.0,
            },
            "data_disk": {
                "percent": round(data_disk.percent, 1),
                "used_gb": round(data_disk.used / (1024 ** 3), 2),
                "total_gb": round(data_disk.total / (1024 ** 3), 2),
                "mount": "/www",
            },
            "network": {
                "rx_rate_mbps": 0.0,
                "tx_rate_mbps": 0.0,
                "interface": interface,
                "totals": {},
            },
            "conntrack": conntrack,
            "fd_usage": fd_usage,
            "load": {
                "load_1": round(load[0], 2),
                "load_5": round(load[1], 2),
                "load_15": round(load[2], 2),
            },
            "top_processes": sections.get("top_processes", []),
            "oom_count": sections.get("oom_count", 0),
            "proxy": sections.get("proxy", {"status_code": 0, "latency_ms": 0, "reachable": False}),
            "docker": sections.get("docker", dict(self._docker_cache)),
            "path_growth": sections.get("path_growth", {"paths": [], "updated_at": 0.0}),
            "data_freshness": sections.get("data_freshness", {"items": [], "stale_count": 0, "ok": False}),
            "dependency_quality": sections.get("dependency_quality", {"targets": [], "ok": False}),
            "self_healing": sections.get("self_healing", {"events": [], "last_cleanup": None, "released_gb_today": 0.0, "ok": True}),
            "docker_resources": sections.get("docker_resources", dict(self._docker_resource_cache)),
            "service_health": sections.get("service_health", {"items": [], "ok": False, "updated_at": 0.0}),
            "database_status": sections.get("database_status", {"items": [], "total_db_mb": 0.0, "total_wal_mb": 0.0, "ok": True, "updated_at": 0.0}),
            "sub2api_io": {},
            "api_links": api_links,
            "http_errors": sections.get("http_errors", dict(self._http_error_cache)),
            "psi_pressure": sections.get("psi_pressure", {}),
            "systemd_failed": sections.get("systemd_failed", {"count": 0, "units": [], "ok": True}),
            "tcp_retrans": {},
            "postgres_connections": sections.get("postgres_connections", {"active": None, "max": None, "ok": False}),
            "performance_baseline": {
                "api_links": api_links.get("baseline", {}),
                "window_seconds": 3600,
            },
            "boot_time": boot_time,
        }

    def get_latest(self) -> dict:
        """Return the latest collected snapshot thread-safely."""
        with self._lock:
            return dict(self._latest)

    def _run(self):
        """Background collection loop."""
        prev_net = {}
        prev_disk = {}
        prev_tcp = {}
        while not self._stop_event.is_set():
            data = self.collect()

            # Fill in network rates using previous snapshot
            net = self._get_network_rates(prev_net, self.interval)
            data["network"]["rx_rate_mbps"] = net["rx_rate_mbps"]
            data["network"]["tx_rate_mbps"] = net["tx_rate_mbps"]
            data["network"]["bytes_recv"] = net["bytes_recv"]
            data["network"]["bytes_sent"] = net["bytes_sent"]
            data["network"]["totals"] = self._get_network_totals(net["bytes_recv"], net["bytes_sent"], net["rx_rate_mbps"], net["tx_rate_mbps"])
            prev_net = {"bytes_recv": net["bytes_recv"], "bytes_sent": net["bytes_sent"]}

            disk_io = self._get_disk_io_rates(prev_disk, self.interval)
            data["disk"]["read_rate_mbps"] = disk_io["read_rate_mbps"]
            data["disk"]["write_rate_mbps"] = disk_io["write_rate_mbps"]
            data["disk"]["read_iops"] = disk_io["read_iops"]
            data["disk"]["write_iops"] = disk_io["write_iops"]
            data["disk"]["await_ms"] = disk_io["await_ms"]
            prev_disk = {"read_bytes": disk_io["read_bytes"], "write_bytes": disk_io["write_bytes"], "read_count": disk_io["read_count"], "write_count": disk_io["write_count"], "read_time": disk_io["read_time"], "write_time": disk_io["write_time"]}

            tcp = self._get_tcp_retrans(prev_tcp, self.interval)
            data["tcp_retrans"] = tcp
            prev_tcp = {"retrans_segs": tcp["retrans_segs"], "out_segs": tcp["out_segs"]}

            sub2api_io = self._get_sub2api_io(self.interval)
            data["sub2api_io"] = sub2api_io

            with self._lock:
                self._latest = data

            # Sleep in small increments so we can stop promptly
            elapsed = 0.0
            step = 0.2
            while elapsed < self.interval and not self._stop_event.is_set():
                time.sleep(min(step, self.interval - elapsed))
                elapsed += step

    def start(self):
        """Start the background collection thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        # Prime the first CPU percent reading
        psutil.cpu_percent(interval=0.1)
        # Prime network counters
        self._stop_event.clear()
        self._maybe_refresh_sections(force=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the background thread to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


# Module-level singleton
collector = SystemCollector(interval=2.0)
