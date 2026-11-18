"""Tests for CommandRunner process execution, timeouts, and secret redaction."""

import sys
from pathlib import Path
from openagent_bridge.models import CommandSpec
from openagent_bridge.runner import CommandRunner


def test_command_success(tmp_path: Path):
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    spec = CommandSpec(
        argv=[sys.executable, "-c", "print('hello from runner')"],
        cwd=tmp_path,
        timeout_seconds=10,
    )
    result = runner.run(spec)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout_path.exists()
    assert "hello from runner" in result.stdout_path.read_text(encoding="utf-8")


def test_command_nonzero_exit(tmp_path: Path):
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    spec = CommandSpec(
        argv=[sys.executable, "-c", "import sys; print('err', file=sys.stderr); sys.exit(42)"],
        cwd=tmp_path,
        timeout_seconds=10,
    )
    result = runner.run(spec)
    assert result.exit_code == 42
    assert result.timed_out is False
    assert "err" in result.stderr_path.read_text(encoding="utf-8")


def test_command_not_found(tmp_path: Path):
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    spec = CommandSpec(
        argv=["nonexistent_command_12345_xyz"],
        cwd=tmp_path,
        timeout_seconds=10,
    )
    result = runner.run(spec)
    assert result.exit_code != 0
    assert result.timed_out is False
    assert result.error_message is not None


def test_command_timeout(tmp_path: Path):
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts", kill_grace_seconds=0.5)
    spec = CommandSpec(
        argv=[sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=1,
    )
    result = runner.run(spec)
    assert result.timed_out is True
    assert result.exit_code != 0


def test_secret_redaction(tmp_path: Path):
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    spec = CommandSpec(
        argv=[
            sys.executable,
            "-c",
            "print('API Key: sk-proj-1234567890abcdef1234567890abcdef')",
        ],
        cwd=tmp_path,
        timeout_seconds=10,
    )
    result = runner.run(spec)
    assert result.exit_code == 0
    content = result.stdout_path.read_text(encoding="utf-8")
    assert "sk-proj-" not in content
    assert "[REDACTED" in content


def test_cwd_with_spaces(tmp_path: Path):
    spaced_dir = tmp_path / "A Folder With Spaces"
    spaced_dir.mkdir(parents=True)
    runner = CommandRunner(artifact_dir=tmp_path / "artifacts")
    spec = CommandSpec(
        argv=[sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=spaced_dir,
        timeout_seconds=10,
    )
    result = runner.run(spec)
    assert result.exit_code == 0
    assert "A Folder With Spaces" in result.stdout_path.read_text(encoding="utf-8")
