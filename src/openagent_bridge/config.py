"""Configuration loading for OpenAgent Bridge.

Configuration is read from a YAML file and exposed as nested dataclasses.
Every path is resolved through ``pathlib.Path`` so spaces in directory names
are handled transparently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# PyYAML is an optional dependency — we try to import it and fall back to a
# stub that raises a clear error at load-time so the rest of the package can
# still be imported without it.
try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Sub-config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WorkerConfig:
    """Controls the task-worker pool."""

    concurrency: int = 1
    heartbeat_seconds: int = 10
    lease_seconds: int = 60


@dataclass
class RunnerConfig:
    """Defaults for subprocess execution."""

    default_timeout_seconds: int = 900
    kill_grace_seconds: int = 5
    max_log_bytes: int = 10_000_000  # 10 MiB


@dataclass
class AutonomyConfig:
    """Guardrails on how far the system may go without human input."""

    enabled: bool = False
    max_iterations: int = 5
    max_wall_minutes: int = 120
    max_agent_invocations: int = 8
    stop_after_no_improvement: int = 2


@dataclass
class PolicyConfig:
    """High-level permission toggles."""

    merge_main: str = "require_approval"
    deploy: str = "forbidden"
    network: str = "deny_by_default"
    dirty_worktree: str = "block"


@dataclass
class WatcherConfig:
    """File-system watcher settings."""

    enabled: bool = False
    debounce_ms: int = 1000


@dataclass
class AgentConfig:
    """Which AI agent(s) to use for execution."""

    preferred: str = "codex"
    fallback: str = "claude-code"


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

@dataclass
class BridgeConfig:
    """Top-level configuration for OpenAgent Bridge.

    Instantiate directly for defaults, or use :func:`load_config` to populate
    from a YAML file on disk.
    """

    data_dir: Path = field(
        default_factory=lambda: Path.home() / ".openagent"
    )
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sub(cls: type, raw: dict[str, Any] | None) -> Any:
    """Instantiate a sub-config dataclass from a dict, ignoring unknown keys."""
    if not raw:
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in raw.items() if k in known})


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load a :class:`BridgeConfig` from a YAML file.

    Parameters
    ----------
    path:
        Path to the YAML configuration file.  If *None*, falls back to the
        ``OPENAGENT_CONFIG`` environment variable and then to
        ``~/.openagent/config.yaml``.

    Returns
    -------
    BridgeConfig
        A fully populated configuration object (missing keys use defaults).

    Raises
    ------
    FileNotFoundError
        If the resolved path does not exist.
    RuntimeError
        If PyYAML is not installed.
    """
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required for config loading.  Install it with: "
            "pip install pyyaml"
        )

    if path is None:
        env = os.environ.get("OPENAGENT_CONFIG")
        if env:
            path = Path(env)
            if not path.exists():
                raise FileNotFoundError(f"Config file specified in OPENAGENT_CONFIG not found: {path}")
        else:
            default_path = Path.home() / ".openagent" / "config.yaml"
            if not default_path.exists():
                return BridgeConfig()
            path = default_path
    else:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    data_dir_raw = raw.get("data_dir")
    data_dir = Path(data_dir_raw).expanduser() if data_dir_raw else BridgeConfig().data_dir

    return BridgeConfig(
        data_dir=data_dir,
        worker=_build_sub(WorkerConfig, raw.get("worker")),
        runner=_build_sub(RunnerConfig, raw.get("runner")),
        autonomy=_build_sub(AutonomyConfig, raw.get("autonomy")),
        policy=_build_sub(PolicyConfig, raw.get("policy")),
        watcher=_build_sub(WatcherConfig, raw.get("watcher")),
        agent=_build_sub(AgentConfig, raw.get("agent")),
    )
