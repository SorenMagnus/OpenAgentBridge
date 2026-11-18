"""Tests for Verifier deterministic checks, policy enforcement, and pass/fail rules."""

import subprocess
import sys
from pathlib import Path
from openagent_bridge.models import CommandSpec
from openagent_bridge.runner import CommandRunner
from openagent_bridge.verifier import Verifier


def _init_repo_with_worktree(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=str(repo_dir), check=True)
    (repo_dir / "app.py").write_text("print('clean')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True)

    # create worktree
    worktree_dir = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "openagent/t1", str(worktree_dir), "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    return repo_dir, worktree_dir


def test_verifier_all_checks_pass(tmp_path: Path):
    _, worktree = _init_repo_with_worktree(tmp_path)
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    verifier = Verifier()

    task = {
        "id": "t1",
        "acceptance": {
            "commands": [[sys.executable, "-c", "import sys; print('check 1 pass'); sys.exit(0)"]],
            "required_files": ["app.py"],
            "forbidden_paths": [".env*"],
        },
    }

    report = verifier.run_checks(task, worktree, runner)
    if not report.overall_pass:
        for c in report.checks:
            if not c.passed and c.stderr_artifact:
                print("CHECK FAILED:", c.check_name, "STDERR:", c.stderr_artifact.read_text())
                print("CHECK STDOUT:", c.stdout_artifact.read_text() if c.stdout_artifact else "")
    assert report.overall_pass is True
    assert len(report.critical_failures) == 0
    assert report.suggested_next_action == "review"


def test_verifier_command_fails(tmp_path: Path):
    _, worktree = _init_repo_with_worktree(tmp_path)
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    verifier = Verifier()

    task = {
        "id": "t2",
        "acceptance": {
            "commands": [[sys.executable, "-c", "print('failed check'); sys.exit(1)"]],
            "required_files": ["app.py"],
        },
    }

    report = verifier.run_checks(task, worktree, runner)
    assert report.overall_pass is False
    assert len(report.critical_failures) > 0
    assert report.suggested_next_action == "repair"


def test_verifier_forbidden_path_violation(tmp_path: Path):
    _, worktree = _init_repo_with_worktree(tmp_path)
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    verifier = Verifier()

    # Agent creates a forbidden file
    (worktree / ".env").write_text("SECRET=123\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=str(worktree), check=True)
    subprocess.run(["git", "commit", "-m", "added env"], cwd=str(worktree), check=True)

    task = {
        "id": "t3",
        "acceptance": {
            "commands": [[sys.executable, "-c", "sys.exit(0)"]],
            "forbidden_paths": [".env*"],
        },
    }

    report = verifier.run_checks(task, worktree, runner)
    assert report.overall_pass is False
    assert "forbidden_paths" in report.critical_failures
