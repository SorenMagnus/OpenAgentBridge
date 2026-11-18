"""Discord Channel Adapter interface skeleton."""

from __future__ import annotations

import logging
from typing import Any

from openagent_bridge.channels.base import BaseChannelAdapter

logger = logging.getLogger(__name__)


class DiscordBotAdapter(BaseChannelAdapter):
    """Skeleton adapter for Discord Bot integration via Gateway WebSocket or Webhooks.

    To implement:
    1. Authenticate with Discord Bot Token (`Authorization: Bot <token>`).
    2. Maintain WebSocket connection to Discord Gateway (`wss://gateway.discord.gg`).
    3. Listen for `MESSAGE_CREATE` / `INTERACTION_CREATE` (Slash Commands).
    4. Convert interactions into `UnifiedTaskRequest`.
    5. Send feedback via Discord REST API (`/channels/{channel.id}/messages`).
    """

    channel_name: str = "discord"

    def __init__(self, bot_token: str = "") -> None:
        super().__init__()
        self.bot_token = bot_token

    def start(self) -> None:
        self._running = True
        logger.info("Discord Bot Adapter skeleton initialized (standby)")

    def stop(self) -> None:
        self._running = False

    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        channel_id = reply_target.get("channel_id")
        logger.info("[DISCORD MOCK] Send to channel %s: %s", channel_id, text)
        return True

    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        reply_target = task.get("reply_target") or {}
        return self.send_message(reply_target, f"[Task {task.get('id')}] {message}")

    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        return self.send_message(reply_target, f"[APPROVAL REQUIRED] {approval.get('id')}")
