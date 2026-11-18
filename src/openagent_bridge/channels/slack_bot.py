"""Slack Channel Adapter interface skeleton."""

from __future__ import annotations

import logging
from typing import Any

from openagent_bridge.channels.base import BaseChannelAdapter

logger = logging.getLogger(__name__)


class SlackBotAdapter(BaseChannelAdapter):
    """Skeleton adapter for Slack Bot integration via Events API or Socket Mode.

    To implement:
    1. Authenticate with Bot Token (`xoxb-...`) or App-Level Token (`xapp-...`).
    2. Use Socket Mode (`slack_bolt.App`) or configure an HTTPS Request URL for Slack Events API.
    3. Listen for `app_mention` and Slash Commands (e.g. `/openagent task ...`).
    4. Convert message into `UnifiedTaskRequest`.
    5. Post Block Kit message responses back via `chat.postMessage`.
    """

    channel_name: str = "slack"

    def __init__(self, bot_token: str = "", app_token: str = "") -> None:
        super().__init__()
        self.bot_token = bot_token
        self.app_token = app_token

    def start(self) -> None:
        self._running = True
        logger.info("Slack Bot Adapter skeleton initialized (standby)")

    def stop(self) -> None:
        self._running = False

    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        channel_id = reply_target.get("channel_id")
        logger.info("[SLACK MOCK] Send to channel %s: %s", channel_id, text)
        return True

    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        reply_target = task.get("reply_target") or {}
        return self.send_message(reply_target, f"[Task {task.get('id')}] {message}")

    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        return self.send_message(reply_target, f"[APPROVAL REQUIRED] {approval.get('id')}")
