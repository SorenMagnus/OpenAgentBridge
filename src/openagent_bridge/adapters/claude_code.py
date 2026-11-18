"""Anthropic Claude Code CLI adapter for OpenAgent Bridge (fallback)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.models import CommandSpec


class ClaudeCodeAdapter(AgentAdapter):
    """Adapter for executing tasks via Claude Code CLI (fallback)."""

    def __init__(self, binary_path: str = "claude") -> None:
        self.binary_path = binary_path

    def detect(self) -> bool:
        """Check if claude CLI is installed and on PATH."""
        return shutil.which(self.binary_path) is not None

    def capabilities(self) -> dict[str, Any]:
        """Detect supported Claude CLI options from --help."""
        if not self.detect():
            return {"available": False}

        caps: dict[str, Any] = {
            "available": True,
            "version": "",
            "non_interactive": False,
            "stream_json": False,
            "max_turns": False,
        }

        try:
            ver_res = self._probe([self.binary_path, "--version"], timeout=5)
            if ver_res.returncode == 0:
                caps["version"] = ver_res.stdout.strip()
        except Exception:
            pass

        try:
            help_res = self._probe([self.binary_path, "--help"], timeout=10)
            if help_res.returncode == 0:
                txt = help_res.stdout
                caps["non_interactive"] = "-p" in txt or "--print" in txt
                caps["stream_json"] = "stream-json" in txt or "--output-format" in txt
                caps["max_turns"] = "--max-turns" in txt
        except Exception:
            pass

        return caps

    def build_command(
        self,
        prompt: str,
        worktree: Path,
        sandbox_mode: str = "workspace-write",
    ) -> CommandSpec:
        """Build command to run claude -p in the given worktree."""
        worktree = Path(worktree).resolve()
        caps = self.capabilities()

        argv = [self.binary_path]

        if caps.get("non_interactive", True):
            argv.extend(["-p", prompt])
        else:
            argv.append(prompt)

        if caps.get("stream_json", False):
            argv.extend(["--output-format", "stream-json"])

        if caps.get("max_turns", False):
            argv.extend(["--max-turns", "10"])

        return CommandSpec(
            argv=argv,
            cwd=worktree,
            timeout_seconds=900,
            capture_output=True,
        )

    def parse_output(self, stdout_path: Path) -> list[dict[str, Any]]:
        """Parse structured output from Claude Code."""
        events: list[dict[str, Any]] = []
        path = Path(stdout_path)
        if not path.exists():
            return events

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"type": "raw_event", "raw": line})

        return events
