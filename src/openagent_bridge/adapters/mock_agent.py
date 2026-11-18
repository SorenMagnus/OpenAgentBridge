"""Deterministic Mock Agent Adapter for testing engineering control loops without external LLMs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.models import CommandSpec


class MockAgentAdapter(AgentAdapter):
    """An independent deterministic agent adapter for end-to-end verification.

    Applies real, deterministic file modifications directly in the worktree
    via subprocess execution, satisfying Requirement 3 of Option 3.
    """

    def __init__(
        self,
        patches: dict[str, tuple[str, str]] | None = None,
        simulate_quota_block: bool = False,
        simulate_crash: bool = False,
        custom_script: str | None = None,
    ) -> None:
        self.patches = patches or {
            "calc.py": ("return a - b", "return a + b"),
        }
        self.simulate_quota_block = simulate_quota_block
        self.simulate_crash = simulate_crash
        self.custom_script = custom_script

    def detect(self) -> bool:
        return True

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "mock-agent",
            "deterministic": True,
            "version": "1.0.0",
            "sandbox_modes": ["workspace-write", "read-only"],
        }

    def build_command(
        self,
        prompt: str,
        worktree: Path,
        sandbox_mode: str = "workspace-write",
    ) -> CommandSpec:
        worktree = Path(worktree).resolve()

        if self.simulate_crash:
            # Simulate a process crash with non-zero exit code
            py_code = (
                "import sys\n"
                "sys.stderr.write('CRITICAL: segmentation fault (core dumped)\\n')\n"
                "sys.exit(139)\n"
            )
            return CommandSpec(
                argv=[sys.executable, "-c", py_code],
                cwd=worktree,
                timeout_seconds=30,
                capture_output=True,
            )

        if self.simulate_quota_block:
            # Simulate OpenAI / Codex quota exhaustion message
            py_code = (
                "import sys\n"
                "sys.stderr.write('You have hit your usage limit. Upgrade to Pro or try again at 9:10 PM.\\n')\n"
                "sys.exit(1)\n"
            )
            return CommandSpec(
                argv=[sys.executable, "-c", py_code],
                cwd=worktree,
                timeout_seconds=30,
                capture_output=True,
            )

        if self.custom_script:
            py_code = self.custom_script
        else:
            # Deterministic file patcher
            patches_json = json.dumps(self.patches)
            prompt_len = len(prompt)
            py_code = f"""
import sys
import json
from pathlib import Path

patches = json.loads({repr(patches_json)})
print(f"[mock-agent] Starting deterministic repair in {{Path.cwd()}}")
print(f"[mock-agent] Prompt received (len={prompt_len})")

applied = 0
for relpath, (old_text, new_text) in patches.items():
    target_file = Path(relpath)
    if target_file.exists():
        content = target_file.read_text(encoding="utf-8")
        if old_text in content:
            updated = content.replace(old_text, new_text, 1)
            target_file.write_text(updated, encoding="utf-8")
            print(f"[mock-agent] Replaced '{{old_text}}' with '{{new_text}}' in {{relpath}}")
            applied += 1
        else:
            print(f"[mock-agent] Target text not found in {{relpath}}, skipped")
    else:
        print(f"[mock-agent] Target file not found: {{relpath}}")

print(f"[mock-agent] Deterministic repair complete. Applied {{applied}} patch(es).")
sys.exit(0)
"""

        return CommandSpec(
            argv=[sys.executable, "-c", py_code],
            cwd=worktree,
            timeout_seconds=60,
            capture_output=True,
        )

    def parse_output(self, stdout_path: Path) -> list[dict[str, Any]]:
        if not stdout_path.exists():
            return []
        lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [{"line": line} for line in lines if line.strip()]
