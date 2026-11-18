"""Single background worker loop with heartbeat leasing and crash recovery."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.adapters.claude_code import ClaudeCodeAdapter
from openagent_bridge.adapters.codex import CodexAdapter
from openagent_bridge.config import BridgeConfig
from openagent_bridge.db import Database
from openagent_bridge.models import CommandSpec, TaskStatus
from openagent_bridge.queue import TaskQueue
from openagent_bridge.recovery import TaskRecovery
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner
from openagent_bridge.verifier import Verifier

logger = logging.getLogger("openagent_bridge.worker")


class Worker:
    """Executes claimed tasks with continuous heartbeat leasing."""

    def __init__(
        self,
        config: BridgeConfig,
        worker_id: str | None = None,
        db: Database | None = None,
    ) -> None:
        self.config = config
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.db = db or Database(config.data_dir / "bridge.db")
        self.queue = TaskQueue(self.db)
        self.recovery = TaskRecovery(self.db, config.data_dir)
        self.repo_mgr = RepositoryManager()
        self.runner = CommandRunner(config=config)
        self.verifier = Verifier()
        self._stop_requested = False

    def is_kill_switched(self) -> bool:
        """Check if STOP file exists in data_dir."""
        stop_file = self.config.data_dir / "STOP"
        return stop_file.exists()

    def run_once(self) -> dict[str, Any] | None:
        """Attempt to claim and process exactly one task from the queue."""
        if self.is_kill_switched():
            logger.info("Kill switch active — skipping task claim.")
            return None

        # Check and recover any expired tasks first
        self.recovery.recover_all()

        lease_seconds = self.config.worker.lease_seconds
        task = self.queue.claim(self.worker_id, lease_seconds=lease_seconds)
        if not task:
            return None

        task_id = task["id"]
        logger.info("Worker %s claimed task %s", self.worker_id, task_id)

        # Start background heartbeat
        heartbeat_stop = threading.Event()
        heartbeat_interval = self.config.worker.heartbeat_seconds

        def _heartbeat_loop():
            while not heartbeat_stop.wait(timeout=heartbeat_interval):
                renewed = self.queue.heartbeat(
                    task_id, self.worker_id, lease_seconds=lease_seconds
                )
                if not renewed:
                    logger.warning("Heartbeat renewal failed for task %s", task_id)
                    break

        hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        hb_thread.start()

        try:
            result = self._execute_task(task)
            return result
        finally:
            heartbeat_stop.set()
            hb_thread.join(timeout=2.0)
            self.queue.release(task_id, self.worker_id)

    def _execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Run execution lifecycle for a claimed task."""
        task_id = task["id"]
        worktree_path_str = task.get("worktree_path")
        worktree_path = Path(worktree_path_str) if worktree_path_str else None

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
        agent_name = "unknown"

        if codex.detect():
            adapter = codex
            agent_name = "codex"
        elif claude.detect():
            adapter = claude
            agent_name = "claude-code"

        run_id = str(uuid.uuid4())
        exit_code = 0
        stdout_path = None
        stderr_path = None

        if adapter and worktree_path and worktree_path.exists():
            cmd_spec = adapter.build_command(
                prompt=task.get("goal", ""),
                worktree=worktree_path,
                sandbox_mode="workspace-write",
            )
            run_res = self.runner.run(cmd_spec)
            exit_code = run_res.exit_code
            stdout_path = run_res.stdout_path
            stderr_path = run_res.stderr_path

        self.db.insert_run({
            "id": run_id,
            "task_id": task_id,
            "iteration": task.get("iteration", 0) + 1,
            "agent": agent_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": exit_code,
            "stdout_path": str(stdout_path) if stdout_path else "",
            "stderr_path": str(stderr_path) if stderr_path else "",
        })

        # Verification
        self.db.transition_task(
            task_id,
            TaskStatus.VERIFYING.value,
            {"type": "task.verifying", "run_id": run_id},
        )

        report = None
        if worktree_path and worktree_path.exists():
            report = self.verifier.run_checks(task, worktree_path, self.runner)
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

        overall_pass = report.overall_pass if report else False
        if overall_pass:
            final_status = TaskStatus.REVIEW_REQUIRED.value
            self.db.transition_task(
                task_id,
                final_status,
                {"type": "task.review_required", "run_id": run_id},
            )
        else:
            final_status = TaskStatus.FAILED.value
            self.db.transition_task(
                task_id,
                final_status,
                {"type": "task.failed", "reason": "verification_failed"},
            )

        return {"task_id": task_id, "status": final_status, "overall_pass": overall_pass}

    def run_forever(self, poll_interval: float = 2.0) -> None:
        """Run worker loop indefinitely until stopped."""
        logger.info("Starting OpenAgent worker %s", self.worker_id)
        while not self._stop_requested:
            try:
                processed = self.run_once()
                if not processed:
                    time.sleep(poll_interval)
            except Exception as exc:
                logger.error("Error in worker loop: %s", exc, exc_info=True)
                time.sleep(poll_interval)

    def stop(self) -> None:
        """Signal worker to stop gracefully."""
        self._stop_requested = True
