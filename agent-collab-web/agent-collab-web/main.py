#!/usr/bin/env python3
"""
Agent Collab Web — lightweight task rooms for Hermes + OpenClaw collaboration.
Port: 7000
"""

from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "[REDACTED_IP]"
PORT = int(os.environ.get("AGENT_COLLAB_PORT", "7000"))
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DB_PATH = Path(os.environ.get("AGENT_COLLAB_DB", str(APP_DIR / "collab.db")))
AGENTS = {
    "hermes": {
        "name": "Hermes",
        "role": "执行 / 服务器操作 / Dashboard 改造",
        "status": "manual",
        "color": "#f59e0b",
    },
    "openclaw": {
        "name": "OpenClaw",
        "role": "审查 / 验证 / 规划 / 辅助执行",
        "status": "manual",
        "color": "#58a6ff",
    },
}


def now() -> float:
    return time.time()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                objective TEXT NOT NULL DEFAULT '',
                main_agent TEXT NOT NULL DEFAULT 'hermes',
                helper_agent TEXT NOT NULL DEFAULT 'openclaw',
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT 'room',
                kind TEXT NOT NULL DEFAULT 'note',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_task_time ON messages(task_id, created_at)")


def row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_task(title: str, objective: str, main_agent: str, helper_agent: str) -> dict:
    task_id = uuid.uuid4().hex[:12]
    timestamp = now()
    main_agent = main_agent if main_agent in AGENTS else "hermes"
    helper_agent = helper_agent if helper_agent in AGENTS else "openclaw"
    if helper_agent == main_agent:
        helper_agent = "openclaw" if main_agent == "hermes" else "hermes"
    with db() as conn:
        conn.execute(
            "INSERT INTO tasks(id,title,objective,main_agent,helper_agent,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, title or "未命名任务", objective or "", main_agent, helper_agent, "active", timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO messages(id,task_id,sender,target,kind,content,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                task_id,
                "system",
                "room",
                "system",
                f"任务已创建。主 Agent：{AGENTS[main_agent]['name']}；辅助 Agent：{AGENTS[helper_agent]['name']}。",
                timestamp,
            ),
        )
    return get_task(task_id)


def list_tasks() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 100").fetchall()
        tasks = [row_dict(row) for row in rows]
        for item in tasks:
            item["message_count"] = conn.execute("SELECT COUNT(*) FROM messages WHERE task_id=?", (item["id"],)).fetchone()[0]
        return tasks


def get_task(task_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("task not found")
        messages = [row_dict(msg) for msg in conn.execute("SELECT * FROM messages WHERE task_id=? ORDER BY created_at ASC", (task_id,)).fetchall()]
        task = row_dict(row)
        task["messages"] = messages
        return task


def add_message(task_id: str, sender: str, target: str, kind: str, content: str) -> dict:
    if not content.strip():
        raise ValueError("content is required")
    sender = sender or "user"
    target = target or "room"
    kind = kind or "note"
    timestamp = now()
    msg = {
        "id": uuid.uuid4().hex,
        "task_id": task_id,
        "sender": sender,
        "target": target,
        "kind": kind,
        "content": content,
        "created_at": timestamp,
    }
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not exists:
            raise KeyError("task not found")
        conn.execute(
            "INSERT INTO messages(id,task_id,sender,target,kind,content,created_at) VALUES(?,?,?,?,?,?,?)",
            (msg["id"], task_id, sender, target, kind, content, timestamp),
        )
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (timestamp, task_id))
    return msg


def update_task(task_id: str, status: str | None = None) -> dict:
    if status not in {"active", "paused", "done", None}:
        raise ValueError("invalid status")
    with db() as conn:
        if status:
            conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, now(), task_id))
    return get_task(task_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentCollabWeb/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {fmt % args}")

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        body = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_json({"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if route == "/api/status":
                self.send_json({"ok": True, "port": PORT, "db": str(DB_PATH), "agents": AGENTS, "time": now()})
                return
            if route == "/api/tasks":
                self.send_json({"tasks": list_tasks()})
                return
            if route.startswith("/api/tasks/"):
                task_id = route.split("/")[-1]
                self.send_json(get_task(task_id))
                return
            if route == "/" or route == "/index.html":
                self.send_static(STATIC_DIR / "index.html")
                return
            if route.startswith("/static/"):
                self.send_static(STATIC_DIR / Path(route).name)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        body = self.read_body()
        try:
            if route == "/api/tasks":
                task = create_task(
                    body.get("title", ""),
                    body.get("objective", ""),
                    body.get("main_agent", "hermes"),
                    body.get("helper_agent", "openclaw"),
                )
                self.send_json(task, 201)
                return
            if route.startswith("/api/tasks/") and route.endswith("/messages"):
                task_id = route.split("/")[-2]
                msg = add_message(
                    task_id,
                    body.get("sender", "user"),
                    body.get("target", "room"),
                    body.get("kind", "note"),
                    body.get("content", ""),
                )
                self.send_json(msg, 201)
                return
            if route.startswith("/api/tasks/"):
                task_id = route.split("/")[-1]
                self.send_json(update_task(task_id, body.get("status")))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)


if __name__ == "__main__":
    init_db()
    print(f"Agent Collab Web running on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
