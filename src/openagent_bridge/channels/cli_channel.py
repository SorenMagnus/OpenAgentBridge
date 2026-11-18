"""Local CLI channel adapter."""

from __future__ import annotations

import logging
from typing import Any

from openagent_bridge.channels.base import BaseChannelAdapter

logger = logging.getLogger(__name__)


class CLIChannelAdapter(BaseChannelAdapter):
    """Channel adapter representing local terminal/CLI interactions."""

    channel_name: str = "cli"

    def __init__(self) -> None:
        super().__init__()
        self.sent_messages: list[tuple[dict[str, Any], str]] = []

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        self.sent_messages.append((reply_target, text))
        logger.info("[CLI CHANNEL] %s", text)
        return True

    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        reply_target = task.get("reply_target") or {}
        return self.send_message(reply_target, f"[Task {task.get('id')}] {message}")

    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        text = (
            f"[APPROVAL REQUIRED] Action: {approval.get('action')}\n"
            f"Approval ID: {approval.get('id')}\n"
            f"Run: openagent approve {approval.get('id')}"
        )
        return self.send_message(reply_target, text)
