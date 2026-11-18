"""Resource and iteration budget tracking for autonomous execution loops."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from openagent_bridge.db import Database


@dataclass(frozen=True)
class BudgetLimits:
    max_iterations: int = 5
    max_wall_minutes: int = 120
    max_agent_invocations: int = 8
    stop_after_no_improvement: int = 2

    @classmethod
    def for_task(cls, task: dict[str, Any], config: Any) -> BudgetLimits:
        budget = task.get("budget", {})
        if isinstance(budget, str):
            try:
                budget = json.loads(budget)
            except ValueError:
                budget = {}

        def cap(name: str) -> int:
            maximum = max(0, getattr(config, name))
            requested = budget.get(name) if isinstance(budget, dict) else None
            if requested is None and name in ("max_iterations", "max_wall_minutes"):
                requested = task.get(name)
            if requested is None:
                return maximum
            try:
                return max(0, min(maximum, int(requested)))
            except (TypeError, ValueError, OverflowError):
                return 0

        return cls(
            max_iterations=cap("max_iterations"),
            max_wall_minutes=cap("max_wall_minutes"),
            max_agent_invocations=cap("max_agent_invocations"),
            stop_after_no_improvement=cap("stop_after_no_improvement"),
        )


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
        self.usage = BudgetUsage(start_time=time.monotonic())

    @property
    def deadline(self) -> float:
        return self.usage.start_time + 60 * self.limits.max_wall_minutes

    @property
    def wall_exceeded(self) -> bool:
        return time.monotonic() >= self.deadline

    @classmethod
    def resume(cls, limits: BudgetLimits, db: Database, task_id: str) -> BudgetTracker:
        tracker = cls(limits)
        previous = db.connection.execute(
            "SELECT used_iterations, used_wall_minutes, used_invocations FROM budgets WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if previous is not None:
            tracker.usage.used_iterations = previous["used_iterations"]
            tracker.usage.used_invocations = previous["used_invocations"]
            tracker.usage.start_time -= previous["used_wall_minutes"] * 60
        return tracker

    def record_invocation(self) -> None:
        """Increment agent invocation count and update elapsed wall minutes."""
        self.usage.used_invocations += 1
        self._update_wall_time()

    def record_iteration(self) -> None:
        """Increment iteration count and update elapsed wall minutes."""
        self.usage.used_iterations += 1
        self._update_wall_time()

    def _update_wall_time(self) -> None:
        elapsed_sec = time.monotonic() - self.usage.start_time
        self.usage.used_wall_minutes = elapsed_sec / 60.0

    def is_exceeded(self) -> tuple[bool, str | None]:
        """Check if any budget dimension has been exceeded.

        Returns (is_exceeded, reason_string).
        """
        self._update_wall_time()

        if self.usage.used_iterations >= self.limits.max_iterations:
            return True, f"max_iterations ({self.limits.max_iterations}) reached"

        if self.usage.used_invocations >= self.limits.max_agent_invocations:
            return True, f"max_agent_invocations ({self.limits.max_agent_invocations}) reached"

        if self.wall_exceeded:
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
                    max_iterations = excluded.max_iterations,
                    max_wall_minutes = excluded.max_wall_minutes,
                    max_agent_invocations = excluded.max_agent_invocations,
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
            raise
