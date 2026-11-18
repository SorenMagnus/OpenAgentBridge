"""Deterministic verification engine for OpenAgent Bridge.

Runs acceptance commands, checks forbidden path modifications, and enforces that
agent text claims cannot override actual test outcomes.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openagent_bridge.models import (
    CommandSpec,
    EvaluationReport,
    VerificationResult,
)
from openagent_bridge.policy import Policy
from openagent_bridge.repository import RepositoryManager
from openagent_bridge.runner import CommandRunner


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Verifier:
    """Runs deterministic checks against a task worktree."""

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()
        self.repo_mgr = RepositoryManager()

    def run_checks(
        self,
        task: dict[str, Any],
        worktree_path: Path,
        runner: CommandRunner,
    ) -> EvaluationReport:
        """Run all acceptance criteria checks in the task worktree.

        Args:
            task: Task dictionary containing acceptance rules, constraints, etc.
            worktree_path: Absolute path to the git worktree to verify.
            runner: CommandRunner instance to execute check commands.

        Returns:
            EvaluationReport containing check outcomes and overall pass/fail status.
        """
        worktree_path = Path(worktree_path)
        task_id = str(task.get("id") or task.get("task_id", ""))
        run_id = str(task.get("current_run_id", ""))

        # 1. Fetch current diff and check for forbidden paths
        diff_info = self.repo_mgr.get_diff(worktree_path)
        base_commit = diff_info.base_sha
        head_commit = diff_info.head_sha
        changed_files = diff_info.changed_files

        acceptance = task.get("acceptance", {})
        if isinstance(acceptance, str):
            import json
            try:
                acceptance = json.loads(acceptance)
            except Exception:
                acceptance = {}

        if isinstance(acceptance, list):
            normalized_cmds = []
            for item in acceptance:
                if isinstance(item, dict):
                    cmd_val = item.get("command") or item.get("argv")
                    if cmd_val:
                        normalized_cmds.append(cmd_val)
                    else:
                        normalized_cmds.append(item)
                else:
                    normalized_cmds.append(item)
            acceptance = {"commands": normalized_cmds}
        elif not isinstance(acceptance, dict):
            acceptance = {}

        forbidden_patterns = acceptance.get("forbidden_paths", [])
        forbidden_violations = self.policy.check_forbidden_paths(changed_files, forbidden_patterns)

        checks: list[VerificationResult] = []
        critical_failures: list[str] = []

        # Record forbidden path check
        if forbidden_violations:
            critical_failures.append("forbidden_paths")
            checks.append(
                VerificationResult(
                    check_name="forbidden_paths",
                    kind="security_policy",
                    argv=[],
                    start_time=_utcnow(),
                    end_time=_utcnow(),
                    duration_ms=0,
                    exit_code=1,
                    passed=False,
                    stdout_artifact=None,
                    stderr_artifact=None,
                    metric={"violations": forbidden_violations},
                )
            )
        else:
            checks.append(
                VerificationResult(
                    check_name="forbidden_paths",
                    kind="security_policy",
                    argv=[],
                    start_time=_utcnow(),
                    end_time=_utcnow(),
                    duration_ms=0,
                    exit_code=0,
                    passed=True,
                    stdout_artifact=None,
                    stderr_artifact=None,
                    metric={"violations": []},
                )
            )

        # 2. Check required files if specified
        required_files = acceptance.get("required_files", [])
        missing_files = []
        for req_file in required_files:
            target = worktree_path / req_file
            if not target.exists():
                missing_files.append(req_file)

        if required_files:
            passed = len(missing_files) == 0
            if not passed:
                critical_failures.append("required_files")
            checks.append(
                VerificationResult(
                    check_name="required_files",
                    kind="filesystem_check",
                    argv=[],
                    start_time=_utcnow(),
                    end_time=_utcnow(),
                    duration_ms=0,
                    exit_code=0 if passed else 1,
                    passed=passed,
                    stdout_artifact=None,
                    stderr_artifact=None,
                    metric={"missing": missing_files},
                )
            )

        # 3. Run acceptance commands
        commands = acceptance.get("commands", [])
        for idx, cmd in enumerate(commands):
            # cmd can be a list of strings (argv)
            if isinstance(cmd, str):
                import shlex
                import sys
                argv = shlex.split(cmd, posix=(sys.platform != "win32"))
            else:
                argv = list(cmd)

            if not argv:
                continue

            # Check for dangerous commands in acceptance specs
            if self.policy.check_dangerous_commands(argv):
                critical_failures.append(f"command_{idx}_security_violation")
                checks.append(
                    VerificationResult(
                        check_name=f"command_{idx}",
                        kind="command",
                        argv=argv,
                        start_time=_utcnow(),
                        end_time=_utcnow(),
                        duration_ms=0,
                        exit_code=1,
                        passed=False,
                        metric={"error": "Command blocked by security policy"},
                    )
                )
                continue

            check_name = f"command_{idx}_{argv[0]}"
            start_dt = _utcnow()
            start_mono = time.monotonic()

            spec = CommandSpec(
                argv=argv,
                cwd=worktree_path,
                timeout_seconds=300,
                capture_output=True,
            )

            result = runner.run(spec)
            end_dt = _utcnow()
            duration_ms = int((time.monotonic() - start_mono) * 1000)

            passed = (result.exit_code == 0) and not result.timed_out
            if not passed:
                critical_failures.append(check_name)

            checks.append(
                VerificationResult(
                    check_name=check_name,
                    kind="command",
                    argv=argv,
                    start_time=start_dt,
                    end_time=end_dt,
                    duration_ms=duration_ms,
                    exit_code=result.exit_code,
                    passed=passed,
                    stdout_artifact=result.stdout_path,
                    stderr_artifact=result.stderr_path,
                    metric={"timed_out": result.timed_out},
                )
            )

        overall_pass = len(critical_failures) == 0
        suggested_next_action = "review" if overall_pass else "repair"

        return EvaluationReport(
            task_id=task_id,
            run_id=run_id,
            base_commit=base_commit,
            head_commit=head_commit,
            checks=checks,
            critical_failures=critical_failures,
            overall_pass=overall_pass,
            suggested_next_action=suggested_next_action,
        )
