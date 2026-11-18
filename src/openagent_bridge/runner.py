"""CommandRunner — subprocess execution with timeout, redaction and artifact logging.

All commands are run with ``shell=False`` to avoid shell-injection.  stdout
and stderr are streamed to artifact files on disk; only summaries and paths
are stored in the database.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from openagent_bridge.logging_utils import SecretRedactor

if TYPE_CHECKING:
    from openagent_bridge.models import CommandSpec

logger = logging.getLogger("openagent_bridge.runner")

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Result dataclass (returned by CommandRunner.run)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a single subprocess invocation."""

    exit_code: int
    stdout_path: Path
    stderr_path: Path
    duration_ms: int
    timed_out: bool
    error_message: str | None = None


# ---------------------------------------------------------------------------
# CommandRunner
# ---------------------------------------------------------------------------

# Maximum bytes we write to an artifact log before truncating.
_MAX_LOG_BYTES = 50 * 1024 * 1024  # 50 MiB default


class CommandRunner:
    """Execute a :class:`CommandSpec` as a managed subprocess.

    Parameters
    ----------
    artifact_dir:
        Root directory for stdout/stderr log files.
    kill_grace_seconds:
        Seconds between SIGTERM/terminate and force-kill.
    max_log_bytes:
        Truncate stdout/stderr artifact files at this size.
    redactor:
        Optional :class:`SecretRedactor` applied to logged output.
    """

    def __init__(
        self,
        artifact_dir: Path | str | None = None,
        *,
        config: Any = None,
        kill_grace_seconds: float = 5.0,
        max_log_bytes: int = _MAX_LOG_BYTES,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if artifact_dir is not None:
            self._artifact_dir = Path(artifact_dir)
        elif config is not None and hasattr(config, "data_dir"):
            self._artifact_dir = Path(config.data_dir) / "artifacts"
            if hasattr(config, "runner"):
                kill_grace_seconds = getattr(config.runner, "kill_grace_seconds", kill_grace_seconds)
                max_log_bytes = getattr(config.runner, "max_log_bytes", max_log_bytes)
        else:
            self._artifact_dir = Path.home() / ".openagent" / "artifacts"

        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._kill_grace = kill_grace_seconds
        self._max_log_bytes = max_log_bytes
        self._redactor = redactor or SecretRedactor()

    # ----- public API -------------------------------------------------------

    def run(self, cmd_spec: "CommandSpec") -> CommandResult:
        """Run *cmd_spec* and return a :class:`CommandResult`.

        The subprocess is **always** launched with ``shell=False``.
        """
        run_id = uuid.uuid4().hex[:12]
        stdout_path = self._artifact_dir / f"{run_id}.stdout.log"
        stderr_path = self._artifact_dir / f"{run_id}.stderr.log"

        env = os.environ.copy()
        if cmd_spec.env_overrides:
            env.update(cmd_spec.env_overrides)

        # Build creation flags per platform.
        kwargs: dict = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True  # creates its own process group

        t0 = time.monotonic()
        timed_out = False
        error_message: str | None = None

        try:
            proc = subprocess.Popen(
                cmd_spec.argv,
                cwd=str(cmd_spec.cwd) if cmd_spec.cwd else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                **kwargs,
            )
        except FileNotFoundError as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            error_message = f"Command not found: {exc}"
            logger.error(error_message)
            self._write_empty(stdout_path, stderr_path)
            return CommandResult(
                exit_code=127,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_ms=duration_ms,
                timed_out=False,
                error_message=error_message,
            )
        except PermissionError as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            error_message = f"Permission denied: {exc}"
            logger.error(error_message)
            self._write_empty(stdout_path, stderr_path)
            return CommandResult(
                exit_code=126,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_ms=duration_ms,
                timed_out=False,
                error_message=error_message,
            )

        try:
            raw_stdout, raw_stderr = proc.communicate(
                timeout=cmd_spec.timeout_seconds
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning(
                "Timeout (%ds) expired for %s — terminating process tree",
                cmd_spec.timeout_seconds,
                cmd_spec.argv,
            )
            self._kill_tree(proc)
            raw_stdout, raw_stderr = proc.communicate()

        duration_ms = int((time.monotonic() - t0) * 1000)

        # Decode safely
        stdout_text = self._safe_decode(raw_stdout)
        stderr_text = self._safe_decode(raw_stderr)

        # Redact secrets
        stdout_text = self._redactor.redact(stdout_text)
        stderr_text = self._redactor.redact(stderr_text)

        # Write artifact files (truncated if necessary)
        self._write_artifact(stdout_path, stdout_text)
        self._write_artifact(stderr_path, stderr_text)

        exit_code = proc.returncode

        if exit_code != 0 and not timed_out:
            logger.info(
                "Command %s exited with code %d", cmd_spec.argv, exit_code
            )

        return CommandResult(
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            duration_ms=duration_ms,
            timed_out=timed_out,
            error_message=error_message,
        )

    # ----- process-tree kill ------------------------------------------------

    def _kill_tree(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """Graceful terminate → wait → force kill of the entire process tree."""
        try:
            if _IS_WINDOWS:
                self._kill_tree_windows(proc)
            else:
                self._kill_tree_posix(proc)
        except Exception:
            logger.exception("Error killing process tree for PID %s", proc.pid)

    def _kill_tree_posix(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """POSIX: send SIGTERM to process group, then SIGKILL after grace."""
        import signal

        pgid = os.getpgid(proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            logger.warning("Grace period expired — force-killing pgid %d", pgid)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _kill_tree_windows(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """Windows: terminate then taskkill /T /F to kill the tree."""
        proc.terminate()
        try:
            proc.wait(timeout=self._kill_grace)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Grace period expired — force-killing tree for PID %d",
                proc.pid,
            )
            # taskkill /T kills the process and its children.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    shell=False,
                )
            except FileNotFoundError:
                # Fallback: just kill the main process.
                proc.kill()

    # ----- helpers ----------------------------------------------------------

    @staticmethod
    def _safe_decode(data: bytes) -> str:
        """Decode bytes, replacing invalid sequences rather than crashing."""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    def _write_artifact(self, path: Path, text: str) -> None:
        """Write *text* to *path*, truncating at max_log_bytes."""
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_log_bytes:
            encoded = encoded[: self._max_log_bytes]
            encoded += b"\n\n[TRUNCATED - exceeded max log size]\n"
            logger.warning(
                "Output truncated to %d bytes: %s", self._max_log_bytes, path
            )
        path.write_bytes(encoded)

    @staticmethod
    def _write_empty(stdout_path: Path, stderr_path: Path) -> None:
        """Create empty artifact files for error-before-launch scenarios."""
        stdout_path.touch()
        stderr_path.touch()
