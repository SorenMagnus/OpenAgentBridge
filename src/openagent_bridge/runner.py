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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
        Maximum seconds to collect output after termination.
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
        self._timeout_cap = getattr(getattr(config, "runner", None), "default_timeout_seconds", None)
        self._kill_grace = kill_grace_seconds
        self._max_log_bytes = max_log_bytes
        self._redactor = redactor or SecretRedactor()

    # ----- public API -------------------------------------------------------

    def run(self, cmd_spec: "CommandSpec", *, deadline: float | None = None) -> CommandResult:
        """Run *cmd_spec* and return a :class:`CommandResult`.

        The subprocess is **always** launched with ``shell=False``.
        """
        run_id = uuid.uuid4().hex[:12]
        stdout_path = self._artifact_dir / f"{run_id}.stdout.log"
        stderr_path = self._artifact_dir / f"{run_id}.stderr.log"

        env = os.environ.copy()
        if cmd_spec.env_overrides:
            env.update(cmd_spec.env_overrides)

        t0 = time.monotonic()
        timeout = cmd_spec.timeout_seconds
        if self._timeout_cap is not None:
            timeout = min(timeout, self._timeout_cap)
        command_deadline = t0 + timeout
        if deadline is not None:
            command_deadline = min(command_deadline, deadline)

        timed_out = False
        error_message = None
        try:
            result = run_captured(
                cmd_spec.argv, cwd=cmd_spec.cwd, env=env, deadline=command_deadline,
                cleanup_timeout=min(1, max(0, self._kill_grace)),
            )
            exit_code = result.returncode
            raw_stdout, raw_stderr = result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            error_message = "Command deadline reached"
            raw_stdout, raw_stderr = exc.output or b"", exc.stderr or b""
            logger.warning("Command deadline reached for %s", cmd_spec.argv[0])
        except OSError as exc:
            exit_code = 127 if isinstance(exc, FileNotFoundError) else 126
            error_message = str(exc)
            raw_stdout = raw_stderr = b""
            logger.error("Cannot run %s: %s", cmd_spec.argv[0], exc)

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


def run_captured(
    argv: list[str],
    *,
    deadline: float,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    cleanup_timeout: float = 1,
) -> subprocess.CompletedProcess[bytes]:
    """Run an argv in a managed process tree with an absolute monotonic deadline."""
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired(argv, 0)

    if _IS_WINDOWS:
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004  # CREATE_SUSPENDED
        options = {"creationflags": creation_flags}
    else:
        options = {"start_new_session": True}

    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False, **options,
    )
    job = None

    def terminate_tree() -> None:
        if job is not None:
            job.terminate()
        elif _IS_WINDOWS:
            if proc.poll() is None:
                proc.kill()
        else:
            import signal

            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        if _IS_WINDOWS:
            job = _WindowsProcessJob(proc)
        timeout = max(0, deadline - time.monotonic())
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            terminate_tree()
            try:
                exc.output, exc.stderr = proc.communicate(timeout=cleanup_timeout)
            except subprocess.TimeoutExpired:
                pass
            raise
    finally:
        terminate_tree()
        if job is not None:
            job.close()


class _WindowsProcessJob:
    """Contain descendants before the suspended subprocess can start."""

    def __init__(self, proc: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateJobObjectW
        create.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        assign = kernel.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        self._terminate = kernel.TerminateJobObject
        self._terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._terminate.restype = wintypes.BOOL
        self._close = kernel.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL

        self.handle = create(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            process_handle = wintypes.HANDLE(int(proc._handle))
            if not assign(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = wintypes.LONG
            status = resume(process_handle)
            if status < 0:
                raise OSError(f"NtResumeProcess failed (status {status:#x})")
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        if not self._terminate(self.handle, 124):
            logger.error("Could not terminate Windows process job")

    def close(self) -> None:
        self._close(self.handle)
