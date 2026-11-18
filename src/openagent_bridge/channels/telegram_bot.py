"""Telegram Bot Channel Adapter for remote mobile task dispatch and feedback."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from openagent_bridge.channels.base import BaseChannelAdapter
from openagent_bridge.config import TelegramConfig
from openagent_bridge.models import UnifiedTaskRequest

logger = logging.getLogger(__name__)


class TelegramBotAdapter(BaseChannelAdapter):
    """Channel adapter for Telegram Bot API (Long-Polling & Webhooks)."""

    channel_name: str = "telegram"

    def __init__(self, telegram_config: TelegramConfig | None = None) -> None:
        super().__init__()
        self.telegram_config = telegram_config or TelegramConfig()
        self._thread: threading.Thread | None = None
        self._last_update_id: int = 0
        self._bot_info: dict[str, Any] = {}

    def _make_opener(self) -> urllib.request.OpenerDirector:
        """Create URL opener with optional proxy configuration."""
        handlers: list[urllib.request.BaseHandler] = []
        proxy = self.telegram_config.proxy_url
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return urllib.request.build_opener(*handlers)

    def _api_request(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
        """Execute a Telegram Bot API request."""
        token = self.telegram_config.bot_token
        if not token:
            raise ValueError("Telegram bot_token is empty or not configured")

        base_url = self.telegram_config.api_base_url.rstrip("/")
        url = f"{base_url}/bot{token}/{method}"

        headers = {"Content-Type": "application/json; charset=utf-8"}
        data = json.dumps(payload or {}).encode("utf-8") if payload else None

        req = urllib.request.Request(url, data=data, headers=headers)
        opener = self._make_opener()
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)

    def get_me(self) -> dict[str, Any]:
        """Fetch bot identity to verify token."""
        return self._api_request("getMe")

    def start(self) -> None:
        """Start the background long-polling worker thread."""
        if not self.telegram_config.enabled:
            logger.info("Telegram channel is disabled in configuration")
            return
        if not self.telegram_config.bot_token:
            logger.warning("Telegram channel enabled but no bot_token provided")
            return
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram Bot Adapter started (polling)")

    def stop(self) -> None:
        """Signal polling loop to terminate."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Telegram Bot Adapter stopped")

    def _poll_loop(self) -> None:
        """Continuous long-polling loop with error backoff."""
        while self._running:
            try:
                updates = self._fetch_updates(offset=self._last_update_id + 1, timeout=10)
                for update in updates:
                    uid = update.get("update_id", 0)
                    if uid > self._last_update_id:
                        self._last_update_id = uid
                    self.handle_incoming_update(update)
            except Exception as exc:
                if not self._running:
                    break
                logger.error("Error in Telegram poll loop: %s", exc)
                time.sleep(2.0)
            time.sleep(self.telegram_config.poll_interval_seconds)

    def _fetch_updates(self, offset: int, timeout: int = 10) -> list[dict[str, Any]]:
        payload = {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        res = self._api_request("getUpdates", payload=payload, timeout=timeout + 5)
        return res.get("result", [])

    def is_user_authorized(self, sender_id: str | int, chat_id: str | int) -> bool:
        """Check if user or chat is whitelisted."""
        int_uid = int(sender_id) if str(sender_id).isdigit() else None
        int_cid = int(chat_id) if str(chat_id).lstrip("-").isdigit() else None

        allowed_users = self.telegram_config.allowed_user_ids
        allowed_chats = self.telegram_config.allowed_chat_ids

        # If whitelist is completely empty, default deny for safety
        if not allowed_users and not allowed_chats:
            return False

        if int_uid and allowed_users and int_uid in allowed_users:
            return True
        if int_cid and allowed_chats and int_cid in allowed_chats:
            return True
        return False

    def handle_incoming_update(self, update: dict[str, Any]) -> None:
        """Parse and process a single Telegram update."""
        msg = update.get("message")
        if not msg:
            return

        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        user = msg.get("from", {})
        sender_id = user.get("id")
        text = (msg.get("text") or "").strip()

        if not chat_id or not sender_id or not text:
            return

        # Security check: Whitelist authorization
        if not self.is_user_authorized(sender_id, chat_id):
            logger.warning("Rejected unauthorized Telegram user: %s (chat %s)", sender_id, chat_id)
            self.send_message(
                {"chat_id": chat_id},
                f"⛔ **Access Denied**\n\nUnauthorized Telegram User ID: `{sender_id}`.\n"
                "Please configure this ID in `channels.telegram.allowed_user_ids`."
            )
            return

        # Parse command
        self._dispatch_command(chat_id, sender_id, text, msg.get("message_id"))

    def _dispatch_command(
        self, chat_id: int | str, sender_id: int | str, text: str, msg_id: int | None
    ) -> None:
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower().split("@")[0]  # strip bot handle e.g. /task@bot

        reply_target = {"chat_id": chat_id, "message_id": msg_id}
        user_identifier = f"telegram:{sender_id}"

        if cmd in ("/start", "/help"):
            help_text = (
                "🤖 **OpenAgent Bridge — Remote Engineering Bot**\n\n"
                "Commands:\n"
                "• `/projects` — List registered projects\n"
                "• `/task <project> <goal>` — Submit a new coding task\n"
                "• `/status <task_id>` — Query task status\n"
                "• `/report <task_id>` — View test verification report\n"
                "• `/cancel <task_id>` — Cancel running task\n"
                "• `/approve <approval_id>` — Approve pending operation\n"
                "• `/reject <approval_id>` — Reject pending operation"
            )
            self.send_message(reply_target, help_text)
            return

        if cmd == "/projects":
            if not self.core:
                self.send_message(reply_target, "⚠️ Core orchestrator not attached.")
                return
            projects = self.core.get_projects(user_identifier)
            if not projects:
                self.send_message(reply_target, "📂 No registered projects found.")
                return
            lines = ["📂 **Available Projects:**"]
            for p in projects:
                lines.append(f"• `{p.get('name')}` — Path: `{p.get('path')}`")
            self.send_message(reply_target, "\n".join(lines))
            return

        if cmd == "/task":
            if len(parts) < 3:
                self.send_message(
                    reply_target,
                    "⚠️ **Usage:** `/task <project_id> <task_goal>`\n\nExample:\n`/task demo Fix calculation bug in app.py`"
                )
                return
            project_id = parts[1]
            goal = parts[2]

            req = UnifiedTaskRequest(
                project_id=project_id,
                user_goal=goal,
                user_id=user_identifier,
                source_channel="telegram",
                reply_target=reply_target,
            )
            ok, task_id, err = self.core.submit_unified_task(req)
            if ok:
                self.send_message(
                    reply_target,
                    f"🚀 **Task Enqueued Successfully!**\n\n"
                    f"• **Task ID:** `{task_id}`\n"
                    f"• **Project:** `{project_id}`\n"
                    f"• **Goal:** {goal}\n\n"
                    f"Use `/status {task_id}` to check real-time progress."
                )
            else:
                self.send_message(reply_target, f"❌ **Task Submission Failed:**\n{err}")
            return

        if cmd == "/status":
            if len(parts) < 2:
                self.send_message(reply_target, "⚠️ **Usage:** `/status <task_id>`")
                return
            task_id = parts[1]
            task = self.core.db.get_task(task_id)
            if not task:
                self.send_message(reply_target, f"❌ Task `{task_id}` not found.")
                return

            status = task.get("status")
            iteration = task.get("iteration", 0)
            branch = task.get("branch") or "N/A"
            status_emoji = {
                "SUCCEEDED": "✅",
                "FAILED": "❌",
                "RUNNING": "⏳",
                "VERIFYING": "🔍",
                "REVIEW_REQUIRED": "⚠️",
                "CANCELLED": "🛑",
                "BUDGET_EXCEEDED": "⌛",
            }.get(status, "ℹ️")

            msg = (
                f"{status_emoji} **Task Status:** `{status}`\n\n"
                f"• **Task ID:** `{task_id}`\n"
                f"• **Iteration:** `{iteration}`\n"
                f"• **Branch:** `{branch}`\n"
                f"• **Goal:** {task.get('goal')}\n"
            )
            self.send_message(reply_target, msg)
            return

        if cmd == "/report":
            if len(parts) < 2:
                self.send_message(reply_target, "⚠️ **Usage:** `/report <task_id>`")
                return
            task_id = parts[1]
            report = self.core.get_task_report(task_id, user_id=user_identifier)
            if not report:
                self.send_message(reply_target, f"❌ No report found for `{task_id}`.")
                return

            v_results = report.get("verification_results", [])
            passed_count = sum(1 for v in v_results if v.get("passed"))
            total_count = len(v_results)

            lines = [
                f"📊 **Verification Report for** `{task_id}`",
                f"• Status: `{report.get('status')}`",
                f"• Checks Passed: `{passed_count}/{total_count}`",
                f"• Iterations: `{report.get('iteration')}`\n",
            ]
            for v in v_results:
                icon = "✅" if v.get("passed") else "❌"
                lines.append(f"{icon} `{v.get('check_name')}` (Exit: {v.get('exit_code')})")

            self.send_message(reply_target, "\n".join(lines))
            return

        if cmd == "/cancel":
            if len(parts) < 2:
                self.send_message(reply_target, "⚠️ **Usage:** `/cancel <task_id>`")
                return
            task_id = parts[1]
            ok, msg = self.core.cancel_task(task_id, user_id=user_identifier, reason="Cancelled via Telegram")
            icon = "🛑" if ok else "⚠️"
            self.send_message(reply_target, f"{icon} {msg}")
            return

        if cmd in ("/approve", "/reject"):
            if len(parts) < 2:
                self.send_message(reply_target, f"⚠️ **Usage:** `{cmd} <approval_id>`")
                return
            app_id = parts[1]
            approved = (cmd == "/approve")
            ok, msg = self.core.resolve_approval(app_id, user_id=user_identifier, approved=approved, reason=f"Resolved via Telegram {cmd}")
            icon = "✅" if ok and approved else ("🚫" if ok else "⚠️")
            self.send_message(reply_target, f"{icon} {msg}")
            return

        self.send_message(
            reply_target,
            f"❓ Unknown command: `{cmd}`. Send `/help` to view valid commands."
        )

    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        """Send formatted Telegram message."""
        chat_id = reply_target.get("chat_id")
        if not chat_id:
            logger.warning("Cannot send Telegram message: missing chat_id in reply_target")
            return False

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        msg_id = reply_target.get("message_id")
        if msg_id:
            payload["reply_to_message_id"] = msg_id

        try:
            self._api_request("sendMessage", payload=payload)
            return True
        except Exception as exc:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
            return False

    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        """Notify user on task lifecycle changes."""
        reply_target = task.get("reply_target") or {}
        if isinstance(reply_target, str):
            try:
                reply_target = json.loads(reply_target)
            except Exception:
                reply_target = {}

        chat_id = reply_target.get("chat_id")
        if not chat_id:
            return False

        status = task.get("status")
        icon = {
            "SUCCEEDED": "🎉",
            "FAILED": "💥",
            "REVIEW_REQUIRED": "⚠️",
            "BUDGET_EXCEEDED": "⌛",
        }.get(status, "🔔")

        text = (
            f"{icon} **Task Update —** `{task.get('id')}`\n\n"
            f"• **Status:** `{status}`\n"
            f"• **Detail:** {message}\n"
            f"• **Iterations:** `{task.get('iteration', 0)}`"
        )
        return self.send_message(reply_target, text)

    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        """Send an urgent approval prompt."""
        chat_id = reply_target.get("chat_id")
        if not chat_id:
            return False

        text = (
            f"🚨 **Action Approval Required!**\n\n"
            f"• **Approval ID:** `{approval.get('id')}`\n"
            f"• **Action:** `{approval.get('action')}`\n"
            f"• **Task ID:** `{approval.get('task_id')}`\n\n"
            f"To allow: `/approve {approval.get('id')}`\n"
            f"To reject: `/reject {approval.get('id')}`"
        )
        return self.send_message(reply_target, text)
