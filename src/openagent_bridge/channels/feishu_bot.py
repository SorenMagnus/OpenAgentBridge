"""Feishu (Lark) Channel Adapter interface skeleton."""

from __future__ import annotations

import logging
from typing import Any

from openagent_bridge.channels.base import BaseChannelAdapter

logger = logging.getLogger(__name__)


class FeishuBotAdapter(BaseChannelAdapter):
    """Skeleton adapter for Feishu (Lark) Open Platform integration.

    To implement:
    1. Authenticate with `app_id` and `app_secret` to obtain `tenant_access_token`.
    2. Register Webhook endpoint or WebSocket client to receive `im.message.receive_v1` events.
    3. Validate signature with `verification_token` or decrypt encrypted payload with `encrypt_key`.
    4. Map user's `open_id` and message content to `UnifiedTaskRequest`.
    5. Post interactive message cards via `https://open.feishu.cn/open-apis/im/v1/messages`.
    """

    channel_name: str = "feishu"

    def __init__(self, app_id: str = "", app_secret: str = "") -> None:
        super().__init__()
        self.app_id = app_id
        self.app_secret = app_secret

    def start(self) -> None:
        self._running = True
        logger.info("Feishu Bot Adapter skeleton initialized (standby)")

    def stop(self) -> None:
        self._running = False

    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        chat_id = reply_target.get("chat_id")
        logger.info("[FEISHU MOCK] Send to chat %s: %s", chat_id, text)
        return True

    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        reply_target = task.get("reply_target") or {}
        return self.send_message(reply_target, f"[Task {task.get('id')}] {message}")

    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        return self.send_message(reply_target, f"[APPROVAL REQUIRED] {approval.get('id')}")
