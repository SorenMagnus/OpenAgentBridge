"""SQLite database manager for OpenAgent Bridge.

Provides transactional storage for tasks, runs, events, verification results,
and approvals. Uses WAL mode for concurrent read access.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


def _to_str(val: Any) -> Any:
    """Safely convert Path or datetime to string for SQLite storage."""
    if val is None:
        return None
    if isinstance(val, Path):
        return str(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return val


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    idempotency_key   TEXT UNIQUE,
    project_id        TEXT,
    goal              TEXT,
    original_goal     TEXT,
    non_goals         TEXT,       -- JSON
    constraints       TEXT,       -- JSON
    acceptance        TEXT,       -- JSON
    status            TEXT NOT NULL DEFAULT 'NEW',
    base_commit       TEXT,
    branch            TEXT,
    worktree_path     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    iteration         INTEGER NOT NULL DEFAULT 0,
    max_iterations    INTEGER,
    max_wall_minutes  INTEGER,
    lease_owner       TEXT,
    lease_until       TEXT,
    heartbeat_at      TEXT,
    executor          TEXT,       -- JSON
    permissions       TEXT,       -- JSON
    budget            TEXT        -- JSON
);

CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    run_id     TEXT,
    type       TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    source     TEXT,
    severity   TEXT,
    payload    TEXT,              -- JSON
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    iteration         INTEGER NOT NULL,
    agent             TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    exit_code         INTEGER,
    stdout_path       TEXT,
    stderr_path       TEXT,
    agent_events_path TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS verification_results (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    run_id          TEXT,
    check_name      TEXT,
    kind            TEXT,
    argv            TEXT,          -- JSON
    start_time      TEXT,
    end_time        TEXT,
    duration_ms     INTEGER,
    exit_code       INTEGER,
    passed          INTEGER,
    stdout_artifact TEXT,
    stderr_artifact TEXT,
    metric          TEXT,          -- JSON
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    action       TEXT,
    status       TEXT,
    requested_at TEXT,
    resolved_at  TEXT,
    resolved_by  TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS locks (
    name       TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    acquired   TEXT NOT NULL,
    expires    TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id        TEXT PRIMARY KEY,
    task_id   TEXT NOT NULL,
    run_id    TEXT,
    kind      TEXT,
    path      TEXT,
    created_at TEXT NOT NULL,
    metadata  TEXT,               -- JSON
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS budgets (
    task_id            TEXT PRIMARY KEY,
    max_iterations     INTEGER,
    max_wall_minutes   INTEGER,
    max_agent_invocations INTEGER,
    used_iterations    INTEGER NOT NULL DEFAULT 0,
    used_wall_minutes  REAL NOT NULL DEFAULT 0,
    used_invocations   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_events_task   ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_task     ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_vr_task       ON verification_results(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
"""

# Valid status transitions — imported lazily to avoid circular deps.
# Duplicated here so the DB layer is self-contained; the authoritative set
# lives in models.py.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "NEW":              {"VALIDATING", "PREPARING", "QUEUED", "RUNNING", "VERIFYING", "BLOCKED", "CANCELLED"},
    "VALIDATING":       {"QUEUED", "PREPARING", "FAILED", "BLOCKED", "CANCELLED"},
    "QUEUED":           {"PREPARING", "CANCELLED"},
    "PREPARING":        {"RUNNING", "BLOCKED", "FAILED", "CANCELLED"},
    "RUNNING":          {"VERIFYING", "FAILED", "BUDGET_EXCEEDED", "CANCELLED", "RECOVERING"},
    "VERIFYING":        {"SUCCEEDED", "REPAIR_PLANNED", "REVIEW_REQUIRED", "FAILED", "BUDGET_EXCEEDED", "CANCELLED"},
    "REPAIR_PLANNED":   {"RUNNING", "QUEUED", "FAILED", "CANCELLED", "BUDGET_EXCEEDED"},
    "REVIEW_REQUIRED":  {"SUCCEEDED", "REPAIR_PLANNED", "FAILED", "CANCELLED"},
    "RECOVERING":       {"QUEUED", "RUNNING", "PREPARING", "FAILED", "CANCELLED"},
    "BLOCKED":          {"QUEUED", "VALIDATING", "CANCELLED", "FAILED"},
    "SUCCEEDED":        {"REVIEW_REQUIRED"},
    "FAILED":           {"QUEUED", "RECOVERING", "CANCELLED"},
    "BUDGET_EXCEEDED":  {"QUEUED", "REVIEW_REQUIRED", "CANCELLED"},
    "CANCELLED":        set(),
}


class InvalidTransitionError(Exception):
    """Raised when a task status transition is not allowed."""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """Thin wrapper around a SQLite database for OpenAgent Bridge."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- helpers -------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- tasks ---------------------------------------------------------------

    def insert_task(self, task_spec: dict[str, Any]) -> str:
        """Insert a new task. Idempotent: returns existing id if the
        idempotency_key already exists.

        *task_spec* is a dict whose keys match the tasks columns (JSON-typed
        columns may be passed as Python objects — they will be serialised).
        """
        idem_key = task_spec.get("idempotency_key")
        if idem_key is not None:
            existing = self.get_task_by_idempotency_key(idem_key)
            if existing is not None:
                return existing["id"]

        task_id = task_spec.get("id") or _gen_id()
        now = _utcnow()

        json_cols = {
            "non_goals", "constraints", "acceptance",
            "executor", "permissions", "budget",
        }
        task_columns = {
            "id", "idempotency_key", "project_id", "goal", "original_goal",
            "non_goals", "constraints", "acceptance", "status", "base_commit",
            "branch", "worktree_path", "created_at", "updated_at", "iteration",
            "max_iterations", "max_wall_minutes", "lease_owner", "lease_until",
            "heartbeat_at", "executor", "permissions", "budget",
        }

        # Normalize project_id if passed as project_name
        spec_copy = dict(task_spec)
        if "project_name" in spec_copy and "project_id" not in spec_copy:
            spec_copy["project_id"] = spec_copy["project_name"]

        # If budget dict provided, propagate scalar limits if not explicitly set
        if isinstance(spec_copy.get("budget"), dict):
            b = spec_copy["budget"]
            if "max_iterations" in b and "max_iterations" not in spec_copy:
                spec_copy["max_iterations"] = b["max_iterations"]
            if "max_wall_minutes" in b and "max_wall_minutes" not in spec_copy:
                spec_copy["max_wall_minutes"] = b["max_wall_minutes"]

        row: dict[str, Any] = {"id": task_id, "created_at": now, "updated_at": now}
        for key, value in spec_copy.items():
            if key not in task_columns:
                continue
            if key in json_cols and not isinstance(value, str) and value is not None:
                row[key] = json.dumps(value)
            else:
                row[key] = _to_str(value)
        row.setdefault("status", "NEW")
        row.setdefault("iteration", 0)

        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        try:
            self._conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_task_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def transition_task(
        self,
        task_id: str,
        new_status: str,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        """Atomically update a task's status and insert a transition event.

        Raises ``InvalidTransitionError`` if the transition is not legal.
        Raises ``ValueError`` if the task does not exist.
        """
        try:
            cur = self._conn.execute(
                "SELECT id, status FROM tasks WHERE id = ?", (task_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Task {task_id} not found")

            current_status = str(row["status"]).upper()
            target_status = str(new_status).upper()
            allowed = _VALID_TRANSITIONS.get(current_status, set())
            if target_status not in allowed:
                raise InvalidTransitionError(
                    f"Cannot transition task {task_id} from "
                    f"{current_status!r} to {target_status!r}"
                )

            now = _utcnow()

            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (target_status, now, task_id),
            )

            event_id = _gen_id()
            # Determine next sequence number for this task.
            seq_cur = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?",
                (task_id,),
            )
            next_seq = seq_cur.fetchone()[0]

            event_type = (event_payload or {}).get("type", "status_transition")
            event_source = (event_payload or {}).get("source", "db")
            event_severity = (event_payload or {}).get("severity", "INFO")

            payload = {
                "from_status": current_status,
                "to_status": target_status,
                **(event_payload or {}),
            }
            self._conn.execute(
                "INSERT INTO events (event_id, task_id, type, timestamp, sequence, source, severity, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    task_id,
                    event_type,
                    now,
                    next_seq,
                    event_source,
                    event_severity,
                    json.dumps(payload),
                ),
            )
            self._conn.commit()
        except (InvalidTransitionError, ValueError):
            self._conn.rollback()
            raise
        except Exception:
            self._conn.rollback()
            raise

    # -- events --------------------------------------------------------------

    def insert_event(self, event: dict[str, Any]) -> str:
        """Insert an event row. Returns the event_id."""
        event_id = event.get("event_id") or _gen_id()
        now = event.get("timestamp") or _utcnow()
        payload = event.get("payload")
        if payload is not None and not isinstance(payload, str):
            payload = json.dumps(payload)

        # Auto-sequence if not provided.
        seq = event.get("sequence")
        if seq is None:
            task_id = event.get("task_id", "")
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?",
                (task_id,),
            )
            seq = cur.fetchone()[0]

        try:
            self._conn.execute(
                "INSERT INTO events (event_id, task_id, run_id, type, timestamp, sequence, source, severity, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event.get("task_id"),
                    event.get("run_id"),
                    event.get("type"),
                    now,
                    seq,
                    event.get("source"),
                    event.get("severity"),
                    payload,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return event_id

    def get_events(self, task_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY sequence ASC",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- runs ----------------------------------------------------------------

    def insert_run(self, run: dict[str, Any]) -> str:
        """Insert a run record. Returns the run id."""
        run_id = run.get("id") or _gen_id()
        try:
            self._conn.execute(
                "INSERT INTO runs (id, task_id, iteration, agent, started_at, ended_at, "
                "exit_code, stdout_path, stderr_path, agent_events_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    run.get("task_id"),
                    run.get("iteration"),
                    run.get("agent"),
                    _to_str(run.get("started_at")),
                    _to_str(run.get("ended_at")),
                    run.get("exit_code"),
                    _to_str(run.get("stdout_path")),
                    _to_str(run.get("stderr_path")),
                    _to_str(run.get("agent_events_path")),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return run_id

    def update_run(self, run: dict[str, Any]) -> None:
        """Update a run record. ``run`` must contain ``id``."""
        run_id = run["id"]
        fields: list[str] = []
        values: list[Any] = []
        for col in (
            "task_id", "iteration", "agent", "started_at", "ended_at",
            "exit_code", "stdout_path", "stderr_path", "agent_events_path",
        ):
            if col in run:
                fields.append(f"{col} = ?")
                values.append(run[col])
        if not fields:
            return
        values.append(run_id)
        try:
            self._conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_runs(self, task_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY iteration ASC",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- verification results ------------------------------------------------

    def insert_verification_result(self, result: dict[str, Any]) -> str:
        """Insert a verification result. Returns its id."""
        result_id = result.get("id") or _gen_id()
        argv = result.get("argv")
        if argv is not None and not isinstance(argv, str):
            argv = json.dumps(argv)
        metric = result.get("metric")
        if metric is not None and not isinstance(metric, str):
            metric = json.dumps(metric)

        try:
            self._conn.execute(
                "INSERT INTO verification_results "
                "(id, task_id, run_id, check_name, kind, argv, start_time, end_time, "
                "duration_ms, exit_code, passed, stdout_artifact, stderr_artifact, metric) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result_id,
                    result.get("task_id"),
                    result.get("run_id"),
                    result.get("check_name"),
                    result.get("kind"),
                    argv,
                    _to_str(result.get("start_time")),
                    _to_str(result.get("end_time")),
                    result.get("duration_ms"),
                    result.get("exit_code"),
                    1 if result.get("passed") else 0,
                    _to_str(result.get("stdout_artifact")),
                    _to_str(result.get("stderr_artifact")),
                    metric,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return result_id

    def get_verification_results(self, task_id: str) -> list[dict[str, Any]]:
        """Get all verification results for a task."""
        cur = self._conn.execute(
            "SELECT * FROM verification_results WHERE task_id = ?",
            (task_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- approvals -----------------------------------------------------------

    def insert_approval(self, approval: dict[str, Any]) -> str:
        """Insert an approval record. Returns its id."""
        approval_id = approval.get("id") or _gen_id()
        try:
            self._conn.execute(
                "INSERT INTO approvals (id, task_id, action, status, requested_at, resolved_at, resolved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    approval.get("task_id"),
                    approval.get("action"),
                    approval.get("status"),
                    approval.get("requested_at") or _utcnow(),
                    approval.get("resolved_at"),
                    approval.get("resolved_by"),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return approval_id
