"""Tests for SQLite database operations, transactions, and state transitions."""

from pathlib import Path
import pytest
from openagent_bridge.db import Database, InvalidTransitionError


def test_database_initialization(tmp_path: Path):
    db_file = tmp_path / "test.db"
    db = Database(db_file)
    assert db_file.exists()

    # Check table existence
    tables = [
        r[0] for r in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "tasks" in tables
    assert "events" in tables
    assert "runs" in tables
    assert "verification_results" in tables
    db.close()


def test_task_insertion_and_idempotency(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    task_spec = {
        "idempotency_key": "unique-task-123",
        "project_id": "test-project",
        "goal": "Fix bug in login",
        "original_goal": "Fix bug in login",
        "status": "NEW",
    }

    task_id1 = db.insert_task(task_spec)
    assert task_id1 is not None

    # Duplicate insertion with same idempotency key
    task_id2 = db.insert_task(task_spec)
    assert task_id1 == task_id2

    task = db.get_task(task_id1)
    assert task is not None
    assert task["goal"] == "Fix bug in login"
    assert task["status"] == "NEW"
    db.close()


def test_state_transitions(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    task_id = db.insert_task({
        "idempotency_key": "task-transition-test",
        "project_id": "p1",
        "goal": "Test transitions",
        "original_goal": "Test transitions",
        "status": "NEW",
    })

    # Legal transition: NEW -> VALIDATING
    db.transition_task(task_id, "VALIDATING", {"reason": "starting validation"})
    task = db.get_task(task_id)
    assert task["status"] == "VALIDATING"

    # Legal transition: VALIDATING -> QUEUED
    db.transition_task(task_id, "QUEUED")
    task = db.get_task(task_id)
    assert task["status"] == "QUEUED"

    # Illegal transition: QUEUED -> SUCCEEDED directly
    with pytest.raises(InvalidTransitionError):
        db.transition_task(task_id, "SUCCEEDED")

    # Verify task state was NOT modified after failed transition
    task = db.get_task(task_id)
    assert task["status"] == "QUEUED"

    # Verify event sequence
    events = db.get_events(task_id)
    assert len(events) >= 2
    assert events[0]["sequence"] == 1
    assert events[1]["sequence"] == 2
    db.close()


def test_verification_results_storage(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    task_id = db.insert_task({
        "idempotency_key": "task-vr-test",
        "project_id": "p1",
        "goal": "Test VR",
        "original_goal": "Test VR",
    })

    db.insert_verification_result({
        "id": "vr-1",
        "task_id": task_id,
        "run_id": "run-1",
        "check_name": "pytest",
        "kind": "command",
        "argv": ["python", "-m", "pytest"],
        "exit_code": 0,
        "passed": 1,
    })

    results = db.get_verification_results(task_id)
    assert len(results) == 1
    assert results[0]["check_name"] == "pytest"
    assert results[0]["passed"] == 1
    db.close()
