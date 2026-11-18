"""Tests for Worker integration with AutonomousRepairLoop behind autonomy.enabled."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.config import AutonomyConfig, BridgeConfig
from openagent_bridge.db import Database
from openagent_bridge.models import CommandSpec, TaskStatus
from openagent_bridge.worker import Worker


def _setup_test_repo(repo_path: Path) -> None:
    """Create a temporary git repository with a failing solution.py."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=str(repo_path), check=True)
    (repo_path / "solution.py").write_text("def solve():\n    return False\n", encoding="utf-8")
    (repo_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "add", "solution.py", ".gitignore"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)


class ScriptableMockAgent(AgentAdapter):
    """Mock agent whose fix behavior is controllable by call count."""

    def __init__(self, fix_on_call: int = 2) -> None:
        self.call_count = 0
        self.fix_on_call = fix_on_call
        self.prompts: list[str] = []

    def detect(self) -> bool:
        return True

    def capabilities(self) -> dict[str, Any]:
        return {"available": True, "name": "scriptable-mock"}

    def build_command(
        self,
        prompt: str,
        worktree: Path,
        sandbox_mode: str = "workspace-write",
    ) -> CommandSpec:
        self.call_count += 1
        self.prompts.append(prompt)

        if self.call_count >= self.fix_on_call:
            py_code = (
                "from pathlib import Path\n"
                "Path('solution.py').write_text('def solve():\\n    return True\\n', encoding='utf-8')\n"
            )
        else:
            py_code = "pass"

        return CommandSpec(
            argv=[sys.executable, "-c", py_code],
            cwd=worktree,
            timeout_seconds=10,
            capture_output=True,
        )

    def parse_output(self, stdout_path: Path) -> list[dict[str, Any]]:
        return []


def _create_env(tmp_path: Path, autonomy_enabled: bool = True):
    repo_dir = tmp_path / "repo"
    _setup_test_repo(repo_dir)

    data_dir = tmp_path / "data"
    db = Database(data_dir / "bridge.db")
    db.register_project("test_proj", str(repo_dir))

    config = BridgeConfig(
        data_dir=data_dir,
        autonomy=AutonomyConfig(
            enabled=autonomy_enabled,
            max_iterations=5,
            max_agent_invocations=5,
        ),
    )
    worker = Worker(config=config, db=db)
    return repo_dir, data_dir, db, worker


def test_worker_repair_loop_fixes_on_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(a) autonomy enabled, agent fixes on call 2 -> REVIEW_REQUIRED, 2nd prompt has error context."""
    repo_dir, data_dir, db, worker = _create_env(tmp_path, autonomy_enabled=True)

    mock_agent = ScriptableMockAgent(fix_on_call=2)
    monkeypatch.setattr("openagent_bridge.worker.CodexAdapter", lambda: mock_agent)
    monkeypatch.setattr("openagent_bridge.worker.ClaudeCodeAdapter.detect", lambda self: False)

    task_spec = {
        "id": "task-loop-a",
        "project_id": "test_proj",
        "goal": "Fix solution.py so solve() returns True",
        "acceptance": {
            "commands": [
                [sys.executable, "-c", "import solution; assert solution.solve() is True"]
            ]
        },
    }
    worker.queue.enqueue(task_spec)

    result = worker.run_once()

    assert result is not None
    assert result["status"] == TaskStatus.REVIEW_REQUIRED.value
    assert result["overall_pass"] is True
    assert mock_agent.call_count == 2
    assert len(mock_agent.prompts) == 2

    # Second prompt must contain structured repair directives
    second_prompt = mock_agent.prompts[1]
    assert "OBJECTIVE (IMMUTABLE)" in second_prompt
    assert "FAILING CHECKS" in second_prompt

    # Events in DB must record repair loop progression
    events = db.get_events("task-loop-a")
    event_types = [e["type"] for e in events]
    assert "repair.planned" in event_types
    assert "repair.succeeded" in event_types


def test_worker_repair_loop_autonomy_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(b) autonomy disabled -> FAILED and agent is only invoked 1 time."""
    repo_dir, data_dir, db, worker = _create_env(tmp_path, autonomy_enabled=False)

    mock_agent = ScriptableMockAgent(fix_on_call=2)
    monkeypatch.setattr("openagent_bridge.worker.CodexAdapter", lambda: mock_agent)
    monkeypatch.setattr("openagent_bridge.worker.ClaudeCodeAdapter.detect", lambda self: False)

    task_spec = {
        "id": "task-loop-b",
        "project_id": "test_proj",
        "goal": "Fix solution.py so solve() returns True",
        "acceptance": {
            "commands": [
                [sys.executable, "-c", "import solution; assert solution.solve() is True"]
            ]
        },
    }
    worker.queue.enqueue(task_spec)

    result = worker.run_once()

    assert result is not None
    assert result["status"] == TaskStatus.FAILED.value
    assert result["overall_pass"] is False
    assert mock_agent.call_count == 1


def test_worker_repair_loop_fixes_on_first_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(c) autonomy enabled, agent fixes on call 1 -> REVIEW_REQUIRED, iterations_run is 1, only 1 invocation."""
    repo_dir, data_dir, db, worker = _create_env(tmp_path, autonomy_enabled=True)

    mock_agent = ScriptableMockAgent(fix_on_call=1)
    monkeypatch.setattr("openagent_bridge.worker.CodexAdapter", lambda: mock_agent)
    monkeypatch.setattr("openagent_bridge.worker.ClaudeCodeAdapter.detect", lambda self: False)

    task_spec = {
        "id": "task-loop-c",
        "project_id": "test_proj",
        "goal": "Fix solution.py so solve() returns True",
        "acceptance": {
            "commands": [
                [sys.executable, "-c", "import solution; assert solution.solve() is True"]
            ]
        },
    }
    worker.queue.enqueue(task_spec)

    result = worker.run_once()

    assert result is not None
    assert result["status"] == TaskStatus.REVIEW_REQUIRED.value
    assert result["overall_pass"] is True
    assert result.get("iterations_run") == 1
    assert mock_agent.call_count == 1
