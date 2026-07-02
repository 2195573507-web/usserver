#!/usr/bin/env python3
"""
Server File Structure Web — read-only filesystem tree browser.
Port: 9300
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import stat
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOST = "[REDACTED_IP]"
PORT = int(os.environ.get("SERVER_FILE_WEB_PORT", "9300"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_ROOT = Path(os.environ.get("SERVER_FILE_WEB_ROOT", "/")).resolve()
MAX_CHILDREN = int(os.environ.get("SERVER_FILE_WEB_MAX_CHILDREN", "500"))
SKIP_DIR_NAMES = {
    ".cache",
    ".git",
    "__pycache__",
    "node_modules",
    "proc",
    "run",
    "sys",
    "dev",
}
QUICK_PATHS = [
    "/",
    "/root",
    "/root/.openclaw",
    "/root/.hermes",
    "/root/obsidian-vault",
    "/opt",
    "/var/log",
    "/etc",
]


def human_size(size: int | None) -> str:
    if size is None:
        return "--"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def safe_resolve(raw_path: str | None) -> Path:
    if not raw_path:
        return DEFAULT_ROOT
    raw_path = unquote(raw_path).strip() or "/"
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    return Path(raw_path).resolve()


def file_type(path: Path, mode: int | None = None) -> str:
    if mode is None:
        try:
            mode = path.lstat().st_mode
        except OSError:
            return "unknown"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "char"
    if stat.S_ISBLK(mode):
        return "block"
    return "other"


def entry_info(path: Path) -> dict:
    try:
        st = path.lstat()
        mode = st.st_mode
        kind = file_type(path, mode)
        target = None
        if kind == "link":
            try:
                target = os.readlink(path)
            except OSError:
                target = None
        return {
            "name": path.name or "/",
            "path": str(path),
            "type": kind,
            "size": st.st_size if kind != "dir" else None,
            "size_human": human_size(st.st_size if kind != "dir" else None),
            "mtime": st.st_mtime,
            "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "mode": oct(stat.S_IMODE(mode)),
            "readable": os.access(path, os.R_OK),
            "executable": os.access(path, os.X_OK),
            "target": target,
        }
    except OSError as exc:
        return {
            "name": path.name or "/",
            "path": str(path),
            "type": "error",
            "size": None,
            "size_human": "--",
            "mtime": 0,
            "mtime_text": "--",
            "mode": "--",
            "readable": False,
            "executable": False,
            "target": None,
            "error": str(exc),
        }


def list_dir(path: Path) -> dict:
    info = entry_info(path)
    if not path.exists():
        return {"ok": False, "error": "路径不存在", "path": str(path), "entry": info, "children": []}
    if not path.is_dir():
        return {"ok": True, "path": str(path), "entry": info, "children": [], "is_file": True}
    children = []
    errors = []
    try:
        with os.scandir(path) as iterator:
            for item in iterator:
                child = Path(item.path)
                children.append(entry_info(child))
                if len(children) >= MAX_CHILDREN:
                    break
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(path), "entry": info, "children": []}

    children.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    return {
        "ok": True,
        "path": str(path),
        "entry": info,
        "parent": str(path.parent) if str(path) != "/" else None,
        "children": children,
        "truncated": len(children) >= MAX_CHILDREN,
        "errors": errors,
    }


def scan_summary(base_paths: list[str]) -> dict:
    results = []
    for raw in base_paths:
        path = safe_resolve(raw)
        dirs = 0
        files = 0
        total_size = 0
        skipped = 0
        started = time.time()
        for root, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
            root_path = Path(root)
            if root_path.name in SKIP_DIR_NAMES and root_path != path:
                skipped += 1
                dirnames[:] = []
                continue
            dirs += len(dirnames)
            files += len(filenames)
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
            for filename in filenames:
                try:
                    total_size += (root_path / filename).lstat().st_size
                except OSError:
                    skipped += 1
            if time.time() - started > 3:
                skipped += 1
                break
        results.append({
            "path": str(path),
            "exists": path.exists(),
            "dirs": dirs,
            "files": files,
            "size": total_size,
            "size_human": human_size(total_size),
            "skipped": skipped,
        })
    return {"items": results, "quick_paths": [p for p in QUICK_PATHS if Path(p).exists()]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ServerFileWeb/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {fmt % args}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/api/status":
            self.send_json({
                "ok": True,
                "root": str(DEFAULT_ROOT),
                "port": PORT,
                "quick_paths": [p for p in QUICK_PATHS if Path(p).exists()],
                "time": time.time(),
            })
            return

        if route == "/api/list":
            target = safe_resolve(query.get("path", ["/"])[0])
            self.send_json(list_dir(target))
            return

        if route == "/api/summary":
            paths = query.get("path") or ["/root", "/opt", "/etc", "/var/log"]
            self.send_json(scan_summary(paths[:12]))
            return

        if route == "/" or route == "/index.html":
            self.send_file(STATIC_DIR / "index.html")
            return

        if route.startswith("/static/"):
            name = html.escape(route.rsplit("/", 1)[-1])
            self.send_file(STATIC_DIR / name)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")


if __name__ == "__main__":
    print(f"Server File Structure Web running on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
