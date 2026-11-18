"""Base adapter interface for Coding Agents."""

from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from openagent_bridge.models import CommandSpec
from openagent_bridge.runner import run_captured


class AgentAdapter(ABC):
    """Abstract base class for coding agent adapters."""

    execution_deadline: float | None = None
    probe_timeout_limit: float | None = None

    def _probe(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if self.execution_deadline is None:
            return subprocess.run(argv, capture_output=True, text=True, shell=False, timeout=timeout)
        if self.probe_timeout_limit is not None:
            timeout = min(timeout, self.probe_timeout_limit)
        result = run_captured(argv, deadline=min(self.execution_deadline, time.monotonic() + timeout))
        return subprocess.CompletedProcess(
            argv, result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )

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
