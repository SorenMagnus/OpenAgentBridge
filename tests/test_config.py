"""Tests for configuration loading and validation."""

from pathlib import Path
import pytest
from openagent_bridge.config import BridgeConfig, load_config


def test_default_config():
    config = load_config(None)
    assert isinstance(config, BridgeConfig)
    assert config.data_dir.name == ".openagent"
    assert config.worker.concurrency == 1
    assert config.runner.default_timeout_seconds == 900
    assert config.autonomy.max_iterations == 5
    assert config.policy.merge_main == "require_approval"
    assert config.agent.preferred == "codex"


def test_custom_yaml_config(tmp_path: Path):
    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text("""
data_dir: "~/custom_openagent"
worker:
  concurrency: 2
  heartbeat_seconds: 15
runner:
  default_timeout_seconds: 600
agent:
  preferred: "claude-code"
""", encoding="utf-8")

    config = load_config(custom_yaml)
    assert "custom_openagent" in str(config.data_dir)
    assert config.worker.concurrency == 2
    assert config.worker.heartbeat_seconds == 15
    assert config.runner.default_timeout_seconds == 600
    assert config.agent.preferred == "claude-code"


def test_missing_config_file():
    with pytest.raises(FileNotFoundError):
        load_config(Path("/nonexistent/config.yaml"))
