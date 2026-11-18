"""Unit tests for authenticated task worktree file read endpoint."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from openagent_bridge.channels.http_channel import HTTPChannelAdapter
from openagent_bridge.config import AuthConfig, BridgeConfig
from openagent_bridge.core import BridgeCore


@pytest.fixture
def http_task_files_setup(tmp_path: Path):
    port = 19135
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    config = BridgeConfig(
        data_dir=tmp_path / "data",
        auth=AuthConfig(
            enabled=True,
            allow_anonymous_cli=True,
            api_tokens=["secret_token_123"],
            users=[
                {
                    "id": "http:secret_token_123",
                    "role": "developer",
                    "allowed_channels": ["http"],
                    "allowed_projects": ["sample_proj"],
                },
            ],
        ),
    )
    core = BridgeCore(config)
    core.db.register_project("sample_proj", str(tmp_path / "sample_proj"))

    task_id = core.db.insert_task({
        "project_id": "sample_proj",
        "user_id": "http:secret_token_123",
        "source_channel": "http",
        "goal": "Test task worktree file reading",
        "worktree_path": str(worktree_dir),
    })

    adapter = HTTPChannelAdapter(host="127.0.0.1", port=port)
    core.register_channel_adapter(adapter)
    adapter.start()

    yield core, adapter, f"http://127.0.0.1:{port}", task_id, worktree_dir

    adapter.stop()


def test_task_files_unauthorized(http_task_files_setup):
    """Case 1: No token / wrong token returns 401."""
    _core, _adapter, base_url, task_id, worktree_dir = http_task_files_setup
    (worktree_dir / "ENV_REPORT.md").write_text("Sample report", encoding="utf-8")

    # 1. No token
    req_no_auth = urllib.request.Request(f"{base_url}/api/v1/tasks/{task_id}/files/ENV_REPORT.md")
    with pytest.raises(urllib.error.HTTPError) as exc_no_auth:
        urllib.request.urlopen(req_no_auth)
    assert exc_no_auth.value.code == 401

    # 2. Wrong token
    req_bad_auth = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/ENV_REPORT.md",
        headers={"Authorization": "Bearer wrong_token_999"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_bad_auth:
        urllib.request.urlopen(req_bad_auth)
    assert exc_bad_auth.value.code == 401


def test_task_files_read_success(http_task_files_setup):
    """Case 2: Normal read of ENV_REPORT.md returns 200 and content verbatim equal."""
    _core, _adapter, base_url, task_id, worktree_dir = http_task_files_setup
    auth_headers = {"Authorization": "Bearer secret_token_123"}

    env_content = (
        "# Environment Report\n"
        "OS: Windows 11 Pro\n"
        "Python: 3.12.16\n"
        "Git: 2.56.0\n"
        "Codex: 0.169.0-alpha.11.1\n"
        "Status: VERIFIED\n"
    )
    (worktree_dir / "ENV_REPORT.md").write_bytes(env_content.encode("utf-8"))

    req = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/ENV_REPORT.md",
        headers=auth_headers,
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))

    assert data["task_id"] == task_id
    assert data["path"] == "ENV_REPORT.md"
    assert data["encoding"] == "utf-8"
    assert data["size"] == len(env_content.encode("utf-8"))
    assert data["content"] == env_content

    # Also test nested directory path (tools/smoke-notes.txt)
    tools_dir = worktree_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    notes_content = "Smoke check passed at iteration 1."
    (tools_dir / "smoke-notes.txt").write_bytes(notes_content.encode("utf-8"))

    req_nested = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/tools/smoke-notes.txt",
        headers=auth_headers,
    )
    with urllib.request.urlopen(req_nested) as resp:
        assert resp.status == 200
        nested_data = json.loads(resp.read().decode("utf-8"))

    assert nested_data["task_id"] == task_id
    assert nested_data["path"] == "tools/smoke-notes.txt"
    assert nested_data["size"] == len(notes_content.encode("utf-8"))
    assert nested_data["content"] == notes_content


def test_task_files_path_traversal_rejected(http_task_files_setup):
    """Case 3: Path traversal (..) returns 400."""
    _core, _adapter, base_url, task_id, _worktree_dir = http_task_files_setup
    auth_headers = {"Authorization": "Bearer secret_token_123"}

    # 1. Plain ..
    req_dotdot = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/../secret.txt",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_dotdot:
        urllib.request.urlopen(req_dotdot)
    assert exc_dotdot.value.code == 400

    # 2. Encoded %2e%2e
    req_encoded = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/%2e%2e/secret.txt",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_encoded:
        urllib.request.urlopen(req_encoded)
    assert exc_encoded.value.code == 400

    # 3. Subdirectory .. traversal
    req_sub_dotdot = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/subdir/../../etc/passwd",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_sub:
        urllib.request.urlopen(req_sub_dotdot)
    assert exc_sub.value.code == 400


def test_task_files_not_found(http_task_files_setup):
    """Case 4: Target not found (missing file, missing task, or directory) returns 404."""
    _core, _adapter, base_url, task_id, worktree_dir = http_task_files_setup
    auth_headers = {"Authorization": "Bearer secret_token_123"}

    # 1. Missing file in existing worktree
    req_missing = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/nonexistent.md",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_missing:
        urllib.request.urlopen(req_missing)
    assert exc_missing.value.code == 404

    # 2. Non-existent task
    req_bad_task = urllib.request.Request(
        f"{base_url}/api/v1/tasks/task-nonexistent-999/files/ENV_REPORT.md",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_bad_task:
        urllib.request.urlopen(req_bad_task)
    assert exc_bad_task.value.code == 404

    # 3. Target is directory instead of file
    somedir = worktree_dir / "somedir"
    somedir.mkdir(parents=True, exist_ok=True)
    req_dir = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/somedir",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_dir:
        urllib.request.urlopen(req_dir)
    assert exc_dir.value.code == 404


def test_task_files_exceeds_size_limit(http_task_files_setup):
    """Case 5: File size exceeding 256 KB (262144 bytes) returns 413 with size and limit."""
    _core, _adapter, base_url, task_id, worktree_dir = http_task_files_setup
    auth_headers = {"Authorization": "Bearer secret_token_123"}

    large_size = 262145  # 256 KB + 1 byte
    (worktree_dir / "large.txt").write_bytes(b"A" * large_size)

    req = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/large.txt",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)

    assert exc_info.value.code == 413
    err_body = json.loads(exc_info.value.read().decode("utf-8"))
    assert err_body["size"] == large_size
    assert err_body["limit"] == 262144


def test_task_files_binary_unsupported(http_task_files_setup):
    """Case 6: Binary file that fails UTF-8 decoding returns 415."""
    _core, _adapter, base_url, task_id, worktree_dir = http_task_files_setup
    auth_headers = {"Authorization": "Bearer secret_token_123"}

    # Invalid UTF-8 byte sequence
    (worktree_dir / "binary.bin").write_bytes(b"\x80\x81\xff\xfe\xaa\xbb")

    req = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/files/binary.bin",
        headers=auth_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)

    assert exc_info.value.code == 415
