"""Base channel adapter definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openagent_bridge.config import BridgeConfig


class BaseChannelAdapter(ABC):
    """Abstract base class for all platform channel adapters."""

    channel_name: str = "base"

    def __init__(self) -> None:
        self.config: BridgeConfig | None = None
        self.core: Any = None
        self._running: bool = False

    def initialize(self, config: BridgeConfig, core: Any) -> None:
        """Initialize the channel adapter with bridge config and core orchestrator."""
        self.config = config
        self.core = core

    @abstractmethod
    def start(self) -> None:
        """Start listening for incoming messages or polling platform events."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and cleanly shutdown connections."""
        pass

    @abstractmethod
    def send_message(self, reply_target: dict[str, Any], text: str) -> bool:
        """Send a plain text or Markdown message to a platform-specific target."""
        pass

    @abstractmethod
    def send_task_update(self, task: dict[str, Any], message: str) -> bool:
        """Send a task progress or lifecycle completion update."""
        pass

    @abstractmethod
    def send_approval_request(
        self, approval: dict[str, Any], reply_target: dict[str, Any]
    ) -> bool:
        """Send an approval notification with interactive/command options."""
        pass

    @property
    def is_running(self) -> bool:
        return self._running
