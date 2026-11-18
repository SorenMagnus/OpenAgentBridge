"""Central router to dispatch bidirectional messages and events across channels."""

from __future__ import annotations

import json
import logging
from typing import Any

from openagent_bridge.channels.base import BaseChannelAdapter

logger = logging.getLogger(__name__)


class ChannelRouter:
    """Manages channel adapters and dispatches outbound feedback."""

    def __init__(self) -> None:
        self._adapters: dict[str, BaseChannelAdapter] = {}

    def register(self, adapter: BaseChannelAdapter) -> None:
        """Register a channel adapter under its unique channel name."""
        self._adapters[adapter.channel_name] = adapter

    def get(self, channel_name: str) -> BaseChannelAdapter | None:
        """Retrieve a registered channel adapter."""
        return self._adapters.get(channel_name)

    def dispatch_message(
        self, channel_name: str, reply_target: dict[str, Any], text: str
    ) -> bool:
        """Dispatch a generic message to the specified channel adapter."""
        adapter = self._adapters.get(channel_name)
        if not adapter:
            logger.warning("No channel adapter registered for '%s'", channel_name)
            return False
        return adapter.send_message(reply_target, text)

    def dispatch_task_update(self, task: dict[str, Any], message: str) -> bool:
        """Dispatch a task progress/completion message to the task's originating channel."""
        channel_name = task.get("source_channel", "cli")
        adapter = self._adapters.get(channel_name)
        if not adapter:
            return False
        return adapter.send_task_update(task, message)

    def dispatch_approval_request(
        self, approval: dict[str, Any], task: dict[str, Any]
    ) -> bool:
        """Dispatch an approval request back to the user on their channel."""
        channel_name = task.get("source_channel", "cli")
        adapter = self._adapters.get(channel_name)
        if not adapter:
            return False
        reply_target = task.get("reply_target") or {}
        if isinstance(reply_target, str):
            try:
                reply_target = json.loads(reply_target)
            except Exception:
                reply_target = {}
        return adapter.send_approval_request(approval, reply_target)

    def start_all(self) -> None:
        """Start all registered adapters."""
        for adapter in self._adapters.values():
            adapter.start()

    def stop_all(self) -> None:
        """Stop all registered adapters."""
        for adapter in self._adapters.values():
            adapter.stop()
