"""Single background worker loop with heartbeat leasing and crash recovery."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.adapters.claude_code import ClaudeCodeAdapter
from openagent_bridge.adapters.codex import CodexAdapter, is_quota_blocked
from openagent_bridge.artifacts import publish_required_artifacts
from openagent_bridge.budget import BudgetLimits, BudgetTracker
from openagent_bridge.task_prompt import TaskPromptRenderer
from openagent_bridge.config import BridgeConfig
from openagent_bridge.db import Database
from openagent_bridge.models import TaskStatus
from openagent_bridge.queue import TaskQueue
from openagent_bridge.recovery import RECOVERABLE_STATUSES, TaskRecovery
from openagent_bridge.repair import AutonomousRepairLoop
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner
from openagent_bridge.telemetry import collect_telemetry
from openagent_bridge.verifier import Verifier

logger = logging.getLogger("openagent_bridge.worker")


class Worker:
    """Executes claimed tasks with continuous heartbeat leasing."""

    def __init__(
        self,
        config: BridgeConfig,
        worker_id: str | None = None,
        db: Database | None = None,
        router: Any = None,
    ) -> None:
        self.config = config
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.db = db or Database(config.data_dir / "bridge.db")
        self.queue = TaskQueue(self.db)
        self.recovery = TaskRecovery(self.db, config.data_dir)
        self.repo_mgr = RepositoryManager()
        self.runner = CommandRunner(config=config)
        self.verifier = Verifier()
        self.router = router
        self._stop_requested = threading.Event()
        self._artifact_backfill_done = False

    def is_kill_switched(self) -> bool:
        """Check if STOP file exists in data_dir."""
        stop_file = self.config.data_dir / "STOP"
        return stop_file.exists()

    def run_once(self) -> dict[str, Any] | None:
        """Attempt to claim and process exactly one task from the queue."""
        if self._stop_requested.is_set():
            return None
        if not self._artifact_backfill_done:
            task_ids = self.db.connection.execute(
                "SELECT id FROM tasks WHERE status = ?", (TaskStatus.REVIEW_REQUIRED.value,),
            ).fetchall()
            for row in task_ids:
                publish_required_artifacts(self.db, self.config, self.db.get_task(row["id"]))
            self._artifact_backfill_done = True
        if self.is_kill_switched():
            logger.info("Kill switch active — skipping task claim.")
            return None

        # Check and recover any expired tasks first
        self.recovery.recover_all()
        if self._stop_requested.is_set():
            return None

        lease_seconds = self.config.worker.lease_seconds
        owner = f'{self.worker_id}:{uuid.uuid4().hex}'
        task = self.queue.claim(owner, lease_seconds=lease_seconds)
        if not task:
            return None

        task_id = task["id"]
        logger.info("Worker %s claimed task %s", self.worker_id, task_id)

        # Start background heartbeat
        heartbeat_stop = threading.Event()
        heartbeat_interval = self.config.worker.heartbeat_seconds

        def _heartbeat_loop():
            heartbeat_db = Database(self.db.db_path)
            try:
                queue = TaskQueue(heartbeat_db)
                while not heartbeat_stop.wait(timeout=heartbeat_interval):
                    try:
                        renewed = queue.heartbeat(task_id, owner, lease_seconds=lease_seconds)
                    except sqlite3.OperationalError:
                        logger.warning("Heartbeat database busy for task %s", task_id, exc_info=True)
                        continue
                    if not renewed:
                        logger.warning("Lease expired or released for task %s", task_id)
                        break
            finally:
                heartbeat_db.close()

        hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        hb_thread.start()

        try:
            with self.db.guard_worker_lease(task_id, owner):
                return self._execute_task(task)
        finally:
            heartbeat_stop.set()
            hb_thread.join()
            current = self.db.get_task(task_id)
            if current and current["status"] not in RECOVERABLE_STATUSES:
                self.queue.release(task_id, owner)

    def _execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Run execution lifecycle for a claimed task."""
        task_id = task["id"]
        limits = self._repair_budget_limits(task)
        tracker = BudgetTracker.resume(limits, self.db, task_id)

        def stop_for_budget(reason: str) -> dict[str, Any]:
            tracker.sync_to_db(self.db, task_id)
            self.db.transition_task(
                task_id, TaskStatus.BUDGET_EXCEEDED.value,
                {"type": "task.budget_exceeded", "reason": reason},
            )
            return {"task_id": task_id, "status": TaskStatus.BUDGET_EXCEEDED.value, "reason": reason}

        exceeded, reason = tracker.is_exceeded()
        if exceeded:
            return stop_for_budget(reason)

        worktree_path_str = task.get("worktree_path")
        worktree_path = Path(worktree_path_str) if worktree_path_str else None
        project = self.db.get_project(task.get("project_id", ""))
        repo_root = Path(project["path"]) if project and project.get("path") else None

        # Automatically initialize worktree if missing (e.g. tasks submitted via HTTP / Bot)
        if not worktree_path or not worktree_path.exists():
            if repo_root is not None:
                try:
                    repo_info = self.repo_mgr.check_repo(repo_root, deadline=tracker.deadline)
                    worktree_path = self.repo_mgr.create_worktree(
                        repo_root=repo_root,
                        task_id=task_id,
                        data_dir=self.config.data_dir,
                        deadline=tracker.deadline,
                    )
                    self.db.connection.execute(
                        "UPDATE tasks SET worktree_path = ?, branch = ?, base_commit = ? WHERE id = ?",
                        (str(worktree_path), f"openagent/{task_id}", repo_info.head_sha, task_id),
                    )
                    self.db.connection.commit()
                    task = self.db.get_task(task_id) or task
                    logger.info("Initialized worktree for task %s at %s", task_id, worktree_path)
                except subprocess.TimeoutExpired:
                    return stop_for_budget("Worktree preparation deadline reached")
                except Exception as exc:
                    logger.exception("Failed to initialize worktree for task %s: %s", task_id, exc)
                    self.db.transition_task(
                        task_id,
                        TaskStatus.BLOCKED.value,
                        {"type": "task.blocked", "reason": f"Worktree initialization failed: {exc}"},
                    )
                    return {"task_id": task_id, "status": "BLOCKED", "reason": str(exc)}

        # Transition to RUNNING
        self.db.transition_task(
            task_id,
            TaskStatus.RUNNING.value,
            {"type": "task.running", "worker_id": self.worker_id},
        )

        # Check for active kill switch
        if self.is_kill_switched():
            self.db.transition_task(
                task_id,
                TaskStatus.BLOCKED.value,
                {"type": "task.paused", "reason": "kill_switch_triggered"},
            )
            return {"task_id": task_id, "status": "BLOCKED"}

        # Run agent
        codex = CodexAdapter()
        claude = ClaudeCodeAdapter()
        adapter = None

        if codex.detect():
            adapter = codex
            agent_name = "codex"
        elif claude.detect():
            adapter = claude
            agent_name = "claude-code"

        if adapter is None or worktree_path is None or not worktree_path.is_dir():
            reason = "No agent available" if adapter is None else "Task worktree unavailable"
            self.db.transition_task(task_id, TaskStatus.FAILED.value, {"type": "task.failed", "reason": reason})
            return {"task_id": task_id, "status": TaskStatus.FAILED.value, "overall_pass": False}

        run_id = str(uuid.uuid4())
        if isinstance(adapter, AgentAdapter):
            adapter.execution_deadline = tracker.deadline
            adapter.probe_timeout_limit = self.config.runner.default_timeout_seconds
        contract = TaskPromptRenderer.for_execution(
            self.db.get_task(task_id) or task, limits,
            data_dir=self.config.data_dir, repo_path=repo_root, worktree=worktree_path,
            policy=asdict(self.config.policy),
        )
        cmd_spec = adapter.build_command(
            prompt=contract,
            worktree=worktree_path,
            sandbox_mode="workspace-write",
        )
        if tracker.wall_exceeded:
            return stop_for_budget("Agent deadline reached")
        tracker.record_invocation()
        tracker.sync_to_db(self.db, task_id)
        run_res = self.runner.run(cmd_spec, deadline=tracker.deadline)
        stdout_path = run_res.stdout_path
        stderr_path = run_res.stderr_path

        self.db.insert_run({
            "id": run_id,
            "task_id": task_id,
            "iteration": task.get("iteration", 0) + 1,
            "agent": agent_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": run_res.exit_code,
            "stdout_path": str(stdout_path) if stdout_path else "",
            "stderr_path": str(stderr_path) if stderr_path else "",
            **collect_telemetry(agent_name, stdout_path),
        })

        tracker.sync_to_db(self.db, task_id)
        if run_res.timed_out:
            return stop_for_budget("Agent deadline reached")

        # Detect quota blockage
        combined_output = ""
        if stdout_path and stdout_path.exists():
            try:
                combined_output += stdout_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if stderr_path and stderr_path.exists():
            try:
                combined_output += "\n" + stderr_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        if is_quota_blocked(combined_output):
            self.db.transition_task(
                task_id,
                TaskStatus.BLOCKED.value,
                {"type": "task.blocked", "reason": "Codex quota blocked: account usage limit reached"},
            )
            return {"task_id": task_id, "status": "BLOCKED", "reason": "quota_blocked"}

        # A one-iteration task can verify the initial result and has no repair round.
        if self.config.autonomy.enabled and tracker.limits.max_iterations > 1:
            return self._execute_with_repair_loop(task, worktree_path, adapter, tracker, contract)

        # Verification
        self.db.transition_task(
            task_id,
            TaskStatus.VERIFYING.value,
            {"type": "task.verifying", "run_id": run_id},
        )

        report = None
        if worktree_path and worktree_path.exists():
            if tracker.wall_exceeded:
                return stop_for_budget("Verification deadline reached")
            tracker.record_iteration()
            tracker.sync_to_db(self.db, task_id)
            report = self.verifier.run_checks(task, worktree_path, self.runner, deadline=tracker.deadline)
            for check in report.checks:
                self.db.insert_verification_result({
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

        tracker.sync_to_db(self.db, task_id)
        if tracker.wall_exceeded:
            return stop_for_budget("Verification deadline reached")
        overall_pass = report.overall_pass if report else False
        if overall_pass:
            final_status = TaskStatus.REVIEW_REQUIRED.value
            self.db.transition_task(
                task_id,
                final_status,
                {"type": "task.review_required", "run_id": run_id},
            )
            publish_required_artifacts(self.db, self.config, self.db.get_task(task_id))
        else:
            final_status = TaskStatus.FAILED.value
            self.db.transition_task(
                task_id,
                final_status,
                {"type": "task.failed", "reason": "verification_failed"},
            )

        if self.router:
            updated_task = self.db.get_task(task_id) or task
            self.router.dispatch_task_update(
                updated_task,
                f"Task ended with status {final_status} (Overall pass: {overall_pass})"
            )

        return {"task_id": task_id, "status": final_status, "overall_pass": overall_pass}

    def _repair_budget_limits(self, task: dict[str, Any]) -> BudgetLimits:
        """Derive BudgetLimits from task's budget field and config defaults."""
        return BudgetLimits.for_task(task, self.config.autonomy)

    def _execute_with_repair_loop(
        self,
        task: dict[str, Any],
        worktree_path: Path,
        adapter: Any,
        tracker: BudgetTracker,
        contract: str,
    ) -> dict[str, Any]:
        """Execute autonomous repair loop for the task."""
        task_id = str(task["id"])

        try:
            loop = AutonomousRepairLoop(
                db=self.db,
                runner=self.runner,
                verifier=self.verifier,
                budget_limits=tracker.limits,
            )
            result = loop.run_loop(
                task=task,
                worktree_path=worktree_path,
                adapter=adapter,
                tracker=tracker,
                contract=contract,
            )
            if result.final_status == TaskStatus.REVIEW_REQUIRED.value:
                publish_required_artifacts(self.db, self.config, self.db.get_task(task_id))

            if self.router:
                updated_task = self.db.get_task(task_id) or task
                self.router.dispatch_task_update(
                    updated_task,
                    f"Task ended with status {result.final_status} (Overall pass: {result.overall_pass})",
                )

            return {
                "task_id": task_id,
                "status": result.final_status,
                "overall_pass": result.overall_pass,
                "iterations_run": result.iterations_run,
                "stop_reason": result.stop_reason,
            }
        except Exception as exc:
            logger.error("Error in repair loop for task %s: %s", task_id, exc, exc_info=True)
            try:
                self.db.transition_task(
                    task_id,
                    TaskStatus.FAILED.value,
                    {"type": "task.failed", "reason": f"repair_loop_error: {exc}"},
                )
            except Exception:
                pass
            return {
                "task_id": task_id,
                "status": TaskStatus.FAILED.value,
                "overall_pass": False,
                "error": str(exc),
            }

    def run_forever(self, poll_interval: float = 2.0) -> None:
        """Run worker loop indefinitely until stopped."""
        logger.info("Starting OpenAgent worker %s", self.worker_id)
        while not self._stop_requested.is_set():
            try:
                processed = self.run_once()
                if not processed:
                    self._stop_requested.wait(poll_interval)
            except Exception as exc:
                logger.error("Error in worker loop: %s", exc, exc_info=True)
                self._stop_requested.wait(poll_interval)

    def stop(self) -> None:
        """Signal worker to stop gracefully."""
        self._stop_requested.set()
