"""Tests for AutonomousRepairLoop termination, budget bounds, and stall detection."""

import subprocess
import sys
from pathlib import Path
from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.budget import BudgetLimits
from openagent_bridge.db import Database
from openagent_bridge.models import CommandSpec, TaskStatus
from openagent_bridge.repair import AutonomousRepairLoop
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner
from openagent_bridge.verifier import Verifier


def _setup_test_repo(repo_path: Path):
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=str(repo_path), check=True)
    (repo_path / "solution.py").write_text("def solve(): return False\n", encoding="utf-8")
    subprocess.run(["git", "add", "solution.py"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)


class MockRepairingAgent(AgentAdapter):
    """Mock agent that fixes the bug on iteration 2."""
    def __init__(self, fix_on_iteration: int = 2):
        self.call_count = 0
        self.fix_on_iteration = fix_on_iteration

    def detect(self) -> bool:
        return True

    def capabilities(self) -> dict:
        return {"available": True}

    def build_command(self, prompt: str, worktree: Path, sandbox_mode: str = "workspace-write") -> CommandSpec:
        self.call_count += 1
        if self.call_count >= self.fix_on_iteration:
            code = "from pathlib import Path; Path('solution.py').write_text('def solve(): return True\\n')"
        else:
            code = "pass"
        return CommandSpec(
            argv=[sys.executable, "-c", code],
            cwd=worktree,
            timeout_seconds=10,
        )

    def parse_output(self, stdout_path: Path) -> list:
        return []


class MockFailingAgent(AgentAdapter):
    """Mock agent that never fixes the bug."""
    def detect(self) -> bool:
        return True

    def capabilities(self) -> dict:
        return {"available": True}

    def build_command(self, prompt: str, worktree: Path, sandbox_mode: str = "workspace-write") -> CommandSpec:
        return CommandSpec(
            argv=[sys.executable, "-c", "pass"],
            cwd=worktree,
            timeout_seconds=10,
        )

    def parse_output(self, stdout_path: Path) -> list:
        return []


def test_repair_loop_succeeds_on_fix(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    _setup_test_repo(repo_dir)

    data_dir = tmp_path / "data"
    db = Database(data_dir / "bridge.db")
    runner = CommandRunner(artifact_dir=data_dir / "artifacts")
    repo_mgr = RepositoryManager()

    task_id = "repair-test-01"
    task = {
        "id": task_id,
        "idempotency_key": "rep-key-01",
        "goal": "Fix solution to return True",
        "original_goal": "Fix solution to return True",
        "acceptance": {
            "commands": [[sys.executable, "-c", "import solution; assert solution.solve() is True"]],
        },
    }
    db.insert_task(task)

    worktree = repo_mgr.create_worktree(repo_dir, task_id, data_dir)

    limits = BudgetLimits(max_iterations=5, max_wall_minutes=10, max_agent_invocations=8)
    repair_loop = AutonomousRepairLoop(db, runner, budget_limits=limits)
    agent = MockRepairingAgent(fix_on_iteration=2)

    result = repair_loop.run_loop(task, worktree, adapter=agent)

    assert result.overall_pass is True
    assert result.final_status == TaskStatus.REVIEW_REQUIRED.value
    assert result.iterations_run in (2, 3)

    # Original goal must be preserved exactly
    stored_task = db.get_task(task_id)
    assert stored_task["original_goal"] == "Fix solution to return True"


def test_repair_loop_halts_on_stalled_signature(tmp_path: Path):
    repo_dir = tmp_path / "repo_stall"
    _setup_test_repo(repo_dir)

    data_dir = tmp_path / "data"
    db = Database(data_dir / "bridge.db")
    runner = CommandRunner(artifact_dir=data_dir / "artifacts")
    repo_mgr = RepositoryManager()

    task_id = "repair-stall-01"
    task = {
        "id": task_id,
        "idempotency_key": "stall-key-01",
        "goal": "Never succeeds",
        "original_goal": "Never succeeds",
        "acceptance": {
            "commands": [[sys.executable, "-c", "import sys; sys.exit(42)"]],
        },
    }
    db.insert_task(task)

    worktree = repo_mgr.create_worktree(repo_dir, task_id, data_dir)

    # Allow max 10 iterations, but stop after 2 consecutive identical failures
    limits = BudgetLimits(max_iterations=10, stop_after_no_improvement=2)
    repair_loop = AutonomousRepairLoop(db, runner, budget_limits=limits)
    agent = MockFailingAgent()

    result = repair_loop.run_loop(task, worktree, adapter=agent)

    # Must halt well before max_iterations!
    assert result.overall_pass is False
    assert "Stalled" in result.stop_reason
    assert result.iterations_run <= 4


def test_repair_loop_halts_on_budget_exceeded(tmp_path: Path):
    repo_dir = tmp_path / "repo_budget"
    _setup_test_repo(repo_dir)

    data_dir = tmp_path / "data"
    db = Database(data_dir / "bridge.db")
    runner = CommandRunner(artifact_dir=data_dir / "artifacts")
    repo_mgr = RepositoryManager()

    task_id = "repair-budget-01"
    task = {
        "id": task_id,
        "idempotency_key": "budget-key-01",
        "goal": "Never succeeds with changing errors",
        "original_goal": "Never succeeds with changing errors",
        "acceptance": {
            "commands": [[sys.executable, "-c", "import time; sys.exit(int(time.time()) % 10 + 1)"]],
        },
    }
    db.insert_task(task)

    worktree = repo_mgr.create_worktree(repo_dir, task_id, data_dir)

    # Strict limit: max 2 iterations
    limits = BudgetLimits(max_iterations=2, stop_after_no_improvement=10)
    repair_loop = AutonomousRepairLoop(db, runner, budget_limits=limits)
    agent = MockFailingAgent()

    result = repair_loop.run_loop(task, worktree, adapter=agent)

    assert result.final_status == TaskStatus.BUDGET_EXCEEDED.value
    assert result.iterations_run == 2
    assert "Budget exceeded" in result.stop_reason
