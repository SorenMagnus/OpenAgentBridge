"""Crash recovery and expired task lease management."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.db import Database
from openagent_bridge.models import TaskStatus


RECOVERABLE_STATUSES = (
    TaskStatus.PREPARING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.REPAIR_PLANNED.value,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRecovery:
    """Detects orphaned or crashed tasks with expired leases and safely recovers them."""

    def __init__(self, db: Database, data_dir: Path | str | None = None) -> None:
        self.db = db
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".openagent"

    def find_expired_tasks(self) -> list[dict[str, Any]]:
        """Find in-flight tasks whose lease has expired."""
        now_str = _utcnow_iso()
        cur = self.db.connection.execute(
            """
            SELECT * FROM tasks
            WHERE status IN (?, ?, ?, ?)
              AND (lease_until IS NOT NULL AND lease_until < ?)
            ORDER BY created_at ASC
            """,
            (
                *RECOVERABLE_STATUSES, now_str,
            ),
        )
        return [dict(r) for r in cur.fetchall()]

    def recover_task(self, task_id: str) -> str:
        """Inspect and recover a single expired/crashed task.

        Returns the new status (QUEUED or FAILED).
        """
        conn = self.db.connection
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self.db.get_task(task_id)
            if task is None:
                return "NOT_FOUND"
            lease = task["lease_until"]
            if task["status"] not in RECOVERABLE_STATUSES or lease is None or lease >= _utcnow_iso():
                return "SKIPPED"

            self.db._transition_task(task_id, TaskStatus.RECOVERING.value, {
                "type": "task.recovery_started",
                "reason": "lease_expired",
                "previous_status": task["status"],
                "previous_owner": task["lease_owner"],
                "previous_lease_until": lease,
            })
            conn.execute(
                "UPDATE tasks SET lease_owner = NULL, lease_until = NULL, heartbeat_at = NULL WHERE id = ?",
                (task_id,),
            )
            iteration = task.get("iteration") or 0
            max_iterations = task.get("max_iterations") or 5
            if iteration >= max_iterations:
                target = TaskStatus.FAILED.value
                event = {
                    "type": "task.recovery_failed",
                    "reason": "max_iterations_reached_during_recovery",
                }
            else:
                target = TaskStatus.QUEUED.value
                worktree = task.get("worktree_path")
                event = {
                    "type": "task.recovered",
                    "action": "requeued",
                    "worktree_viable": bool(worktree and (Path(worktree) / ".git").exists()),
                    "iteration": iteration,
                }
            self.db._transition_task(task_id, target, event)
            return target

    def recover_all(self) -> list[dict[str, Any]]:
        """Recover all in-flight tasks with expired leases."""
        expired = self.find_expired_tasks()
        results = []
        for t in expired:
            task_id = t["id"]
            new_status = self.recover_task(task_id)
            if new_status not in ("SKIPPED", "NOT_FOUND"):
                results.append({"task_id": task_id, "recovered_status": new_status})
        return results
