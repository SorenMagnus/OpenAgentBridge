"""Autonomous bounded repair loop orchestrator."""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.adapters.claude_code import ClaudeCodeAdapter
from openagent_bridge.adapters.codex import CodexAdapter, is_quota_blocked
from openagent_bridge.budget import BudgetLimits, BudgetTracker
from openagent_bridge.db import Database
from openagent_bridge.evaluator import ProgressEvaluator
from openagent_bridge.models import CommandSpec, EvaluationReport, TaskStatus
from openagent_bridge.task_prompt import TaskPromptRenderer
from openagent_bridge.policy import Policy
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner
from openagent_bridge.telemetry import collect_telemetry
from openagent_bridge.verifier import Verifier

logger = logging.getLogger("openagent_bridge.repair")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RepairLoopResult:
    task_id: str
    final_status: str
    iterations_run: int
    overall_pass: bool
    stop_reason: str
    best_head_sha: str
    reports: list[EvaluationReport]


def build_repair_prompt(
    original_goal: str,
    constraints: list[str],
    non_goals: list[str],
    failing_checks: list[str],
    error_summary: str,
    diff_stat: str,
) -> str:
    """Construct an immutable, structured repair prompt for the coding agent."""
    lines = ["You are tasked with fixing test/build failures in this repository."]
    if original_goal:
        lines.append(f"OBJECTIVE (IMMUTABLE): {original_goal}")

    if constraints:
        lines.append("CONSTRAINTS:")
        for c in constraints:
            lines.append(f"  - {c}")

    if non_goals:
        lines.append("NON-GOALS:")
        for ng in non_goals:
            lines.append(f"  - {ng}")

    lines.append("FAILING CHECKS:")
    for fc in failing_checks:
        lines.append(f"  - {fc}")

    if error_summary:
        lines.append("ERROR DETAILS:")
        lines.append(error_summary.strip()[:2000])

    if diff_stat:
        lines.append("CURRENT WORKTREE DIFF STAT:")
        lines.append(diff_stat.strip()[:1000])

    lines.append(
        "INSTRUCTIONS: Make minimal, targeted code changes to resolve the failures above. "
        "Do NOT change the original objective. Do NOT touch forbidden paths (e.g. .env, secrets)."
    )

    return "\n\n".join(lines)


class AutonomousRepairLoop:
    """Executes a bounded, self-correcting repair loop in an isolated task worktree."""

    def __init__(
        self,
        db: Database,
        runner: CommandRunner,
        verifier: Verifier | None = None,
        policy: Policy | None = None,
        budget_limits: BudgetLimits | None = None,
    ) -> None:
        self.db = db
        self.runner = runner
        self.verifier = verifier or Verifier()
        self.policy = policy or Policy()
        self.budget_limits = budget_limits or BudgetLimits()
        self.repo_mgr = RepositoryManager()

    def run_loop(
        self,
        task: dict[str, Any],
        worktree_path: Path,
        adapter: AgentAdapter | None = None,
        tracker: BudgetTracker | None = None,
        contract: str | None = None,
    ) -> RepairLoopResult:
        """Execute bounded autonomous repair loop."""
        worktree_path = Path(worktree_path)
        task_id = str(task["id"])
        # Initialize budget and progress tracker
        tracker = tracker or BudgetTracker.resume(BudgetLimits.for_task(task, self.budget_limits), self.db, task_id)
        contract = contract or TaskPromptRenderer.render(task, tracker.limits, worktree=worktree_path)
        evaluator = ProgressEvaluator(stall_threshold=tracker.limits.stop_after_no_improvement)

        # Select agent adapter
        if adapter is None:
            codex = CodexAdapter()
            claude = ClaudeCodeAdapter()
            if codex.detect():
                adapter = codex
            elif claude.detect():
                adapter = claude
            else:
                adapter = codex  # Fallback to default config

        reports: list[EvaluationReport] = []
        best_head_sha = ""
        best_pass_count = -1
        stop_reason = ""
        final_status = TaskStatus.FAILED.value

        iteration = 0

        def stop_for_budget(reason: str) -> str:
            self.db.transition_task(
                task_id,
                TaskStatus.BUDGET_EXCEEDED.value,
                {"type": "repair.budget_exceeded", "iteration": iteration, "reason": reason},
            )
            return TaskStatus.BUDGET_EXCEEDED.value

        while True:
            if tracker.wall_exceeded or tracker.usage.used_iterations >= tracker.limits.max_iterations:
                _, stop_reason = tracker.is_exceeded()
                stop_reason = f"Budget exceeded: {stop_reason}"
                final_status = stop_for_budget(stop_reason)
                break
            tracker.record_iteration()
            iteration += 1
            tracker.sync_to_db(self.db, task_id)

            logger.info("Task %s starting repair iteration %d", task_id, iteration)

            # 1. Verification run
            self.db.transition_task(
                task_id,
                TaskStatus.VERIFYING.value,
                {"type": "repair.verifying", "iteration": iteration},
            )
            report = self.verifier.run_checks(task, worktree_path, self.runner, deadline=tracker.deadline)
            reports.append(report)

            # Store verification results in DB
            run_id = str(uuid.uuid4())
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
                stop_reason = "Budget exceeded: verification deadline reached"
                final_status = stop_for_budget(stop_reason)
                break

            pass_count = sum(1 for c in report.checks if c.passed)

            # Track best commit
            if pass_count > best_pass_count:
                best_pass_count = pass_count
                best_head_sha = report.head_commit

            # 2. Check success condition
            if report.overall_pass:
                stop_reason = "All acceptance checks passed"
                final_status = TaskStatus.REVIEW_REQUIRED.value
                self.db.transition_task(
                    task_id,
                    final_status,
                    {"type": "repair.succeeded", "iteration": iteration, "reason": stop_reason},
                )
                break

            # 3. Evaluate progress and check stall
            progress = evaluator.record_run(report)
            if progress["is_stalled"]:
                stop_reason = (
                    f"Stalled: identical failure signature repeated "
                    f"{progress['identical_rounds']} consecutive times with no progress"
                )
                final_status = TaskStatus.REVIEW_REQUIRED.value
                self.db.transition_task(
                    task_id,
                    final_status,
                    {"type": "repair.stalled", "iteration": iteration, "reason": stop_reason},
                )
                break

            # 4. Check budget exceeded
            exceeded, budget_reason = tracker.is_exceeded()
            if exceeded:
                stop_reason = f"Budget exceeded: {budget_reason}"
                final_status = stop_for_budget(stop_reason)
                break

            try:
                current_diff = self.repo_mgr.get_diff(
                    worktree_path, deadline=tracker.deadline,
                    base_commit=task.get("base_commit") or "", include_worktree=True,
                )
            except subprocess.TimeoutExpired:
                stop_reason = "Budget exceeded: diff deadline reached"
                final_status = stop_for_budget(stop_reason)
                break

            # 5. Build bounded repair prompt and invoke Agent
            self.db.transition_task(
                task_id,
                TaskStatus.REPAIR_PLANNED.value,
                {"type": "repair.planned", "iteration": iteration},
            )

            # Collect error snippets from failing checks
            error_snippets = []
            for c in report.checks:
                if not c.passed and c.stderr_artifact and c.stderr_artifact.exists():
                    try:
                        error_snippets.append(c.stderr_artifact.read_text(encoding="utf-8")[:500])
                    except Exception:
                        pass

            repair_prompt = build_repair_prompt(
                original_goal="",
                constraints=[],
                non_goals=[],
                failing_checks=report.critical_failures,
                error_summary="\n---\n".join(error_snippets),
                diff_stat=current_diff.stat,
            )

            self.db.transition_task(
                task_id,
                TaskStatus.RUNNING.value,
                {"type": "repair.agent_running", "iteration": iteration},
            )
            if isinstance(adapter, AgentAdapter):
                adapter.execution_deadline = tracker.deadline
            cmd_spec = adapter.build_command(
                prompt=contract + "\n\n" + repair_prompt,
                worktree=worktree_path,
                sandbox_mode="workspace-write",
            )
            if tracker.wall_exceeded:
                stop_reason = "Budget exceeded: agent deadline reached"
                final_status = stop_for_budget(stop_reason)
                break
            tracker.record_invocation()
            tracker.sync_to_db(self.db, task_id)
            agent_result = self.runner.run(cmd_spec, deadline=tracker.deadline)

            self.db.insert_run({
                "id": run_id,
                "task_id": task_id,
                "iteration": iteration,
                "agent": type(adapter).__name__,
                "started_at": _utcnow_iso(),
                "ended_at": _utcnow_iso(),
                "exit_code": agent_result.exit_code,
                "stdout_path": str(agent_result.stdout_path),
                "stderr_path": str(agent_result.stderr_path),
                **collect_telemetry(type(adapter).__name__, agent_result.stdout_path),
            })

            tracker.sync_to_db(self.db, task_id)
            if agent_result.timed_out:
                stop_reason = "Agent invocation timed out"
                final_status = TaskStatus.BUDGET_EXCEEDED.value
                self.db.transition_task(task_id, final_status, {"type": "agent.timeout", "run_id": run_id})
                break

            # Check for quota exhaustion / account limits
            combined_output = ""
            if agent_result.stdout_path and agent_result.stdout_path.exists():
                try:
                    combined_output += agent_result.stdout_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            if agent_result.stderr_path and agent_result.stderr_path.exists():
                try:
                    combined_output += "\n" + agent_result.stderr_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

            if is_quota_blocked(combined_output):
                stop_reason = "Codex quota blocked: account usage limit reached"
                final_status = TaskStatus.BLOCKED.value
                self.db.transition_task(
                    task_id,
                    final_status,
                    {
                        "type": "agent.quota_blocked",
                        "iteration": iteration,
                        "reason": stop_reason,
                    },
                )
                logger.warning("Task %s stopped due to agent quota block: %s", task_id, stop_reason)
                break

            # Commit agent changes within the same subprocess deadline.
            for argv in (
                ["git", "add", "-A"],
                ["git", "commit", "-m", f"openagent auto-repair iteration {iteration}"],
            ):
                git_result = self.runner.run(CommandSpec(
                    argv=argv, cwd=worktree_path, timeout_seconds=60,
                ), deadline=tracker.deadline)
                if git_result.exit_code != 0:
                    break

        tracker.sync_to_db(self.db, task_id)
        return RepairLoopResult(
            task_id=task_id,
            final_status=final_status,
            iterations_run=iteration,
            overall_pass=reports[-1].overall_pass if reports else False,
            stop_reason=stop_reason,
            best_head_sha=best_head_sha,
            reports=reports,
        )
