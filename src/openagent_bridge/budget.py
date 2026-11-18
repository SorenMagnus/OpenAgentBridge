"""Resource and iteration budget tracking for autonomous execution loops."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from openagent_bridge.db import Database


@dataclass
class BudgetLimits:
    max_iterations: int = 5
    max_wall_minutes: int = 120
    max_agent_invocations: int = 8
    stop_after_no_improvement: int = 2


@dataclass
class BudgetUsage:
    used_iterations: int = 0
    used_wall_minutes: float = 0.0
    used_invocations: int = 0
    start_time: float = 0.0


class BudgetTracker:
    """Enforces wall-clock, iteration, and invocation limits on tasks."""

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or BudgetLimits()
        self.usage = BudgetUsage(start_time=time.time())

    def record_invocation(self) -> None:
        """Increment agent invocation count and update elapsed wall minutes."""
        self.usage.used_invocations += 1
        self._update_wall_time()

    def record_iteration(self) -> None:
        """Increment iteration count and update elapsed wall minutes."""
        self.usage.used_iterations += 1
        self._update_wall_time()

    def _update_wall_time(self) -> None:
        elapsed_sec = time.time() - self.usage.start_time
        self.usage.used_wall_minutes = round(elapsed_sec / 60.0, 2)

    def is_exceeded(self) -> tuple[bool, str | None]:
        """Check if any budget dimension has been exceeded.

        Returns (is_exceeded, reason_string).
        """
        self._update_wall_time()

        if self.usage.used_iterations >= self.limits.max_iterations:
            return True, f"max_iterations ({self.limits.max_iterations}) reached"

        if self.usage.used_invocations >= self.limits.max_agent_invocations:
            return True, f"max_agent_invocations ({self.limits.max_agent_invocations}) reached"

        if self.usage.used_wall_minutes >= self.limits.max_wall_minutes:
            return True, f"max_wall_minutes ({self.limits.max_wall_minutes}) exceeded"

        return False, None

    def sync_to_db(self, db: Database, task_id: str) -> None:
        """Persist current budget usage into SQLite budgets table."""
        self._update_wall_time()
        conn = db.connection
        try:
            conn.execute(
                """
                INSERT INTO budgets (task_id, max_iterations, max_wall_minutes, max_agent_invocations,
                                     used_iterations, used_wall_minutes, used_invocations)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    used_iterations = excluded.used_iterations,
                    used_wall_minutes = excluded.used_wall_minutes,
                    used_invocations = excluded.used_invocations
                """,
                (
                    task_id,
                    self.limits.max_iterations,
                    self.limits.max_wall_minutes,
                    self.limits.max_agent_invocations,
                    self.usage.used_iterations,
                    self.usage.used_wall_minutes,
                    self.usage.used_invocations,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
