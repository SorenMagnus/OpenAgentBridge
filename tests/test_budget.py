"""Tests for BudgetTracker limits, dimensions, and database synchronization."""

from pathlib import Path
from openagent_bridge.budget import BudgetLimits, BudgetTracker
from openagent_bridge.db import Database


def test_budget_iteration_limit():
    limits = BudgetLimits(max_iterations=3, max_agent_invocations=10, max_wall_minutes=60)
    tracker = BudgetTracker(limits)

    exceeded, _ = tracker.is_exceeded()
    assert exceeded is False

    tracker.record_iteration()
    tracker.record_iteration()
    exceeded, _ = tracker.is_exceeded()
    assert exceeded is False

    tracker.record_iteration()
    exceeded, reason = tracker.is_exceeded()
    assert exceeded is True
    assert "max_iterations" in reason


def test_budget_invocation_limit():
    limits = BudgetLimits(max_iterations=10, max_agent_invocations=2, max_wall_minutes=60)
    tracker = BudgetTracker(limits)

    tracker.record_invocation()
    exceeded, _ = tracker.is_exceeded()
    assert exceeded is False

    tracker.record_invocation()
    exceeded, reason = tracker.is_exceeded()
    assert exceeded is True
    assert "max_agent_invocations" in reason


def test_budget_db_sync(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    task_id = db.insert_task({
        "idempotency_key": "bgt-sync-01",
        "goal": "Test budget sync",
        "original_goal": "Test budget sync",
    })

    limits = BudgetLimits(max_iterations=5, max_wall_minutes=30, max_agent_invocations=8)
    tracker = BudgetTracker(limits)
    tracker.record_iteration()
    tracker.record_invocation()
    tracker.sync_to_db(db, task_id)

    cur = db.connection.execute("SELECT * FROM budgets WHERE task_id = ?", (task_id,))
    row = cur.fetchone()
    assert row is not None
    assert row["used_iterations"] == 1
    assert row["used_invocations"] == 1
    assert row["max_iterations"] == 5
