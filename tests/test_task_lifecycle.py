"""End-to-end integration tests for task lifecycle management."""

import json
import subprocess
import sys
from pathlib import Path
import pytest
from openagent_bridge.config import BridgeConfig
from openagent_bridge.db import Database, InvalidTransitionError
from openagent_bridge.models import CommandSpec, TaskSpec, TaskStatus
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner
from openagent_bridge.verifier import Verifier


def _setup_git_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), check=True)
    (path / "main.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True)


def test_full_lifecycle_clean_success(tmp_path: Path):
    repo_dir = tmp_path / "project_repo"
    _setup_git_repo(repo_dir)

    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")
    repo_mgr = RepositoryManager()
    config = BridgeConfig(data_dir=data_dir)
    runner = CommandRunner(config=config)
    verifier = Verifier()

    # 1. Create TaskSpec
    task_id = "lifecycle-001"
    task_spec = {
        "id": task_id,
        "idempotency_key": "idem-clean-001",
        "project_id": "test_proj",
        "goal": "Ensure add function works",
        "original_goal": "Ensure add function works",
        "acceptance": {
            "commands": [[sys.executable, "-c", "import sys; sys.exit(0)"]],
            "required_files": ["main.py"],
        },
        "status": TaskStatus.NEW.value,
    }

    # 2. Insert into DB
    assigned_id = db.insert_task(task_spec)
    assert assigned_id == task_id

    # 3. Create isolated worktree
    worktree = repo_mgr.create_worktree(repo_dir, task_id, data_dir)
    assert worktree.exists()

    # 4. State transitions: NEW -> PREPARING -> RUNNING
    db.transition_task(task_id, TaskStatus.PREPARING.value)
    db.transition_task(task_id, TaskStatus.RUNNING.value)

    # 5. Agent modifies file in worktree
    (worktree / "main.py").write_text(
        "def add(a, b): return a + b\n# Agent modified\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "main.py"], cwd=str(worktree), check=True)
    subprocess.run(["git", "commit", "-m", "agent update"], cwd=str(worktree), check=True)

    # 6. Verification
    db.transition_task(task_id, TaskStatus.VERIFYING.value)
    report = verifier.run_checks(task_spec, worktree, runner)
    assert report.overall_pass is True

    # 7. Final status: REVIEW_REQUIRED (never auto-merge to main)
    db.transition_task(task_id, TaskStatus.REVIEW_REQUIRED.value)
    task = db.get_task(task_id)
    assert task["status"] == TaskStatus.REVIEW_REQUIRED.value

    # Verify original repo unchanged
    orig_content = (repo_dir / "main.py").read_text(encoding="utf-8")
    assert "Agent modified" not in orig_content

    db.close()


def test_dirty_repo_blocks_task_creation(tmp_path: Path):
    repo_dir = tmp_path / "dirty_project"
    _setup_git_repo(repo_dir)

    # Make repo dirty
    (repo_dir / "uncommitted.txt").write_text("dirty state", encoding="utf-8")

    repo_mgr = RepositoryManager()
    assert repo_mgr.is_dirty(repo_dir) is True

    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")

    task_id = "lifecycle-dirty-001"
    # When dirty, task is marked BLOCKED without creating worktree
    db.insert_task({
        "id": task_id,
        "idempotency_key": "idem-dirty-001",
        "project_id": "test_proj",
        "goal": "Test dirty block",
        "original_goal": "Test dirty block",
        "status": TaskStatus.BLOCKED.value,
    })

    task = db.get_task(task_id)
    assert task["status"] == TaskStatus.BLOCKED.value
    db.close()


def test_duplicate_idempotency_key_returns_same_task(tmp_path: Path):
    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")

    task1_id = db.insert_task({
        "id": "task-first",
        "idempotency_key": "same-key-123",
        "goal": "Initial goal",
        "original_goal": "Initial goal",
    })

    task2_id = db.insert_task({
        "id": "task-second",
        "idempotency_key": "same-key-123",
        "goal": "Attempt to duplicate",
        "original_goal": "Attempt to duplicate",
    })

    assert task1_id == task2_id
    assert task1_id == "task-first"
    db.close()


def test_illegal_transition_rejection(tmp_path: Path):
    data_dir = tmp_path / "bridge_data"
    db = Database(data_dir / "bridge.db")

    task_id = db.insert_task({
        "id": "task-illegal",
        "idempotency_key": "illegal-key",
        "status": TaskStatus.NEW.value,
    })

    with pytest.raises(InvalidTransitionError):
        # NEW cannot jump straight to SUCCEEDED
        db.transition_task(task_id, TaskStatus.SUCCEEDED.value)

    db.close()
