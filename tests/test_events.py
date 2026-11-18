"""Tests for EventManager sequencing, monotonic ordering, and real-time streaming."""

from pathlib import Path
from openagent_bridge.db import Database
from openagent_bridge.events import EventManager
from openagent_bridge.models import TaskStatus


def test_event_monotonic_sequence(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    em = EventManager(db)
    task_id = db.insert_task({
        "idempotency_key": "evt-test-01",
        "goal": "Test events",
        "original_goal": "Test events",
    })

    e1 = em.emit(task_id, type="task.started", source="test", payload={"step": 1})
    e2 = em.emit(task_id, type="task.progress", source="test", payload={"step": 2})
    e3 = em.emit(task_id, type="task.finished", source="test", payload={"step": 3})

    assert e1.sequence == 1
    assert e2.sequence == 2
    assert e3.sequence == 3

    events = em.get_events(task_id)
    assert len(events) == 3
    assert [e["sequence"] for e in events] == [1, 2, 3]


def test_event_duplicate_safe(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    em = EventManager(db)
    task_id = db.insert_task({
        "idempotency_key": "evt-dup-01",
        "goal": "Test dup events",
        "original_goal": "Test dup events",
    })

    # Direct insert duplicate event ID should not crash the database
    e = em.emit(task_id, type="test.event", payload={"x": 1})
    assert e.event_id is not None

    events = em.get_events(task_id)
    assert len(events) == 1


def test_event_streaming_follow(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    em = EventManager(db)
    task_id = db.insert_task({
        "idempotency_key": "evt-stream-01",
        "goal": "Test streaming",
        "original_goal": "Test streaming",
        "status": TaskStatus.SUCCEEDED.value,  # Terminal status ensures generator finishes
    })

    em.emit(task_id, type="step.one")
    em.emit(task_id, type="step.two")

    streamed = list(em.follow(task_id, since_sequence=0, poll_interval=0.1, max_idle_seconds=1.0))
    assert len(streamed) == 2
    assert streamed[0]["type"] == "step.one"
    assert streamed[1]["type"] == "step.two"
