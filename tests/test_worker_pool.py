"""Exercise worker claims, heartbeat, recovery and graceful shutdown under contention."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from openagent_bridge.config import BridgeConfig, WorkerConfig
from openagent_bridge.db import Database
from openagent_bridge.queue import TaskQueue
from openagent_bridge.recovery import TaskRecovery
from openagent_bridge.worker import Worker


def test_two_workers_claim_once_keep_leases_and_stop(tmp_path):
    config = BridgeConfig(data_dir=tmp_path, worker=WorkerConfig(
        concurrency=2, heartbeat_seconds=1, lease_seconds=3,
    ))
    db = Database(tmp_path / "bridge.db")
    queue = TaskQueue(db)
    expired = db.insert_task({
        "goal": "Recover this task",
        "status": "PREPARING",
        "lease_owner": "crashed-worker",
        "lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    })
    task_ids = {expired}
    for index in range(23):
        task_ids.add(queue.enqueue({"goal": f"Task {index}"}))

    started = threading.Barrier(3)
    release = threading.Event()
    claimed = {}
    workers = [Worker(config=config) for _ in range(config.worker.concurrency)]
    for worker in workers:
        def execute(task, worker=worker):
            task_id = task["id"]
            worker.db.transition_task(task_id, "RUNNING")
            first = worker.worker_id not in claimed
            if first:
                claimed[worker.worker_id] = task
                started.wait(timeout=8)
                assert release.wait(timeout=8)
            worker.db.transition_task(task_id, "VERIFYING")
            worker.db.transition_task(task_id, "SUCCEEDED")
            return task
        worker._execute_task = execute

    with ThreadPoolExecutor(max_workers=2) as pool:
        threads = [pool.submit(worker.run_forever, 0.01) for worker in workers]
        try:
            started.wait(timeout=8)
            assert len({task["id"] for task in claimed.values()}) == 2
            deadline = time.monotonic() + 5
            # Wait past the original lease to prove heartbeat keeps recovery from stealing it.
            while any(datetime.now(timezone.utc).isoformat() <= task["lease_until"] for task in claimed.values()):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert TaskRecovery(db).recover_all() == []
            for task in claimed.values():
                assert db.get_task(task["id"])["heartbeat_at"] > task["heartbeat_at"]
            release.set()
            deadline = time.monotonic() + 8
            while any(db.get_task(task_id)["status"] != "SUCCEEDED" for task_id in task_ids):
                assert time.monotonic() < deadline
                time.sleep(0.02)
        finally:
            release.set()
            for worker in workers:
                worker.stop()
            for thread in threads:
                thread.result(timeout=8)
            for worker in workers:
                worker.db.close()

    for task_id in task_ids:
        assert db.get_task(task_id)["lease_owner"] is None
        events = db.get_events(task_id)
        assert sum(event["type"] == "task.claimed" for event in events) == 1
        assert sum(event["type"] == "task.recovered" for event in events) == (task_id == expired)
    db.close()

def test_stale_worker_cannot_transition_a_reclaimed_task(tmp_path):
    import pytest

    from openagent_bridge.db import InvalidTransitionError

    db = Database(tmp_path / "bridge.db")
    stale = Database(tmp_path / "bridge.db")
    task_id = db.insert_task({
        "goal": "Reclaimed work",
        "status": "RUNNING",
        "lease_owner": "original",
        "lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    })
    assert TaskRecovery(db).recover_task(task_id) == "QUEUED"
    assert TaskQueue(db).claim("replacement")["id"] == task_id
    db.transition_task(task_id, "RUNNING")
    with stale.guard_worker_lease(task_id, "original"):
        with pytest.raises(InvalidTransitionError, match="no longer holds the lease"):
            stale.transition_task(task_id, "VERIFYING")
    assert db.get_task(task_id)["lease_owner"] == "replacement"
    assert db.get_task(task_id)["status"] == "RUNNING"
    stale.close()
    db.close()
