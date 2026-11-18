"""Git repository operations for OpenAgent Bridge.

All git commands run via ``subprocess`` with ``shell=False`` and an explicit
argv list. All paths use ``pathlib.Path`` and handle spaces correctly.

**Safety invariant**: this module NEVER runs destructive operations
(``git reset --hard``, ``git clean -fd``, ``git stash``) on the user's
main worktree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RepoInfo:
    """Snapshot of a repository's state."""
    root: Path
    branch: str
    head_sha: str
    dirty: bool

    def __getitem__(self, item: str) -> Any:
        if item == "repo_root":
            return str(self.root)
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except (AttributeError, KeyError):
            return default


@dataclass
class DiffInfo:
    """Result of ``get_diff``."""
    base_sha: str
    head_sha: str
    stat: str
    full_diff: str
    changed_files: list[str] = field(default_factory=list)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except (AttributeError, KeyError):
            return default


class RepositoryError(Exception):
    """General repository operation failure."""


class BlockedDirtyWorktreeError(RepositoryError):
    """Raised when a worktree cannot be created because the repo is dirty."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with *shell=False*.

    ``cwd`` is converted to ``str`` so paths with spaces work on Windows.
    """
    argv = ["git", *args]
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
        shell=False,
    )


# ---------------------------------------------------------------------------
# RepositoryManager
# ---------------------------------------------------------------------------

class RepositoryManager:
    """High-level Git operations needed by the bridge."""

    # -- inspection ----------------------------------------------------------

    @staticmethod
    def check_repo(path: Path) -> RepoInfo:
        """Return basic information about the repository at *path*.

        Raises ``RepositoryError`` if *path* is not inside a git repo.
        """
        path = Path(path)
        try:
            root_result = _run_git(
                "rev-parse", "--show-toplevel", cwd=path
            )
            repo_root = Path(root_result.stdout.strip())

            branch_result = _run_git(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=path
            )
            branch = branch_result.stdout.strip()

            sha_result = _run_git("rev-parse", "HEAD", cwd=path)
            head_sha = sha_result.stdout.strip()

            dirty = RepositoryManager.is_dirty(path)

            return RepoInfo(
                root=repo_root,
                branch=branch,
                head_sha=head_sha,
                dirty=dirty,
            )
        except subprocess.CalledProcessError as exc:
            raise RepositoryError(
                f"Not a git repository or git error at {path}: {exc.stderr}"
            ) from exc

    @staticmethod
    def is_dirty(path: Path) -> bool:
        """Return *True* if the working tree at *path* has uncommitted changes."""
        path = Path(path)
        result = _run_git(
            "status", "--porcelain", cwd=path, check=False
        )
        return bool(result.stdout.strip())

    # -- worktree management -------------------------------------------------

    @staticmethod
    def create_worktree(
        repo_root: Path,
        task_id: str,
        data_dir: Path,
    ) -> Path:
        """Create a new git worktree for a task.

        Branch: ``openagent/<task_id>``
        Worktree location: ``<data_dir>/worktrees/<task_id>``

        Raises ``BlockedDirtyWorktreeError`` if the repo has uncommitted
        changes (we never force-clean the user's tree).

        Returns the absolute path of the new worktree.
        """
        repo_root = Path(repo_root)
        data_dir = Path(data_dir)

        if RepositoryManager.is_dirty(repo_root):
            raise BlockedDirtyWorktreeError(
                f"Repository at {repo_root} has uncommitted changes. "
                "Commit or stash them before creating a worktree."
            )

        branch_name = f"openagent/{task_id}"
        worktree_path = data_dir / "worktrees" / task_id
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Prune any stale worktree registrations first
        _run_git("worktree", "prune", cwd=repo_root, check=False)

        # Check if branch exists
        branch_check = _run_git("rev-parse", "--verify", branch_name, cwd=repo_root, check=False)
        branch_exists = (branch_check.returncode == 0)

        # Create worktree (with new or existing branch)
        try:
            if branch_exists:
                _run_git(
                    "worktree", "add", "--force",
                    str(worktree_path),
                    branch_name,
                    cwd=repo_root,
                )
            else:
                _run_git(
                    "worktree", "add",
                    "-b", branch_name,
                    str(worktree_path),
                    "HEAD",
                    cwd=repo_root,
                )
        except subprocess.CalledProcessError as exc:
            raise RepositoryError(
                f"Failed to create worktree: {exc.stderr}"
            ) from exc

        return worktree_path.resolve()

    @staticmethod
    def remove_worktree(worktree_path: Path, repo_root: Path | None = None) -> None:
        """Remove a previously created worktree.

        Uses ``git worktree remove --force`` from the main repository.
        The associated branch is NOT deleted.
        """
        import shutil

        worktree_path = Path(worktree_path)
        if not worktree_path.exists():
            return

        main_repo: Path | None = Path(repo_root) if repo_root else None
        git_file = worktree_path / ".git"
        if main_repo is None and git_file.is_file():
            try:
                content = git_file.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    raw_gitdir = content.split(":", 1)[1].strip()
                    gitdir = Path(raw_gitdir)
                    # gitdir is like <repo>/.git/worktrees/<task>
                    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                        main_repo = gitdir.parent.parent.parent
            except Exception:
                pass

        removed = False
        if main_repo is not None and main_repo.exists():
            try:
                _run_git(
                    "worktree", "remove", "--force",
                    str(worktree_path),
                    cwd=main_repo,
                    check=False,
                )
                removed = not worktree_path.exists()
            except Exception:
                pass

        if not removed and worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
            if main_repo is not None and main_repo.exists():
                _run_git("worktree", "prune", cwd=main_repo, check=False)

    # -- diff ----------------------------------------------------------------

    @staticmethod
    def get_diff(worktree_path: Path) -> DiffInfo:
        """Return the diff between the worktree's merge-base and HEAD.

        The merge-base is computed against ``HEAD~`` of the main branch that
        the worktree branched from (falling back to the first commit if
        there is no parent).

        Returns a ``DiffInfo`` with stat, full diff, and list of changed
        file paths.
        """
        worktree_path = Path(worktree_path)

        # HEAD SHA
        head = _run_git("rev-parse", "HEAD", cwd=worktree_path)
        head_sha = head.stdout.strip()

        # Find merge-base against the first parent of the branch commit.
        # `git merge-base HEAD HEAD~1` gives the common ancestor — when
        # the branch was just created this is HEAD~1 itself.
        mb = _run_git(
            "merge-base", "HEAD", "HEAD~1",
            cwd=worktree_path,
            check=False,
        )
        if mb.returncode == 0 and mb.stdout.strip():
            base_sha = mb.stdout.strip()
        else:
            # Fallback: diff against the parent of HEAD, or empty tree.
            parent = _run_git(
                "rev-parse", "HEAD~1", cwd=worktree_path, check=False,
            )
            base_sha = (
                parent.stdout.strip()
                if parent.returncode == 0
                else "4b825dc642cb6eb9a060e54bf899d15f3f965"  # git empty tree
            )

        # Stat
        stat_result = _run_git(
            "diff", "--stat", base_sha, head_sha, cwd=worktree_path, check=False,
        )
        stat = stat_result.stdout.strip()

        # Full diff
        diff_result = _run_git(
            "diff", base_sha, head_sha, cwd=worktree_path, check=False,
        )
        full_diff = diff_result.stdout

        # Changed files list
        name_result = _run_git(
            "diff", "--name-only", base_sha, head_sha,
            cwd=worktree_path,
            check=False,
        )
        changed_files = [
            f for f in name_result.stdout.strip().splitlines() if f
        ]

        return DiffInfo(
            base_sha=base_sha,
            head_sha=head_sha,
            stat=stat,
            full_diff=full_diff,
            changed_files=changed_files,
        )
