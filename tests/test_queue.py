"""Tests for TaskQueue scheduling, atomic claiming, heartbeats, and cancellation."""

import time
from pathlib import Path
from openagent_bridge.db import Database
from openagent_bridge.models import TaskStatus
from openagent_bridge.queue import TaskQueue


def test_queue_enqueue_and_claim(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    queue = TaskQueue(db)

    task_spec = {
        "id": "t-queue-01",
        "idempotency_key": "k-01",
        "goal": "Test queue task",
        "original_goal": "Test queue task",
    }
    task_id = queue.enqueue(task_spec)
    assert task_id == "t-queue-01"

    # Worker 1 claims task
    claimed = queue.claim(worker_id="w-01", lease_seconds=10)
    assert claimed is not None
    assert claimed["id"] == "t-queue-01"
    assert claimed["lease_owner"] == "w-01"
    assert claimed["status"].upper() == TaskStatus.PREPARING.value

    # Worker 2 attempts claim — must be None because task is actively leased
    claimed_again = queue.claim(worker_id="w-02", lease_seconds=10)
    assert claimed_again is None


def test_queue_heartbeat_renewal(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    queue = TaskQueue(db)

    task_id = queue.enqueue({
        "id": "t-queue-02",
        "idempotency_key": "k-02",
        "goal": "Test heartbeat",
        "original_goal": "Test heartbeat",
    })

    claimed = queue.claim(worker_id="w-01", lease_seconds=2)
    assert claimed is not None

    # Heartbeat from correct worker succeeds
    renewed = queue.heartbeat(task_id, worker_id="w-01", lease_seconds=10)
    assert renewed is True

    # Heartbeat from wrong worker fails
    wrong_renew = queue.heartbeat(task_id, worker_id="w-other", lease_seconds=10)
    assert wrong_renew is False


def test_queue_cancellation(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    queue = TaskQueue(db)

    task_id = queue.enqueue({
        "id": "t-cancel-01",
        "idempotency_key": "k-cancel-01",
        "goal": "Task to cancel",
        "original_goal": "Task to cancel",
    })

    # Cancel task
    cancelled = queue.cancel(task_id, reason="User cancelled via test")
    assert cancelled is True

    task = db.get_task(task_id)
    assert task["status"].upper() == TaskStatus.CANCELLED.value
    assert task["lease_owner"] is None

    # Cannot claim cancelled task
    claimed = queue.claim(worker_id="w-01", lease_seconds=10)
    assert claimed is None
