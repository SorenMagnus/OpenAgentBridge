"""Loopback HTTP server for external controller task intake."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from openagent_bridge.db import Database
from openagent_bridge.events import EventManager
from openagent_bridge.queue import TaskQueue


def _make_handler(queue: TaskQueue, db: Database, em: EventManager):
    class BridgeHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default server access logs

        def _send_json(self, status_code: int, data: Any):
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            if not parts or parts[0] == "health":
                self._send_json(200, {"status": "ok", "service": "openagent-bridge"})
                return

            if parts[0] == "tasks" and len(parts) >= 2:
                task_id = parts[1]
                if len(parts) == 3 and parts[2] == "events":
                    events = em.get_events(task_id)
                    self._send_json(200, {"task_id": task_id, "events": events})
                    return
                task = db.get_task(task_id)
                if task:
                    self._send_json(200, task)
                else:
                    self._send_json(404, {"error": "Task not found"})
                return

            self._send_json(404, {"error": "Endpoint not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"

            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json(400, {"error": "Invalid JSON payload"})
                return

            if parts and parts[0] == "tasks":
                if len(parts) == 1:
                    # Enqueue task
                    task_id = queue.enqueue(data)
                    self._send_json(201, {"task_id": task_id, "status": "QUEUED"})
                    return
                elif len(parts) == 3 and parts[2] == "cancel":
                    task_id = parts[1]
                    success = queue.cancel(task_id, reason=data.get("reason", "Cancelled via HTTP"))
                    self._send_json(200, {"task_id": task_id, "cancelled": success})
                    return

            self._send_json(404, {"error": "Endpoint not found"})

    return BridgeHTTPHandler


class LocalHTTPTransport:
    """Manages a loopback HTTP intake service."""

    def __init__(self, db: Database, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.db = db
        self.queue = TaskQueue(db)
        self.em = EventManager(db)
        self.host = host
        self.port = port
        self.server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler_cls = _make_handler(self.queue, self.db, self.em)
        self.server = HTTPServer((self.host, self.port), handler_cls)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
