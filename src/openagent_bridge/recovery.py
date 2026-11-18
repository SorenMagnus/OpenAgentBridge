"""Crash recovery and expired task lease management."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.db import Database, InvalidTransitionError
from openagent_bridge.models import TaskStatus


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
            WHERE status IN (?, ?, ?)
              AND (lease_until IS NOT NULL AND lease_until < ?)
            ORDER BY created_at ASC
            """,
            (
                TaskStatus.PREPARING.value,
                TaskStatus.RUNNING.value,
                TaskStatus.VERIFYING.value,
                now_str,
            ),
        )
        return [dict(r) for r in cur.fetchall()]

    def recover_task(self, task_id: str) -> str:
        """Inspect and recover a single expired/crashed task.

        Returns the new status (QUEUED or FAILED).
        """
        task = self.db.get_task(task_id)
        if not task:
            return "NOT_FOUND"

        old_status = task["status"].upper()
        old_owner = task.get("lease_owner")
        old_lease = task.get("lease_until")

        # 1. Transition to RECOVERING
        try:
            self.db.transition_task(
                task_id,
                TaskStatus.RECOVERING.value,
                {
                    "type": "task.recovery_started",
                    "reason": "lease_expired",
                    "previous_status": old_status,
                    "previous_owner": old_owner,
                    "previous_lease_until": old_lease,
                },
            )
        except InvalidTransitionError:
            pass

        # 2. Check worktree viability
        worktree_path_str = task.get("worktree_path")
        worktree_viable = False
        if worktree_path_str:
            wt_path = Path(worktree_path_str)
            worktree_viable = wt_path.exists() and (wt_path / ".git").exists()

        # 3. Check retry iteration count
        iteration = task.get("iteration") or 0
        max_iterations = task.get("max_iterations") or 5

        conn = self.db.connection
        # Release old lease
        conn.execute(
            "UPDATE tasks SET lease_owner = NULL, lease_until = NULL, heartbeat_at = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()

        # 4. Decide next action: requeue or fail
        if iteration < max_iterations:
            # Requeue for retry
            new_status = TaskStatus.QUEUED.value
            self.db.transition_task(
                task_id,
                new_status,
                {
                    "type": "task.recovered",
                    "action": "requeued",
                    "worktree_viable": worktree_viable,
                    "iteration": iteration,
                },
            )
        else:
            new_status = TaskStatus.FAILED.value
            self.db.transition_task(
                task_id,
                new_status,
                {
                    "type": "task.recovery_failed",
                    "reason": "max_iterations_reached_during_recovery",
                },
            )

        return new_status

    def recover_all(self) -> list[dict[str, Any]]:
        """Recover all in-flight tasks with expired leases."""
        expired = self.find_expired_tasks()
        results = []
        for t in expired:
            task_id = t["id"]
            new_status = self.recover_task(task_id)
            results.append({"task_id": task_id, "recovered_status": new_status})
        return results
