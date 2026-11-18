"""Task queue management over SQLite with atomic lease and claim semantics."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from openagent_bridge.db import Database, InvalidTransitionError
from openagent_bridge.models import TaskStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TaskQueue:
    """Manages task scheduling, atomic worker claiming, and lease heartbeats."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, task_spec: dict[str, Any]) -> str:
        """Add a task to the queue in QUEUED status.

        Returns task ID. Idempotent: returns existing ID if idempotency_key matches.
        """
        task_spec = dict(task_spec)
        task_spec.setdefault("status", TaskStatus.NEW.value)
        task_id = self.db.insert_task(task_spec)

        task = self.db.get_task(task_id)
        if task and task["status"].upper() == TaskStatus.NEW.value:
            try:
                self.db.transition_task(
                    task_id,
                    TaskStatus.QUEUED.value,
                    {"type": "task.queued", "source": "queue"},
                )
            except InvalidTransitionError:
                pass

        return task_id

    def claim(self, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        """Atomically claim a QUEUED task with an expired or absent lease.

        Returns the claimed task record or None if no tasks are available.
        """
        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        now_str = _iso(now)
        lease_until_str = _iso(lease_until)

        conn = self.db.connection
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN IMMEDIATE")
            # Select first available task that is QUEUED and not actively leased
            cursor.execute(
                """
                SELECT id, status FROM tasks
                WHERE status = ?
                  AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (TaskStatus.QUEUED.value, now_str),
            )
            row = cursor.fetchone()
            if not row:
                conn.commit()
                return None

            task_id = row["id"]

            # Claim and transition to PREPARING in one atomic block
            cursor.execute(
                """
                UPDATE tasks
                SET lease_owner = ?,
                    lease_until = ?,
                    heartbeat_at = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_until_str, now_str, TaskStatus.PREPARING.value, now_str, task_id),
            )

            # Insert event
            cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?",
                (task_id,),
            )
            next_seq = cursor.fetchone()[0]

            import uuid, json
            cursor.execute(
                """
                INSERT INTO events (event_id, task_id, run_id, type, timestamp, sequence, source, severity, payload)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    task_id,
                    "task.claimed",
                    now_str,
                    next_seq,
                    f"worker:{worker_id}",
                    "info",
                    json.dumps({"worker_id": worker_id, "lease_until": lease_until_str}),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return self.db.get_task(task_id)

    def heartbeat(self, task_id: str, worker_id: str, lease_seconds: int = 60) -> bool:
        """Renew the worker's active lease on a task."""
        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        now_str = _iso(now)
        lease_until_str = _iso(lease_until)

        conn = self.db.connection
        try:
            cur = conn.execute(
                """
                UPDATE tasks
                SET heartbeat_at = ?,
                    lease_until = ?,
                    updated_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (now_str, lease_until_str, now_str, task_id, worker_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            return False

    def release(self, task_id: str, worker_id: str) -> None:
        """Release a lease without changing task status."""
        conn = self.db.connection
        try:
            conn.execute(
                """
                UPDATE tasks
                SET lease_owner = NULL,
                    lease_until = NULL
                WHERE id = ? AND lease_owner = ?
                """,
                (task_id, worker_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def cancel(self, task_id: str, reason: str = "User requested cancellation") -> bool:
        """Cancel a task and release any active lease."""
        task = self.db.get_task(task_id)
        if not task:
            return False

        current_status = task["status"].upper()
        if current_status in (TaskStatus.SUCCEEDED.value, TaskStatus.CANCELLED.value):
            return False

        try:
            self.db.transition_task(
                task_id,
                TaskStatus.CANCELLED.value,
                {"type": "task.cancelled", "reason": reason},
            )
            conn = self.db.connection
            conn.execute(
                "UPDATE tasks SET lease_owner = NULL, lease_until = NULL WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            return True
        except Exception:
            return False
