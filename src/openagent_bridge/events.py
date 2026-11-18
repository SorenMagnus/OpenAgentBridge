"""Event emission, ordering, and streaming event subscription."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Generator

from openagent_bridge.db import Database
from openagent_bridge.models import Event, TaskStatus


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.REVIEW_REQUIRED.value,
    TaskStatus.BUDGET_EXCEEDED.value,
}


class EventManager:
    """Publishes and queries structured lifecycle events."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def emit(
        self,
        task_id: str,
        type: str,
        source: str = "bridge",
        severity: str = "info",
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> Event:
        """Emit a new event with strictly monotonic sequence number for task_id."""
        payload_dict = payload or {}
        event_id = str(uuid.uuid4())
        now = _utcnow_iso()

        conn = self.db.connection
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?",
                (task_id,),
            )
            next_seq = cursor.fetchone()[0]

            cursor.execute(
                """
                INSERT INTO events (event_id, task_id, run_id, type, timestamp, sequence, source, severity, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    task_id,
                    run_id,
                    type,
                    now,
                    next_seq,
                    source,
                    severity,
                    json.dumps(payload_dict),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Handle duplicate event_id safely
            conn.rollback()

        return Event(
            event_id=event_id,
            task_id=task_id,
            run_id=run_id,
            type=type,
            timestamp=datetime.now(timezone.utc),
            sequence=next_seq,
            source=source,
            severity=severity,
            payload=payload_dict,
        )

    def get_events(self, task_id: str, since_sequence: int = 0) -> list[dict[str, Any]]:
        """Retrieve events for task_id with sequence > since_sequence, ordered ascending."""
        cur = self.db.connection.execute(
            """
            SELECT * FROM events
            WHERE task_id = ? AND sequence > ?
            ORDER BY sequence ASC
            """,
            (task_id, since_sequence),
        )
        events = []
        for r in cur.fetchall():
            d = dict(r)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except Exception:
                    pass
            events.append(d)
        return events

    def follow(
        self,
        task_id: str,
        since_sequence: int = 0,
        poll_interval: float = 0.5,
        max_idle_seconds: float = 300.0,
    ) -> Generator[dict[str, Any], None, None]:
        """Stream events in real-time until the task reaches a terminal state."""
        last_seq = since_sequence
        start_idle = time.time()

        while True:
            new_events = self.get_events(task_id, since_sequence=last_seq)
            if new_events:
                for evt in new_events:
                    yield evt
                    last_seq = max(last_seq, evt.get("sequence", last_seq))
                start_idle = time.time()

            # Check if task is terminal
            task = self.db.get_task(task_id)
            if not task:
                break

            status = task.get("status", "").upper()
            if status in TERMINAL_STATUSES and not new_events:
                break

            if (time.time() - start_idle) > max_idle_seconds:
                break

            time.sleep(poll_interval)
