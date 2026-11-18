"""Human-in-the-loop approval management for high-risk actions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from openagent_bridge.db import Database


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalGate:
    """Manages explicit human approval requests for sensitive operations."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def request(self, task_id: str, action: str) -> str:
        """Create a pending approval request for a sensitive action."""
        approval_id = str(uuid.uuid4())
        now = _utcnow_iso()
        conn = self.db.connection
        try:
            conn.execute(
                """
                INSERT INTO approvals (id, task_id, action, status, requested_at, resolved_at, resolved_by)
                VALUES (?, ?, ?, 'pending', ?, NULL, NULL)
                """,
                (approval_id, task_id, action, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return approval_id

    def list_pending(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """List all pending approval requests, optionally filtered by task_id."""
        conn = self.db.connection
        if task_id:
            cur = conn.execute(
                "SELECT * FROM approvals WHERE task_id = ? AND status = 'pending' ORDER BY requested_at ASC",
                (task_id,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY requested_at ASC"
            )
        return [dict(r) for r in cur.fetchall()]

    def resolve(self, approval_id: str, decision: str, resolved_by: str = "human") -> bool:
        """Resolve a pending approval request (decision must be 'approved' or 'rejected')."""
        decision_norm = decision.strip().lower()
        if decision_norm not in ("approved", "rejected"):
            raise ValueError(f"Decision must be 'approved' or 'rejected', got: {decision}")

        now = _utcnow_iso()
        conn = self.db.connection
        try:
            cur = conn.execute(
                """
                UPDATE approvals
                SET status = ?, resolved_at = ?, resolved_by = ?
                WHERE id = ? AND status = 'pending'
                """,
                (decision_norm, now, resolved_by, approval_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            return False

    def is_approved(self, task_id: str, action: str) -> bool:
        """Check if an action has been explicitly approved for a task."""
        conn = self.db.connection
        cur = conn.execute(
            """
            SELECT id FROM approvals
            WHERE task_id = ? AND action = ? AND status = 'approved'
            """,
            (task_id, action),
        )
        return cur.fetchone() is not None
