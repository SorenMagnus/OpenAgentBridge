"""Review and fast-forward an approved task result from a local operator shell."""

import argparse
import json
import subprocess
from pathlib import Path

from openagent_bridge.approvals import ApprovalGate
from openagent_bridge.config import load_config
from openagent_bridge.db import Database
from openagent_bridge.repository import RepositoryError, RepositoryManager, _run_git


def merge_task(db: Database, task_id: str, target: str, *, request=False, execute=False):
    task = db.get_task(task_id)
    if not task or task["status"] not in {"SUCCEEDED", "REVIEW_REQUIRED"}:
        raise RepositoryError("A completed or review-ready task is required")
    project = db.get_project(task["project_id"])
    if not project or not task.get("worktree_path"):
        raise RepositoryError("Task must have a registered project and worktree")
    root = Path(project["path"])
    target_info = RepositoryManager.check_repo(root)
    result = RepositoryManager.check_repo(Path(task["worktree_path"]))
    if target_info.branch != target or target in {"HEAD", result.branch}:
        raise RepositoryError("Check out the target branch in the project before planning")
    if result.branch != f"openagent/{task_id}" or result.dirty or target_info.dirty:
        raise RepositoryError("Both worktrees must be clean and the task branch must match")
    if _run_git("rev-parse", f"refs/heads/{result.branch}", cwd=root).stdout.strip() != result.head_sha:
        raise RepositoryError("Project and task result disagree")
    if _run_git("merge-base", "--is-ancestor", target_info.head_sha, result.head_sha, cwd=root,
                check=False).returncode:
        raise RepositoryError("Target has diverged from task result")
    action = f"merge:{target}:{target_info.head_sha}:{result.head_sha}"
    gate = ApprovalGate(db)
    plan = {"target": target, "base": target_info.head_sha, "result": result.head_sha,
            "approved": gate.is_approved(task_id, action)}
    if request and not plan["approved"]:
        pending = next((row for row in gate.list_pending(task_id) if row["action"] == action), None)
        plan["approval_id"] = pending["id"] if pending else gate.request(task_id, action)
    if execute:
        if not plan["approved"]:
            raise RepositoryError("Approval required for this exact target and result")
        _run_git("merge", "--ff-only", result.head_sha, cwd=root)
        plan["merged"] = True
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--target", required=True, help="Checked-out destination branch")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--request-approval", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    config = load_config()
    if args.execute and config.policy.merge_main in (False, "deny", "forbidden"):
        parser.exit(1, "Merge is disabled by administrator policy\n")
    db = Database(config.data_dir / "bridge.db")
    try:
        plan = merge_task(db, args.task_id, args.target, request=args.request_approval,
                          execute=args.execute)
    except (RepositoryError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(plan, indent=2))
