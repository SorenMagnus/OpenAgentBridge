"""OpenAI Codex CLI adapter for OpenAgent Bridge."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.models import CommandSpec


def is_quota_blocked(output: str) -> bool:
    """Detect if Codex output indicates an account usage limit or quota exhaustion."""
    if not output:
        return False
    lower = output.lower()
    quota_signatures = [
        "hit your usage limit",
        "usage limit",
        "purchase more credits",
        "insufficient_quota",
        "rate_limit_exceeded",
        "upgrade to pro",
    ]
    return any(sig in lower for sig in quota_signatures)


class CodexAdapter(AgentAdapter):
    """Adapter for executing tasks via OpenAI Codex CLI."""

    def __init__(self, binary_path: str = "codex") -> None:
        self.binary_path = binary_path

    def detect(self) -> bool:
        """Check if codex CLI is installed and on PATH."""
        return shutil.which(self.binary_path) is not None

    def capabilities(self) -> dict[str, Any]:
        """Detect supported Codex CLI options from --help."""
        if not self.detect():
            return {"available": False}

        caps: dict[str, Any] = {
            "available": True,
            "version": "",
            "jsonl_output": False,
            "sandbox_modes": [],
            "cd_support": False,
            "ephemeral": False,
        }

        try:
            ver_res = subprocess.run(
                [self.binary_path, "--version"],
                capture_output=True,
                text=True,
                shell=False,
                timeout=5,
            )
            if ver_res.returncode == 0:
                caps["version"] = ver_res.stdout.strip()
        except Exception:
            pass

        try:
            help_res = subprocess.run(
                [self.binary_path, "exec", "--help"],
                capture_output=True,
                text=True,
                shell=False,
                timeout=10,
            )
            if help_res.returncode == 0:
                txt = help_res.stdout
                caps["jsonl_output"] = "--json" in txt
                caps["cd_support"] = "--cd" in txt or "-C" in txt
                caps["ephemeral"] = "--ephemeral" in txt
                caps["skip_git_repo_check"] = "--skip-git-repo-check" in txt
                caps["approve_for_me"] = "--approve-for-me" in txt
                if "workspace-write" in txt:
                    caps["sandbox_modes"].append("workspace-write")
                if "read-only" in txt:
                    caps["sandbox_modes"].append("read-only")
                if "danger-full-access" in txt:
                    caps["sandbox_modes"].append("danger-full-access")
        except Exception:
            pass

        return caps

    def build_command(
        self,
        prompt: str,
        worktree: Path,
        sandbox_mode: str = "workspace-write",
    ) -> CommandSpec:
        """Build command to run codex exec in the given worktree."""
        worktree = Path(worktree).resolve()
        caps = self.capabilities()

        argv = [self.binary_path, "exec"]

        # In codex-cli, --sandbox and --approve-for-me are mutually exclusive.
        # --approve-for-me automatically routes approvals via the workspace-write sandbox.
        if caps.get("approve_for_me", False):
            argv.append("--approve-for-me")
        elif sandbox_mode in caps.get("sandbox_modes", ["workspace-write"]):
            argv.extend(["--sandbox", sandbox_mode])
        else:
            argv.extend(["--sandbox", "workspace-write"])

        # Structured JSONL output
        if caps.get("jsonl_output", True):
            argv.append("--json")

        # Working directory
        if caps.get("cd_support", True):
            argv.extend(["--cd", str(worktree)])

        # Ephemeral session to avoid polluting global state
        if caps.get("ephemeral", True):
            argv.append("--ephemeral")

        # Allow running inside worktree / detached repository
        if caps.get("skip_git_repo_check", True):
            argv.append("--skip-git-repo-check")

        # Instructions / prompt
        argv.append(prompt)

        return CommandSpec(
            argv=argv,
            cwd=worktree,
            timeout_seconds=900,
            capture_output=True,
        )

    def parse_output(self, stdout_path: Path) -> list[dict[str, Any]]:
        """Parse JSONL output lines, preserving unparseable lines as raw events."""
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
                    parsed = json.loads(line)
                    events.append(parsed)
                except json.JSONDecodeError:
                    events.append({
                        "type": "raw_event",
                        "raw": line,
                    })

        return events
