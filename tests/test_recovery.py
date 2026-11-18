"""Tests for crash recovery, lease expiration detection, and safe requeuing."""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from openagent_bridge.db import Database
from openagent_bridge.models import TaskStatus
from openagent_bridge.recovery import TaskRecovery


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), check=True)
    (path / "file.txt").write_text("content", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True)


def test_recovery_detects_expired_in_flight_tasks(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    recovery = TaskRecovery(db, tmp_path)

    # 1. Create task that simulated crash: RUNNING with past lease_until
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()

    crashed_id = db.insert_task({
        "id": "crashed-001",
        "idempotency_key": "crash-k-01",
        "goal": "Crashed task",
        "original_goal": "Crashed task",
        "status": TaskStatus.RUNNING.value,
        "lease_owner": "dead-worker",
        "lease_until": past,
    })

    healthy_id = db.insert_task({
        "id": "healthy-001",
        "idempotency_key": "healthy-k-01",
        "goal": "Healthy task",
        "original_goal": "Healthy task",
        "status": TaskStatus.RUNNING.value,
        "lease_owner": "active-worker",
        "lease_until": future,
    })

    expired = recovery.find_expired_tasks()
    expired_ids = [t["id"] for t in expired]
    assert "crashed-001" in expired_ids
    assert "healthy-001" not in expired_ids

    # 2. Recover crashed task
    new_status = recovery.recover_task("crashed-001")
    assert new_status == TaskStatus.QUEUED.value

    # Verify task state in database
    recovered_task = db.get_task("crashed-001")
    assert recovered_task["status"].upper() == TaskStatus.QUEUED.value
    assert recovered_task["lease_owner"] is None
    assert recovered_task["lease_until"] is None

    # Verify recovery events emitted
    events = db.get_events("crashed-001")
    event_types = [e["type"] for e in events]
    assert "task.recovery_started" in event_types
    assert "task.recovered" in event_types
