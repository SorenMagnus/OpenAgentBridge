"""Tests for ProjectWatcher debounce, storm protection, and ignore patterns."""

import time
from pathlib import Path
from openagent_bridge.watcher import ProjectWatcher


def test_watcher_ignore_patterns(tmp_path: Path):
    changes = []
    watcher = ProjectWatcher(
        repo_path=tmp_path,
        on_change=lambda files: changes.append(files),
        debounce_ms=50,
    )

    # Ignored files
    assert watcher.is_ignored(".git/objects/abc") is True
    assert watcher.is_ignored("node_modules/package/index.js") is True
    assert watcher.is_ignored("dist/bundle.js") is True
    assert watcher.is_ignored(".openagent/bridge.db") is True
    assert watcher.is_ignored("app.log") is True
    assert watcher.is_ignored("src/__pycache__/app.cpython-311.pyc") is True

    # Valid files
    assert watcher.is_ignored("src/main.py") is False
    assert watcher.is_ignored("README.md") is False


def test_watcher_event_storm_debounce(tmp_path: Path):
    callback_calls = []
    watcher = ProjectWatcher(
        repo_path=tmp_path,
        on_change=lambda files: callback_calls.append(files),
        debounce_ms=100,  # 100ms debounce
    )
    watcher._running = True

    # Simulate event storm: 100 rapid file modification events in 10ms
    for i in range(100):
        watcher.notify_change(f"src/file_{i % 5}.py")

    # Immediate check: timer is active, callback should not have fired yet
    assert len(callback_calls) == 0

    # Wait for debounce window to expire
    time.sleep(0.25)

    # After debounce, exactly 1 callback call should have occurred
    assert len(callback_calls) == 1
    # 5 unique files should be in the debounced batch
    assert len(callback_calls[0]) == 5
