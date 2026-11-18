"""Data models for OpenAgent Bridge.

All domain objects are plain dataclasses so they stay serialisation-agnostic.
Enums use ``enum.Enum`` with string values for readable JSON/YAML round-trips.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """Every state a task can be in throughout its lifecycle."""

    NEW = "NEW"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REPAIR_PLANNED = "REPAIR_PLANNED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    RECOVERING = "RECOVERING"


VALID_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.NEW: frozenset({
        TaskStatus.VALIDATING,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.VALIDATING: frozenset({
        TaskStatus.QUEUED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.QUEUED: frozenset({
        TaskStatus.PREPARING,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.PREPARING: frozenset({
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.VERIFYING,
        TaskStatus.FAILED,
        TaskStatus.BUDGET_EXCEEDED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    }),
    TaskStatus.VERIFYING: frozenset({
        TaskStatus.SUCCEEDED,
        TaskStatus.REPAIR_PLANNED,
        TaskStatus.REVIEW_REQUIRED,
        TaskStatus.FAILED,
    }),
    TaskStatus.REPAIR_PLANNED: frozenset({
        TaskStatus.RECOVERING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.REVIEW_REQUIRED: frozenset({
        TaskStatus.SUCCEEDED,
        TaskStatus.REPAIR_PLANNED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.SUCCEEDED: frozenset(),  # terminal
    TaskStatus.FAILED: frozenset({
        TaskStatus.RECOVERING,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.BUDGET_EXCEEDED: frozenset({
        TaskStatus.REVIEW_REQUIRED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.CANCELLED: frozenset(),  # terminal
    TaskStatus.BLOCKED: frozenset({
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.RECOVERING: frozenset({
        TaskStatus.PREPARING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
}
"""Legal transitions per status.  Any transition not listed here MUST be
rejected by the state-machine layer."""


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandSpec:
    """Describes a single process invocation."""

    argv: list[str]
    cwd: Path
    timeout_seconds: int
    env_overrides: dict[str, str] | None = None
    capture_output: bool = True


# ---------------------------------------------------------------------------
# Task specification (what the user/caller provides)
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    """The *intent* behind a task — everything needed before execution starts."""

    schema_version: str
    task_id: str
    idempotency_key: str
    project_id: str
    goal: str
    original_goal: str
    non_goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance: dict[str, Any] = field(default_factory=lambda: {
        "commands": [],           # list of argv lists
        "required_files": [],     # list of relative path strings
        "forbidden_paths": [],    # list of glob patterns
    })
    executor: dict[str, str] = field(default_factory=lambda: {
        "preferred": "",
        "fallback": "",
    })
    budget: dict[str, int] = field(default_factory=lambda: {
        "max_iterations": 5,
        "max_wall_minutes": 30,
        "max_agent_invocations": 10,
    })
    permissions: dict[str, bool] = field(default_factory=lambda: {
        "filesystem": True,
        "network": False,
        "merge": False,
        "deploy": False,
    })


# ---------------------------------------------------------------------------
# Task (runtime state)
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """A *live* task — combines the spec with mutable runtime state."""

    # -- identity --
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # -- spec fields (inlined for flat access) --
    schema_version: str = "1"
    task_id: str = ""
    idempotency_key: str = ""
    project_id: str = ""
    goal: str = ""
    original_goal: str = ""
    non_goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance: dict[str, Any] = field(default_factory=dict)
    executor: dict[str, str] = field(default_factory=dict)
    budget: dict[str, int] = field(default_factory=dict)
    permissions: dict[str, bool] = field(default_factory=dict)
    # -- runtime state --
    status: TaskStatus = TaskStatus.NEW
    base_commit: str = ""
    branch: str = ""
    worktree_path: Path | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    iteration: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    heartbeat_at: datetime | None = None


# ---------------------------------------------------------------------------
# Run / verification / evaluation
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    """One invocation of an agent against a task."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    iteration: int = 0
    agent: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    exit_code: int | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    agent_events_path: Path | None = None


@dataclass
class VerificationResult:
    """Outcome of a single verification check (test suite, linter, etc.)."""

    check_name: str
    kind: str  # e.g. "pytest", "mypy", "lint", "custom"
    argv: list[str] = field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_ms: int = 0
    exit_code: int = -1
    passed: bool = False
    stdout_artifact: Path | None = None
    stderr_artifact: Path | None = None
    metric: dict[str, Any] | None = None


@dataclass
class EvaluationReport:
    """Aggregated result across all verification checks for a run."""

    task_id: str
    run_id: str
    base_commit: str
    head_commit: str
    checks: list[VerificationResult] = field(default_factory=list)
    critical_failures: list[str] = field(default_factory=list)
    overall_pass: bool = False
    suggested_next_action: str = ""


# ---------------------------------------------------------------------------
# Events / approvals
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """A structured event emitted at any point in the task lifecycle."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    run_id: str | None = None
    type: str = ""          # e.g. "task.created", "run.started"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    sequence: int = 0
    source: str = ""        # component that emitted the event
    severity: str = "info"  # info | warning | error | critical
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Approval:
    """A human-in-the-loop approval gate."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str = ""
    action: str = ""        # what is being approved, e.g. "merge", "deploy"
    status: str = "pending"  # pending | approved | rejected
    requested_at: datetime = field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None
    resolved_by: str | None = None
