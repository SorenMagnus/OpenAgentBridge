"""Tests for git repository management, worktree isolation, and diffs."""

import subprocess
from pathlib import Path
import pytest
from openagent_bridge.repository import (
    BlockedDirtyWorktreeError,
    RepositoryError,
    RepositoryManager,
)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    readme = path / "README.md"
    readme.write_text("Hello git\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(path), check=True)


def test_check_repo_clean(tmp_path: Path):
    repo_path = tmp_path / "my_repo"
    _init_git_repo(repo_path)

    mgr = RepositoryManager()
    info = mgr.check_repo(repo_path)
    assert info.root.resolve() == repo_path.resolve()
    assert info.dirty is False
    assert info.head_sha != ""


def test_check_not_a_repo(tmp_path: Path):
    mgr = RepositoryManager()
    with pytest.raises(RepositoryError):
        mgr.check_repo(tmp_path)


def test_dirty_repo_blocks_worktree(tmp_path: Path):
    repo_path = tmp_path / "dirty_repo"
    _init_git_repo(repo_path)

    # Modify file without committing
    (repo_path / "README.md").write_text("modified content\n", encoding="utf-8")

    mgr = RepositoryManager()
    assert mgr.is_dirty(repo_path) is True

    with pytest.raises(BlockedDirtyWorktreeError):
        mgr.create_worktree(
            repo_root=repo_path,
            task_id="task-dirty",
            data_dir=tmp_path / "data",
        )


def test_worktree_creation_and_isolation(tmp_path: Path):
    repo_path = tmp_path / "clean_repo"
    _init_git_repo(repo_path)

    mgr = RepositoryManager()
    data_dir = tmp_path / "data"
    worktree = mgr.create_worktree(
        repo_root=repo_path,
        task_id="task-001",
        data_dir=data_dir,
    )

    assert worktree.exists()
    assert (worktree / "README.md").exists()

    # Modify file in worktree
    (worktree / "README.md").write_text("Worktree branch modification\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(worktree), check=True)
    subprocess.run(["git", "commit", "-m", "Worktree commit"], cwd=str(worktree), check=True)

    # Main repo must be untouched
    assert (repo_path / "README.md").read_text(encoding="utf-8") == "Hello git\n"

    # Get diff
    diff = mgr.get_diff(worktree)
    assert "README.md" in diff.changed_files

    # Clean removal
    mgr.remove_worktree(worktree)
    assert not worktree.exists()


def test_worktree_with_spaces_in_path(tmp_path: Path):
    # Tests folder containing spaces
    spaced_repo = tmp_path / "Path With Spaces" / "Repo Target"
    _init_git_repo(spaced_repo)

    mgr = RepositoryManager()
    data_dir = tmp_path / "Data Dir With Spaces"
    worktree = mgr.create_worktree(
        repo_root=spaced_repo,
        task_id="task-spaced-123",
        data_dir=data_dir,
    )

    assert worktree.exists()
    assert (worktree / "README.md").exists()

    mgr.remove_worktree(worktree)
    assert not worktree.exists()
