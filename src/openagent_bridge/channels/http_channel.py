"""Universal HTTP REST Channel Adapter."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from openagent_bridge.channels.base import BaseChannelAdapter
from openagent_bridge.models import UnifiedTaskRequest

logger = logging.getLogger(__name__)


def _make_http_handler(adapter: HTTPChannelAdapter):
    class BridgeHTTPRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default server access logs

        def _send_json(self, status_code: int, data: Any):
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        def _get_auth_user(self) -> str:
            """Extract and validate authentication token."""
            core = adapter.core
            if not core:
                return "anonymous"

            auth_header = self.headers.get("Authorization", "")
            api_key = self.headers.get("X-API-Key", "")
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
            elif api_key:
                token = api_key.strip()

            if core.config.auth.enabled and core.config.auth.api_tokens:
                if token not in core.config.auth.api_tokens:
                    return ""

            user = core.acl.authenticate("http", token or "anonymous")
            return user.id if user else "anonymous"

        def do_GET(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            if not parts or parts[0] == "health":
                self._send_json(200, {"status": "ok", "channel": "http"})
                return

            user_id = self._get_auth_user()
            if not user_id:
                self._send_json(401, {"error": "Unauthorized: Invalid or missing API key"})
                return

            core = adapter.core
            if not core:
                self._send_json(503, {"error": "Bridge core not ready"})
                return

            # GET /api/v1/projects
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "projects":
                projects = core.get_projects(user_id=user_id)
                self._send_json(200, {"projects": projects})
                return

            # GET /api/v1/tasks/<id> or /tasks/<id>/events
            if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "tasks":
                task_id = parts[3]
                if len(parts) == 5 and parts[4] == "events":
                    events = core.events.get_events(task_id)
                    self._send_json(200, {"task_id": task_id, "events": events})
                    return
                task = core.db.get_task(task_id)
                if task:
                    self._send_json(200, task)
                else:
                    self._send_json(404, {"error": "Task not found"})
                return

            # Legacy compatibility /tasks/<id>
            if parts[0] == "tasks" and len(parts) >= 2:
                task_id = parts[1]
                if len(parts) == 3 and parts[2] == "events":
                    events = core.events.get_events(task_id)
                    self._send_json(200, {"task_id": task_id, "events": events})
                    return
                task = core.db.get_task(task_id)
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

            user_id = self._get_auth_user()
            if not user_id:
                self._send_json(401, {"error": "Unauthorized: Invalid or missing API key"})
                return

            core = adapter.core
            if not core:
                self._send_json(503, {"error": "Bridge core not ready"})
                return

            # POST /api/v1/tasks (or /tasks)
            if (len(parts) == 3 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "tasks") or (
                len(parts) == 1 and parts[0] == "tasks"
            ):
                req = UnifiedTaskRequest(
                    project_id=data.get("project_id", data.get("project_name", "")),
                    user_goal=data.get("goal", data.get("user_goal", "")),
                    user_id=user_id,
                    source_channel="http",
                    idempotency_key=data.get("idempotency_key"),
                    non_goals=data.get("non_goals", []),
                    constraints=data.get("constraints", []),
                    acceptance=data.get("acceptance", {}),
                    executor=data.get("executor", {}),
                    budget=data.get("budget", {}),
                    permissions=data.get("permissions", {}),
                    reply_target=data.get("reply_target", {}),
                )
                ok, task_id, err = core.submit_unified_task(req)
                if ok:
                    self._send_json(201, {"task_id": task_id, "status": "QUEUED"})
                else:
                    self._send_json(403, {"error": err})
                return

            # POST /api/v1/tasks/<id>/cancel (or /tasks/<id>/cancel)
            if (len(parts) == 5 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "tasks" and parts[4] == "cancel") or (
                len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel"
            ):
                task_id = parts[1] if parts[0] == "tasks" else parts[3]
                reason = data.get("reason", "Cancelled via HTTP")
                ok, msg = core.cancel_task(task_id, user_id=user_id, reason=reason)
                if ok:
                    self._send_json(200, {"task_id": task_id, "cancelled": True})
                else:
                    self._send_json(400, {"task_id": task_id, "cancelled": False, "error": msg})
                return

            # POST /api/v1/approvals/<id>/resolve
            if len(parts) == 5 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "approvals" and parts[4] == "resolve":
                approval_id = parts[3] or data.get("approval_id", "")
                approved = bool(data.get("approved", False))
                reason = data.get("reason", "")
                ok, msg = core.resolve_approval(approval_id, user_id=user_id, approved=approved, reason=reason)
                if ok:
                    self._send_json(200, {"approval_id": approval_id, "resolved": True})
                else:
                    self._send_json(400, {"approval_id": approval_id, "resolved": False, "error": msg})
                return

            self._send_json(404, {"error": "Endpoint not found"})

    return BridgeHTTPRequestHandler


class HTTPChannelAdapter(BaseChannelAdapter):
    """Universal HTTP channel adapter."""

    channel_name: str = "http"

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.sent_updates: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._running:
            return
        handler_cls = _make_http_handler(self)
        self.server = HTTPServer((self.host, self.port), handler_cls)
        self._running = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("HTTP Channel Adapter running on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._running = False

    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        self.sent_updates.append({"type": "message", "reply_target": reply_target, "text": text})
        return True

    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        self.sent_updates.append({
            "type": "task_update",
            "task_id": task.get("id"),
            "status": task.get("status"),
            "message": message,
        })
        return True

    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        self.sent_updates.append({
            "type": "approval_request",
            "approval": approval,
            "reply_target": reply_target,
        })
        return True
