"""Tests for LockManager distributed SQLite locks with TTL and renewal."""

import time
from pathlib import Path
from openagent_bridge.db import Database
from openagent_bridge.locks import LockManager


def test_lock_acquire_and_release(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    lm = LockManager(db)

    # Worker 1 acquires lock
    acq1 = lm.acquire(name="project-lock", owner="worker-1", ttl_seconds=10)
    assert acq1 is True

    # Worker 2 attempts same lock — fails
    acq2 = lm.acquire(name="project-lock", owner="worker-2", ttl_seconds=10)
    assert acq2 is False

    # Worker 1 releases lock
    rel = lm.release(name="project-lock", owner="worker-1")
    assert rel is True

    # Now Worker 2 can acquire
    acq3 = lm.acquire(name="project-lock", owner="worker-2", ttl_seconds=10)
    assert acq3 is True


def test_lock_ttl_expiration(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    lm = LockManager(db)

    # Acquire lock with 1s TTL
    lm.acquire(name="short-lock", owner="worker-1", ttl_seconds=1)

    # Wait for TTL to expire
    time.sleep(1.2)

    # Worker 2 should steal the expired lock
    acq = lm.acquire(name="short-lock", owner="worker-2", ttl_seconds=10)
    assert acq is True


def test_lock_renewal(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    lm = LockManager(db)

    lm.acquire(name="renew-lock", owner="worker-1", ttl_seconds=2)

    # Renew lock
    renewed = lm.renew(name="renew-lock", owner="worker-1", ttl_seconds=20)
    assert renewed is True

    # Wrong owner cannot renew
    wrong = lm.renew(name="renew-lock", owner="worker-fake", ttl_seconds=20)
    assert wrong is False
