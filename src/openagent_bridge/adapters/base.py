"""Base adapter interface for Coding Agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from openagent_bridge.models import CommandSpec


class AgentAdapter(ABC):
    """Abstract base class for coding agent adapters."""

    @abstractmethod
    def detect(self) -> bool:
        """Check if this agent's executable is installed and available in PATH."""
        ...

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Query agent flags and supported runtime features."""
        ...

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        worktree: Path,
        sandbox_mode: str = "workspace-write",
    ) -> CommandSpec:
        """Construct the CommandSpec to invoke the agent non-interactively."""
        ...

    @abstractmethod
    def parse_output(self, stdout_path: Path) -> list[dict[str, Any]]:
        """Parse structured logs or JSONL output from the agent."""
        ...
