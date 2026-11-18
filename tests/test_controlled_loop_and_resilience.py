"""Controlled end-to-end engineering loop and resilience tests.

Validates the full autonomous control loop using MockAgentAdapter (Option 3):
- 2-round repair loop on real Git worktree with actual bug injection & verification
- Real Git diff verification
- Prompt evolution & failure detail logging
- Quota exhaustion / account usage limit detection (BLOCKED, no endless retries)
- Duplicate task idempotency
- Distributed lock concurrency conflicts
- Process crash & timeout handling
- RBAC permission denial
- Budget exhaustion and safe rollback
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openagent_bridge.access_control import AccessControlEngine, User
from openagent_bridge.adapters.mock_agent import MockAgentAdapter
from openagent_bridge.budget import BudgetLimits
from openagent_bridge.config import AuthConfig
from openagent_bridge.core import BridgeCore
from openagent_bridge.db import Database
from openagent_bridge.locks import LockManager
from openagent_bridge.models import CommandSpec, TaskStatus, UnifiedTaskRequest
from openagent_bridge.repair import AutonomousRepairLoop
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner
from openagent_bridge.verifier import Verifier


def _setup_buggy_calc_repo(repo_path: Path) -> None:
    """Create a real Git repository with a deliberate calculation bug."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=str(repo_path), check=True)

    # Buggy calc.py: returns a - b instead of a + b
    calc_code = (
        "def add(a: int, b: int) -> int:\n"
        "    return a - b  # deliberate bug: subtraction instead of addition\n"
    )
    (repo_path / "calc.py").write_text(calc_code, encoding="utf-8")

    # Unit test test_calc.py: expects add(2, 3) == 5
    test_code = (
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )
    (repo_path / "test_calc.py").write_text(test_code, encoding="utf-8")
    (repo_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")

    subprocess.run(["git", "add", "calc.py", "test_calc.py", ".gitignore"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "commit", "-m", "init: repo with buggy calc.py"], cwd=str(repo_path), check=True)


def test_controlled_two_round_repair_loop_with_real_worktree(tmp_path: Path):
    """Test full 2-round autonomous repair loop with actual pytest verification.

    Round 1: Pytest fails on buggy calc.py (2 - 3 == -1 != 5).
             MockAgent receives repair prompt with failure details and fixes calc.py.
    Round 2: Pytest passes on fixed calc.py (2 + 3 == 5).
             Loop terminates with REVIEW_REQUIRED and reports pass.
    """
    repo_dir = tmp_path / "calc_repo"
    _setup_buggy_calc_repo(repo_dir)

    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")
    runner = CommandRunner(artifact_dir=data_dir / "artifacts")
    repo_mgr = RepositoryManager()

    # Create isolated linked worktree
    worktree_path = repo_mgr.create_worktree(
        repo_root=repo_dir,
        task_id="task-calc-100",
        data_dir=data_dir,
    )

    # Setup task with explicit acceptance check
    task_spec = {
        "id": "task-calc-100",
        "project_id": "calc_repo",
        "goal": "Fix addition function in calc.py so test_calc.py passes",
        "original_goal": "Fix addition function in calc.py",
        "status": TaskStatus.NEW.value,
        "worktree_path": str(worktree_path),
        "acceptance": [
            {
                "kind": "pytest",
                "command": f"{sys.executable} -m pytest test_calc.py",
            }
        ],
    }
    db.insert_task(task_spec)

    # MockAgent that applies deterministic patch to calc.py
    mock_agent = MockAgentAdapter(
        patches={
            "calc.py": (
                "return a - b  # deliberate bug: subtraction instead of addition",
                "return a + b  # fixed addition",
            )
        }
    )

    limits = BudgetLimits(max_iterations=3, max_agent_invocations=3)
    loop = AutonomousRepairLoop(
        db=db,
        runner=runner,
        budget_limits=limits,
    )

    result = loop.run_loop(
        task=task_spec,
        worktree_path=worktree_path,
        adapter=mock_agent,
    )

    # Verification assertions
    assert result.iterations_run == 2, f"Expected 2 iterations, got {result.iterations_run}"
    assert result.overall_pass is True, "Expected overall pass on iteration 2"
    assert result.final_status == TaskStatus.REVIEW_REQUIRED.value

    # Round 1 failure evidence
    r1 = result.reports[0]
    assert r1.overall_pass is False, "Round 1 must fail on deliberate bug"
    cmd_check_r1 = [c for c in r1.checks if c.kind == "command"][0]
    assert cmd_check_r1.passed is False
    assert cmd_check_r1.exit_code != 0

    # Read round 1 failure output artifact
    r1_stdout = cmd_check_r1.stdout_artifact.read_text(encoding="utf-8")
    assert "FAILED test_calc.py::test_add" in r1_stdout or "assert -1 == 5" in r1_stdout

    # Round 2 pass evidence
    r2 = result.reports[1]
    assert r2.overall_pass is True, "Round 2 must pass after agent fix"
    cmd_check_r2 = [c for c in r2.checks if c.kind == "command"][0]
    assert cmd_check_r2.passed is True
    assert cmd_check_r2.exit_code == 0

    # Real Git Diff verification
    diff = repo_mgr.get_diff(worktree_path)
    assert "-    return a - b" in diff.full_diff
    assert "+    return a + b" in diff.full_diff

    # Verify task state transitions in DB
    events = db.get_events("task-calc-100")
    event_types = [e["type"] for e in events]
    assert "repair.verifying" in event_types
    assert "repair.planned" in event_types
    assert "repair.agent_running" in event_types
    assert "repair.succeeded" in event_types

    # Clean up worktree
    repo_mgr.remove_worktree(worktree_path, repo_root=repo_dir)


def test_quota_exhaustion_detection_stops_immediately(tmp_path: Path):
    """Test that agent quota/usage limit output immediately transitions task to BLOCKED.

    Ensures no misjudgment as code defects and stops further retries.
    """
    repo_dir = tmp_path / "calc_repo_quota"
    _setup_buggy_calc_repo(repo_dir)

    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")
    runner = CommandRunner(artifact_dir=data_dir / "artifacts")
    repo_mgr = RepositoryManager()

    worktree_path = repo_mgr.create_worktree(
        repo_root=repo_dir,
        task_id="task-quota-1",
        data_dir=data_dir,
    )

    task_spec = {
        "id": "task-quota-1",
        "project_id": "calc_repo",
        "goal": "Fix bug",
        "status": TaskStatus.NEW.value,
        "worktree_path": str(worktree_path),
        "acceptance": [
            {
                "kind": "pytest",
                "command": f"{sys.executable} -m pytest test_calc.py",
            }
        ],
    }
    db.insert_task(task_spec)

    # MockAgent simulating OpenAI / Codex usage limit
    quota_agent = MockAgentAdapter(simulate_quota_block=True)

    limits = BudgetLimits(max_iterations=5)
    loop = AutonomousRepairLoop(db=db, runner=runner, budget_limits=limits)

    result = loop.run_loop(task=task_spec, worktree_path=worktree_path, adapter=quota_agent)

    # Assert immediately stopped at iteration 1 as BLOCKED
    assert result.final_status == TaskStatus.BLOCKED.value
    assert "quota blocked" in result.stop_reason.lower()
    assert result.iterations_run == 1, "Must not retry when quota is blocked"

    # Verify task status in database
    db_task = db.get_task("task-quota-1")
    assert db_task["status"] == TaskStatus.BLOCKED.value

    repo_mgr.remove_worktree(worktree_path, repo_root=repo_dir)


def test_task_idempotency_duplicate_submission(tmp_path: Path):
    """Test duplicate task submissions with idempotency_key return existing task ID."""
    db = Database(tmp_path / "bridge.db")

    task_spec = {
        "idempotency_key": "telegram_msg_998811",
        "project_id": "main_project",
        "goal": "Run migration",
        "status": TaskStatus.NEW.value,
    }

    first_id = db.insert_task(task_spec)
    assert first_id is not None

    # Submit second time with identical idempotency key
    second_id = db.insert_task(task_spec)
    assert second_id == first_id

    # Verify only 1 task in database
    cur = db.connection.execute("SELECT count(*) as count FROM tasks")
    assert cur.fetchone()["count"] == 1


def test_distributed_lock_concurrency_conflict(tmp_path: Path):
    """Test distributed locks prevent concurrent access to the same task/resource."""
    db = Database(tmp_path / "bridge.db")
    lm = LockManager(db)

    # Worker 1 acquires lock
    assert lm.acquire("resource-task-lock", owner="worker-1", ttl_seconds=10) is True

    # Worker 2 attempts to acquire same lock -> fails
    assert lm.acquire("resource-task-lock", owner="worker-2", ttl_seconds=10) is False

    # Worker 1 releases lock
    assert lm.release("resource-task-lock", owner="worker-1") is True

    # Worker 2 can now acquire
    assert lm.acquire("resource-task-lock", owner="worker-2", ttl_seconds=10) is True
    assert lm.release("resource-task-lock", owner="worker-2") is True


def test_process_crash_handling(tmp_path: Path):
    """Test that runner handles child process crashes (e.g. exit 139) without raising uncaught exceptions."""
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    crash_agent = MockAgentAdapter(simulate_crash=True)

    cmd = crash_agent.build_command("test crash", tmp_path)
    result = runner.run(cmd)

    assert result.exit_code == 139
    assert result.timed_out is False
    assert result.stderr_path.exists()
    stderr_text = result.stderr_path.read_text(encoding="utf-8")
    assert "segmentation fault" in stderr_text


def test_command_runner_timeout(tmp_path: Path):
    """Test that command runner enforces timeout bounds and terminates runaway processes."""
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")

    # Command that sleeps for 5 seconds
    py_code = "import time; time.sleep(5)"
    spec = CommandSpec(
        argv=[sys.executable, "-c", py_code],
        cwd=tmp_path,
        timeout_seconds=1,  # 1 second timeout
        capture_output=True,
    )

    t0 = time.time()
    result = runner.run(spec)
    duration = time.time() - t0

    assert result.timed_out is True
    assert duration < 4.0, f"Process took {duration}s, should have timed out within ~1-2s"


def test_access_control_permission_denial():
    """Test RBAC blocks unauthorized users and channels from submitting or executing tasks."""
    auth_config = AuthConfig(
        enabled=True,
        users=[
            {"id": "user_guest", "channel": "telegram", "role": "viewer", "allowed_channels": ["telegram"], "allowed_projects": ["proj-public"]},
            {"id": "user_dev", "channel": "telegram", "role": "developer", "allowed_channels": ["telegram"], "allowed_projects": ["proj-dev"]},
        ],
    )
    acl = AccessControlEngine(auth_config)

    # 1. Unregistered user denied
    unauth = acl.authenticate("telegram", "unknown_hacker")
    assert unauth is None

    # 2. Viewer denied submission on public project (viewer role cannot submit tasks)
    viewer = acl.authenticate("telegram", "user_guest")
    assert viewer is not None
    ok, err = acl.authorize_task_submission(viewer, "proj-public", permissions={"read": True})
    assert ok is False
    assert "viewer" in err.lower()

    # 3. Developer denied submission on unassigned project
    dev = acl.authenticate("telegram", "user_dev")
    assert dev is not None
    ok, err = acl.authorize_task_submission(dev, "proj-secret", permissions={})
    assert ok is False
    assert "not authorized to access project" in err.lower()

    # 4. Developer denied dangerous permissions (e.g. deploy=True)
    ok, err = acl.authorize_task_submission(dev, "proj-dev", permissions={"deploy": True})
    assert ok is False
    assert "forbidden without admin privilege" in err.lower()


def test_budget_exhaustion_and_safe_rollback(tmp_path: Path):
    """Test budget limits trigger BUDGET_EXCEEDED and worktree can be cleanly reset."""
    repo_dir = tmp_path / "calc_repo_budget"
    _setup_buggy_calc_repo(repo_dir)

    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")
    runner = CommandRunner(artifact_dir=data_dir / "artifacts")
    repo_mgr = RepositoryManager()

    worktree_path = repo_mgr.create_worktree(
        repo_root=repo_dir,
        task_id="task-budget-1",
        data_dir=data_dir,
    )

    task_spec = {
        "id": "task-budget-1",
        "project_id": "calc_repo",
        "goal": "Try to fix with budget of 1 iteration",
        "status": TaskStatus.NEW.value,
        "worktree_path": str(worktree_path),
        "acceptance": [
            {
                "kind": "pytest",
                "command": f"{sys.executable} -m pytest test_calc.py",
            }
        ],
    }
    db.insert_task(task_spec)

    # Agent that doesn't fix anything
    failing_agent = MockAgentAdapter(
        custom_script="import sys\nprint('Doing nothing')\nsys.exit(0)\n"
    )

    limits = BudgetLimits(max_iterations=1)  # Only allow 1 iteration
    loop = AutonomousRepairLoop(db=db, runner=runner, budget_limits=limits)

    result = loop.run_loop(task=task_spec, worktree_path=worktree_path, adapter=failing_agent)

    # Must hit budget exceeded
    assert result.final_status == TaskStatus.BUDGET_EXCEEDED.value
    assert "budget exceeded" in result.stop_reason.lower()

    # Safe rollback verification
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(worktree_path), check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=str(worktree_path), check=True)
    status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(worktree_path), capture_output=True, text=True)
    assert status_proc.stdout.strip() == "", "Worktree must be completely clean after rollback"

    repo_mgr.remove_worktree(worktree_path, repo_root=repo_dir)
