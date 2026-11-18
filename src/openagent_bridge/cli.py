"""OpenAgent Bridge CLI — command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.telemetry import collect_telemetry


def _utcnow() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------

def _check_tool(argv: list[str]) -> str:
    """Check whether the named executable responds successfully to a fixed probe."""
    executable = shutil.which(argv[0])
    if executable is None:
        return "UNAVAILABLE"
    try:
        result = subprocess.run(
            [executable, *argv[1:]], capture_output=True, text=True,
            errors="replace", timeout=10, shell=False,
        )
    except OSError:
        return "PROBE_FAILED"
    except subprocess.TimeoutExpired:
        return "PROBE_FAILED"
    if result.returncode != 0:
        return "PROBE_FAILED"
    output = (result.stdout.strip() or result.stderr.strip()).splitlines()
    return f"VERIFIED  ({output[0]})" if output else "VERIFIED"


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run capability checks and print results."""
    from concurrent.futures import ThreadPoolExecutor

    tools = {
        "Python": [sys.executable, "--version"],
        "Git": ["git", "--version"],
        "Codex": ["codex", "--version"],
        "Claude": ["claude", "--version"],
        "Node": ["node", "--version"],
        "Blender": ["blender", "--version"],
        "GPU (NVIDIA)": ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
    }
    with ThreadPoolExecutor(max_workers=len(tools)) as pool:
        checks = dict(zip(tools, pool.map(_check_tool, tools.values())))
    if args.json:
        print(json.dumps(checks))
    else:
        print(f"OpenAgent Bridge Doctor ({platform.system()} {platform.release()})")
        for name, status in checks.items():
            print(f"  {name:14s} {status}")
    return 0


# ---------------------------------------------------------------------------
# project add command
# ---------------------------------------------------------------------------

def cmd_project_add(args: argparse.Namespace) -> int:
    """Register a project path in the database."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.repository import RepositoryManager

    config = load_config(Path(args.config) if args.config else None)
    repo_path = Path(args.path).resolve()

    repo_mgr = RepositoryManager()
    try:
        info = repo_mgr.check_repo(repo_path)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if info.get("dirty"):
        print(f"WARNING: repository at {repo_path} has uncommitted changes.")

    db_path = config.data_dir / "bridge.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)

    project_id = info.get("project_id", repo_path.name)
    db.register_project(project_id, repo_path)
    print(f"Registered project '{project_id}' at {repo_path}")
    print(f"  branch: {info.get('branch', 'unknown')}")
    print(f"  HEAD:   {info.get('head_sha', 'unknown')}")
    print(f"  dirty:  {info.get('dirty', False)}")
    return 0


# ---------------------------------------------------------------------------
# task submit command
# ---------------------------------------------------------------------------

def cmd_task_submit(args: argparse.Namespace) -> int:
    """Submit a task from a JSON spec file."""
    from dataclasses import asdict

    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.models import TaskSpec, TaskStatus
    from openagent_bridge.repository import RepositoryManager
    from openagent_bridge.runner import CommandRunner
    from openagent_bridge.verifier import Verifier
    from openagent_bridge.adapters.codex import CodexAdapter
    from openagent_bridge.adapters.base import AgentAdapter
    from openagent_bridge.adapters.claude_code import ClaudeCodeAdapter
    from openagent_bridge.budget import BudgetLimits, BudgetTracker
    from openagent_bridge.task_prompt import TaskPromptRenderer

    config = load_config(Path(args.config) if args.config else None)

    # a. Load TaskSpec from JSON file
    task_path = Path(args.task_file)
    if not task_path.exists():
        print(f"ERROR: task file not found: {task_path}", file=sys.stderr)
        return 1

    with open(task_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    spec = TaskSpec(
        schema_version=raw.get("schema_version", "0.1"),
        task_id=raw.get("task_id"),
        idempotency_key=raw["idempotency_key"],
        project_id=raw["project_id"],
        goal=raw["goal"],
        original_goal=raw.get("original_goal", raw["goal"]),
        non_goals=raw.get("non_goals", []),
        constraints=raw.get("constraints", []),
        acceptance=raw.get("acceptance", {"commands": [], "required_files": [], "forbidden_paths": []}),
        executor=raw.get("executor", {"preferred": "codex", "fallback": "claude-code"}),
        budget=raw.get("budget", {"max_iterations": 5, "max_wall_minutes": 120, "max_agent_invocations": 8}),
        permissions=raw.get("permissions", {"filesystem": "task_worktree", "network": "deny_by_default", "merge": "human_approval", "deploy": "forbidden"}),
    )

    db_path = config.data_dir / "bridge.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)

    # b. Check idempotency
    existing = db.get_task_by_idempotency_key(spec.idempotency_key)
    if existing:
        print(f"Task already exists with id={existing['id']} (idempotency_key={spec.idempotency_key})")
        return 0

    # c. Assign task_id
    task_id = spec.task_id or str(uuid.uuid4())

    tracker = BudgetTracker(BudgetLimits.for_task({"budget": spec.budget}, config.autonomy))

    # d. Check repo dirty status
    repo_mgr = RepositoryManager()
    project_path = Path(args.project) if hasattr(args, "project") and args.project else Path.cwd()
    repo_info = None
    preflight_exceeded, _ = tracker.is_exceeded()
    if not preflight_exceeded:
        try:
            repo_info = repo_mgr.check_repo(project_path, deadline=tracker.deadline)
        except subprocess.TimeoutExpired:
            pass  # Record the task and its exhausted budget below.
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if repo_info is not None and repo_info.dirty:
        # Insert task as BLOCKED
        db.insert_task({
            "id": task_id,
            "idempotency_key": spec.idempotency_key,
            "project_id": spec.project_id,
            "goal": spec.goal,
            "original_goal": spec.original_goal,
            "non_goals": spec.non_goals,
            "constraints": spec.constraints,
            "acceptance": spec.acceptance,
            "status": TaskStatus.BLOCKED.value,
            "base_commit": repo_info.get("head_sha", ""),
            "branch": "",
            "worktree_path": "",
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "iteration": 0,
            "max_iterations": spec.budget.get("max_iterations", 5),
            "max_wall_minutes": spec.budget.get("max_wall_minutes", 120),
            "executor": spec.executor,
            "permissions": spec.permissions,
            "budget": spec.budget,
        })
        print(f"Task {task_id} BLOCKED — repository has uncommitted changes.")
        return 1

    # Insert task as NEW then transition
    db.insert_task({
        "id": task_id,
        "idempotency_key": spec.idempotency_key,
        "project_id": spec.project_id,
        "goal": spec.goal,
        "original_goal": spec.original_goal,
        "non_goals": spec.non_goals,
        "constraints": spec.constraints,
        "acceptance": spec.acceptance,
        "status": TaskStatus.NEW.value,
        "base_commit": repo_info.head_sha if repo_info else "",
        "branch": f"openagent/{task_id}" if repo_info else "",
        "worktree_path": "",
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
        "iteration": 0,
        "max_iterations": spec.budget.get("max_iterations", 5),
        "max_wall_minutes": spec.budget.get("max_wall_minutes", 120),
        "executor": spec.executor,
        "permissions": spec.permissions,
        "budget": spec.budget,
    })

    def stop_for_budget(reason: str) -> int:
        tracker.sync_to_db(db, task_id)
        db.transition_task(
            task_id, TaskStatus.BUDGET_EXCEEDED.value,
            {"type": "task.budget_exceeded", "reason": reason},
        )
        print(f"Task {task_id} BUDGET_EXCEEDED ({reason})", file=sys.stderr)
        return 1

    db.transition_task(task_id, TaskStatus.PREPARING.value, {"type": "task.preparing"})
    exceeded, reason = tracker.is_exceeded()
    if exceeded or repo_info is None:
        return stop_for_budget(reason or "Repository inspection deadline reached")
    try:
        worktree_path = repo_mgr.create_worktree(
            repo_root=Path(repo_info["repo_root"]),
            task_id=task_id,
            data_dir=config.data_dir,
            deadline=tracker.deadline,
        )
    except subprocess.TimeoutExpired:
        return stop_for_budget("Worktree preparation deadline reached")
    except Exception as exc:
        db.transition_task(task_id, TaskStatus.FAILED.value, {"type": "task.failed", "reason": str(exc)})
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    db.connection.execute("UPDATE tasks SET worktree_path = ? WHERE id = ?", (str(worktree_path), task_id))
    db.connection.commit()
    db.transition_task(task_id, TaskStatus.RUNNING.value, {"type": "task.running"})

    # f. Run agent (Codex preferred, Claude fallback)
    runner = CommandRunner(config=config)
    agent_name = spec.executor.get("preferred", "codex")

    codex_adapter = CodexAdapter()
    claude_adapter = ClaudeCodeAdapter()

    adapter = None
    if agent_name == "codex" and codex_adapter.detect():
        adapter = codex_adapter
    elif claude_adapter.detect():
        adapter = claude_adapter
        agent_name = "claude-code"
    else:
        print(f"ERROR: No agent available (tried codex, claude-code)", file=sys.stderr)
        db.transition_task(task_id, TaskStatus.FAILED.value, {"type": "task.failed", "reason": "no agent available"})
        return 1

    if isinstance(adapter, AgentAdapter):
        adapter.execution_deadline = tracker.deadline
        adapter.probe_timeout_limit = config.runner.default_timeout_seconds
    task_data = db.get_task(task_id)
    cmd_spec = adapter.build_command(
        prompt=TaskPromptRenderer.for_execution(
            task_data, tracker.limits, data_dir=config.data_dir,
            repo_path=Path(repo_info["repo_root"]), worktree=worktree_path,
            policy=asdict(config.policy),
        ),
        worktree=worktree_path,
        sandbox_mode="workspace-write",
    )
    if tracker.wall_exceeded:
        return stop_for_budget("Agent deadline reached")
    tracker.record_invocation()
    tracker.sync_to_db(db, task_id)
    run_result = runner.run(cmd_spec, deadline=tracker.deadline)

    run_id = str(uuid.uuid4())
    db.insert_run({
        "id": run_id,
        "task_id": task_id,
        "iteration": 1,
        "agent": agent_name,
        "started_at": _utcnow(),
        "ended_at": _utcnow(),
        "exit_code": run_result.exit_code,
        "stdout_path": str(run_result.stdout_path),
        "stderr_path": str(run_result.stderr_path),
        **collect_telemetry(agent_name, run_result.stdout_path),
    })

    tracker.sync_to_db(db, task_id)
    if run_result.timed_out or tracker.wall_exceeded:
        return stop_for_budget("Agent deadline reached")

    # g. Run verification checks
    db.transition_task(task_id, TaskStatus.VERIFYING.value, {"type": "task.verifying"})
    verifier = Verifier()
    task_data = db.get_task(task_id)
    tracker.record_iteration()
    tracker.sync_to_db(db, task_id)
    report = verifier.run_checks(task_data, worktree_path, runner, deadline=tracker.deadline)

    # Store verification results
    for check in report.checks:
        db.insert_verification_result({
            "id": str(uuid.uuid4()),
            "task_id": task_id,
            "run_id": run_id,
            "check_name": check.check_name,
            "kind": check.kind,
            "argv": check.argv,
            "start_time": check.start_time,
            "end_time": check.end_time,
            "duration_ms": check.duration_ms,
            "exit_code": check.exit_code,
            "passed": check.passed,
            "stdout_artifact": check.stdout_artifact,
            "stderr_artifact": check.stderr_artifact,
            "metric": check.metric,
        })

    tracker.sync_to_db(db, task_id)
    if tracker.wall_exceeded:
        return stop_for_budget("Verification deadline reached")

    # i. Transition to REVIEW_REQUIRED
    if report.overall_pass:
        db.transition_task(task_id, TaskStatus.REVIEW_REQUIRED.value, {"type": "task.review_required"})
        status_label = "REVIEW_REQUIRED"
    else:
        db.transition_task(task_id, TaskStatus.FAILED.value, {"type": "task.failed", "reason": "verification failed"})
        status_label = "FAILED"

    # j. Print summary
    print(f"\nTask Summary")
    print(f"  ID:          {task_id}")
    print(f"  Status:      {status_label}")
    print(f"  Agent:       {agent_name}")
    print(f"  Base SHA:    {report.base_commit or 'N/A'}")
    print(f"  Head SHA:    {report.head_commit or 'N/A'}")
    print(f"  Checks:      {len(report.checks)} run, {len(report.critical_failures)} failures")
    print(f"  Overall:     {'PASS' if report.overall_pass else 'FAIL'}")
    return 0 if report.overall_pass else 1


# ---------------------------------------------------------------------------
# task status command
# ---------------------------------------------------------------------------

def cmd_task_status(args: argparse.Namespace) -> int:
    """Show current status of a task."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")

    task = db.get_task(args.task_id)
    if not task:
        print(f"ERROR: task {args.task_id} not found", file=sys.stderr)
        return 1

    print(f"Task {task['id']}")
    print(f"  Status:    {task['status']}")
    print(f"  Project:   {task.get('project_id', 'N/A')}")
    print(f"  Goal:      {task.get('goal', 'N/A')}")
    print(f"  Created:   {task.get('created_at', 'N/A')}")
    print(f"  Updated:   {task.get('updated_at', 'N/A')}")
    print(f"  Iteration: {task.get('iteration', 0)}")
    return 0


# ---------------------------------------------------------------------------
# task report command
# ---------------------------------------------------------------------------

def cmd_task_report(args: argparse.Namespace) -> int:
    """Show detailed report for a task."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.repository import RepositoryManager

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")

    task = db.get_task(args.task_id)
    if not task:
        print(f"ERROR: task {args.task_id} not found", file=sys.stderr)
        return 1

    print(f"Task Report: {task['id']}")
    print(f"  Status:      {task['status']}")
    print(f"  Base SHA:    {task.get('base_commit', 'N/A')}")

    worktree_path = task.get("worktree_path")
    if worktree_path and Path(worktree_path).exists():
        repo_mgr = RepositoryManager()
        try:
            diff = repo_mgr.get_diff(Path(worktree_path))
            print(f"  Head SHA:    {diff.get('head_sha', 'N/A')}")
            changed = diff.get("changed_files", [])
            print(f"  Changed files ({len(changed)}):")
            for f in changed:
                print(f"    - {f}")
        except Exception:
            print(f"  Head SHA:    N/A")
    else:
        print(f"  Head SHA:    N/A")

    # Show runs
    runs = db.get_runs(args.task_id)
    if runs:
        print(f"  Runs:")
        for run in runs:
            print(f"    [{run.get('agent', '?')}] exit_code={run.get('exit_code', '?')} "
                  f"stdout={run.get('stdout_path', 'N/A')} stderr={run.get('stderr_path', 'N/A')}")
            if run["model"] is not None or run["usage"] is not None:
                print(f"      model={run['model']} usage={json.dumps(run['usage'])}")

    # Show verification results
    vr_list = db.get_verification_results(args.task_id)
    if vr_list:
        print(f"  Verification Results ({len(vr_list)}):")
        for vr in vr_list:
            status = "PASS" if vr.get("passed") else "FAIL"
            print(f"    [{status}] {vr.get('check_name')} (exit_code={vr.get('exit_code')}, duration={vr.get('duration_ms')}ms)")

    return 0


# ---------------------------------------------------------------------------
# pause / resume commands
# ---------------------------------------------------------------------------

def cmd_pause(args: argparse.Namespace) -> int:
    """Create STOP file to pause processing."""
    from openagent_bridge.config import load_config

    config = load_config(Path(args.config) if args.config else None)
    stop_file = config.data_dir / "STOP"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(_utcnow(), encoding="utf-8")
    print(f"Paused — STOP file created at {stop_file}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Remove STOP file to resume processing."""
    from openagent_bridge.config import load_config

    config = load_config(Path(args.config) if args.config else None)
    stop_file = config.data_dir / "STOP"
    if stop_file.exists():
        stop_file.unlink()
        print("Resumed — STOP file removed.")
    else:
        print("Not paused — no STOP file found.")
    return 0


# ---------------------------------------------------------------------------
# Phase 2 commands: worker, events, recover, cancel
# ---------------------------------------------------------------------------

def _run_workers(config, worker_id: str | None = None, stop_listener=None) -> None:
    """Run the configured worker pool and drain in-flight tasks at shutdown."""
    import threading
    from openagent_bridge.worker import Worker

    workers = []
    threads = []
    try:
        for index in range(config.worker.concurrency):
            name = worker_id
            if worker_id and config.worker.concurrency > 1:
                name = f"{worker_id}-{index + 1}"
            worker = Worker(config=config, worker_id=name)
            workers.append(worker)
            thread = threading.Thread(target=worker.run_forever, name=worker.worker_id)
            thread.start()
            threads.append(thread)
        print(f"[*] Background workers started: {len(threads)}")
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("Stopping workers...")
    finally:
        for worker in workers:
            worker.stop()
        try:
            if stop_listener is not None:
                stop_listener()
        finally:
            for thread in threads:
                thread.join()
            for worker in workers:
                worker.db.close()


def cmd_worker(args: argparse.Namespace) -> int:
    """Run one task or start the configured worker pool."""
    from openagent_bridge.config import load_config
    from openagent_bridge.worker import Worker

    config = load_config(args.config)
    if args.once:
        worker = Worker(config=config, worker_id=args.worker_id)
        try:
            print("Worker run_once finished:", worker.run_once())
        finally:
            worker.db.close()
    else:
        _run_workers(config, worker_id=args.worker_id)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """Query or stream events for a task."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.events import EventManager

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")
    em = EventManager(db)

    if args.follow:
        print(f"Streaming events for task {args.task_id} (Ctrl+C to stop)...")
        try:
            for evt in em.follow(args.task_id):
                print(f"[{evt.get('sequence', '?')}] {evt.get('timestamp', '')} {evt.get('type', '')} ({evt.get('source', '')}): {evt.get('payload', {})}")
        except KeyboardInterrupt:
            pass
        return 0

    events = em.get_events(args.task_id)
    print(f"Events for task {args.task_id} ({len(events)}):")
    for evt in events:
        print(f"  [{evt.get('sequence', '?')}] {evt.get('timestamp', '')} {evt.get('type', '')} ({evt.get('source', '')}): {evt.get('payload', {})}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """Recover orphaned or crashed tasks with expired leases."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.recovery import TaskRecovery

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")
    recovery = TaskRecovery(db, config.data_dir)
    recovered = recovery.recover_all()
    print(f"Recovered {len(recovered)} tasks:")
    for r in recovered:
        print(f"  Task {r['task_id']} -> {r['recovered_status']}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a task."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.queue import TaskQueue

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")
    queue = TaskQueue(db)
    reason = args.reason or "Cancelled via CLI"
    success = queue.cancel(args.task_id, reason=reason)
    if success:
        print(f"Task {args.task_id} cancelled.")
        return 0
    print(f"Failed to cancel task {args.task_id} (task not found or already terminal).", file=sys.stderr)
    return 1


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve a pending approval request."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.approvals import ApprovalGate

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")
    gate = ApprovalGate(db)
    success = gate.resolve(args.approval_id, decision="approved", resolved_by=args.by or "human")
    if success:
        print(f"Approval {args.approval_id} APPROVED by {args.by or 'human'}.")
        return 0
    print(f"Failed to approve {args.approval_id} (not found or already resolved).", file=sys.stderr)
    return 1


def cmd_reject(args: argparse.Namespace) -> int:
    """Reject a pending approval request."""
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.approvals import ApprovalGate

    config = load_config(Path(args.config) if args.config else None)
    db = Database(config.data_dir / "bridge.db")
    gate = ApprovalGate(db)
    success = gate.resolve(args.approval_id, decision="rejected", resolved_by=args.by or "human")
    if success:
        print(f"Approval {args.approval_id} REJECTED by {args.by or 'human'}.")
        return 0
    print(f"Failed to reject {args.approval_id} (not found or already resolved).", file=sys.stderr)
    return 1


def cmd_prompt(args: argparse.Namespace) -> int:
    """Local operator workflow for versioned agent guidance."""
    from dataclasses import asdict
    from openagent_bridge.config import load_config
    from openagent_bridge.db import Database
    from openagent_bridge.prompt_registry import PromptRegistry, PromptTemplate, digest
    from openagent_bridge.prompt_evaluator import BenchmarkCase, benchmark_inputs, evaluate_candidate
    from openagent_bridge.self_evolution import HarnessEvolutionEngine

    try:
        config = load_config(args.config)
        registry = PromptRegistry(config.data_dir / "prompts")
        template = "coding_task"
        if args.operation == "bootstrap":
            registry.save_template(PromptTemplate(template, 1, None, "", status="CHAMPION"))
        elif args.operation == "propose":
            db = Database(config.data_dir / "bridge.db")
            try:
                print(json.dumps(HarnessEvolutionEngine(db).propose_prompt(
                    registry, args.file.read_text(encoding="utf-8"), args.version)))
            finally:
                db.close()
        elif args.operation in ("inputs", "evaluate"):
            cases = [BenchmarkCase(**row) for row in json.loads(args.file.read_text(encoding="utf-8"))]
            if args.operation == "inputs":
                _, _, prompts = benchmark_inputs(registry, template, args.version, cases)
                print(json.dumps({
                    version: {
                        case_id: {"prompt": prompt, "prompt_sha256": digest(prompt)}
                        for case_id, prompt in entries.items()
                    }
                    for version, entries in prompts.items()
                }))
                return 0
            recordings = json.loads(args.recordings.read_text(encoding="utf-8"))

            def recorded_output(version: int, case_id: str, prompt: str) -> str:
                row = recordings[str(version)][case_id]
                if row["prompt_sha256"] != digest(prompt):
                    raise ValueError(f"Recorded prompt does not match version {version}, case {case_id}")
                return row["output"]

            result = evaluate_candidate(registry, template, args.version, cases, recorded_output)
            print(json.dumps(asdict(result)))
            return 0 if result.eligible_for_promotion else 1
        elif not registry.promote(template, args.version, args.approved_by):
            print("Candidate must pass evaluation before approval.", file=sys.stderr)
            return 1
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        print(f"Prompt workflow failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the HTTP REST server and background worker pool."""
    from openagent_bridge.config import load_config
    from openagent_bridge.core import BridgeCore
    from openagent_bridge.channels.http_channel import HTTPChannelAdapter

    config = load_config(args.config)
    core = BridgeCore(config)
    host = getattr(args, "host", "0.0.0.0") or "0.0.0.0"
    port = int(getattr(args, "port", 8765) or 8765)
    http_adapter = HTTPChannelAdapter(host=host, port=port)
    try:
        core.register_channel_adapter(http_adapter)
        http_adapter.start()
        print(f"[*] OpenAgent Bridge HTTP server running on http://{host}:{port}")
        _run_workers(config, stop_listener=http_adapter.stop)
    finally:
        http_adapter.stop()
        core.db.close()
    return 0


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="openagent",
        description="OpenAgent Bridge — orchestrate AI coding agents with structured task management.",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config YAML file (default: ~/.openagent/config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # doctor
    doctor = subparsers.add_parser("doctor", help="Run capability checks")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable tool capabilities")

    # project
    project_parser = subparsers.add_parser("project", help="Project management")
    project_sub = project_parser.add_subparsers(dest="project_command")
    project_add = project_sub.add_parser("add", help="Register a project")
    project_add.add_argument("path", help="Path to the git repository")

    # task
    task_parser = subparsers.add_parser("task", help="Task management")
    task_sub = task_parser.add_subparsers(dest="task_command")

    task_submit = task_sub.add_parser("submit", help="Submit a task from JSON spec")
    task_submit.add_argument("task_file", help="Path to task spec JSON file")
    task_submit.add_argument("--project", "-p", default=None, help="Project path (default: cwd)")

    task_status = task_sub.add_parser("status", help="Show task status")
    task_status.add_argument("task_id", help="Task ID")

    task_report = task_sub.add_parser("report", help="Show task report")
    task_report.add_argument("task_id", help="Task ID")

    publish = task_sub.add_parser("publish-artifact", help="Publish a local file for this task")
    publish.add_argument("task_id")
    publish.add_argument("file", type=Path)

    # pause / resume
    subparsers.add_parser("pause", help="Pause processing (create STOP file)")
    subparsers.add_parser("resume", help="Resume processing (remove STOP file)")

    # worker
    worker_parser = subparsers.add_parser("worker", help="Run background worker")
    worker_parser.add_argument("--worker-id", default=None, help="Custom worker ID")
    worker_parser.add_argument("--once", action="store_true", help="Process only one task and exit")

    # events
    events_parser = subparsers.add_parser("events", help="Query or follow task events")
    events_parser.add_argument("task_id", help="Task ID")
    events_parser.add_argument("--follow", "-f", action="store_true", help="Follow event stream in real time")

    # recover
    subparsers.add_parser("recover", help="Recover crashed tasks with expired leases")

    # cancel
    cancel_parser = subparsers.add_parser("cancel", help="Cancel a task")
    cancel_parser.add_argument("task_id", help="Task ID to cancel")
    cancel_parser.add_argument("--reason", default=None, help="Reason for cancellation")

    # approve / reject
    approve_parser = subparsers.add_parser("approve", help="Approve an approval request")
    approve_parser.add_argument("approval_id", help="Approval ID")
    approve_parser.add_argument("--by", default=None, help="Approver identity")

    reject_parser = subparsers.add_parser("reject", help="Reject an approval request")
    reject_parser.add_argument("approval_id", help="Approval ID")
    reject_parser.add_argument("--by", default=None, help="Rejecter identity")

    prompt = subparsers.add_parser("prompt", help="Manage offline prompt candidates")
    operations = prompt.add_subparsers(dest="operation", required=True)
    operations.add_parser("bootstrap", help="Register the default empty guidance as version 1")
    propose = operations.add_parser("propose", help="Register a candidate from a guidance text file")
    propose.add_argument("version", type=int)
    propose.add_argument("file", type=Path)
    inputs = operations.add_parser("inputs", help="Render benchmark inputs for offline model runs")
    evaluate = operations.add_parser("evaluate", help="Replay recorded benchmark outputs")
    for action in (inputs, evaluate):
        action.add_argument("version", type=int)
        action.add_argument("file", type=Path, help="JSON list of validation and holdout cases")
    evaluate.add_argument("recordings", type=Path, help="JSON outputs keyed by version and case ID")
    approve = operations.add_parser("approve", help="Record explicit operator approval")
    approve.add_argument("version", type=int)
    approve.add_argument("--approved-by", required=True)

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start HTTP REST API server and background worker")
    serve_parser.add_argument("--host", default="0.0.0.0", help="HTTP server bind host (default: 0.0.0.0)")
    serve_parser.add_argument("--port", "-p", type=int, default=8765, help="HTTP server bind port (default: 8765)")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    dispatch: dict[str, Any] = {
        "doctor": cmd_doctor,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "worker": cmd_worker,
        "serve": cmd_serve,
        "prompt": cmd_prompt,
        "events": cmd_events,
        "recover": cmd_recover,
        "cancel": cmd_cancel,
        "approve": cmd_approve,
        "reject": cmd_reject,
    }

    if args.command in dispatch:
        return dispatch[args.command](args)

    if args.command == "project":
        if args.project_command == "add":
            return cmd_project_add(args)
        print("Usage: openagent project add <path>", file=sys.stderr)
        return 1

    if args.command == "task" and args.task_command == "publish-artifact":
        from openagent_bridge.artifacts import register_artifact
        from openagent_bridge.config import load_config
        from openagent_bridge.db import Database

        config = load_config(args.config)
        db = Database(config.data_dir / "bridge.db")
        try:
            print(register_artifact(db, config, args.task_id, args.file))
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            db.close()
        return 0

    if args.command == "task":
        task_dispatch = {
            "submit": cmd_task_submit,
            "status": cmd_task_status,
            "report": cmd_task_report,
        }
        if args.task_command in task_dispatch:
            return task_dispatch[args.task_command](args)
        print("Usage: openagent task {submit|status|report|publish-artifact}", file=sys.stderr)
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
