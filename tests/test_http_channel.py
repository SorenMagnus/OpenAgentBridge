"""Unit tests for universal HTTP REST Channel Adapter."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from openagent_bridge.channels.http_channel import HTTPChannelAdapter
from openagent_bridge.config import AuthConfig, BridgeConfig
from openagent_bridge.core import BridgeCore


@pytest.fixture
def http_setup(tmp_path: Path):
    port = 19123
    config = BridgeConfig(
        data_dir=tmp_path / "data",
        auth=AuthConfig(
            enabled=True,
            allow_anonymous_cli=True,
            api_tokens=["secret_token_123"],
            users=[
                {"id": "http:secret_token_123", "role": "developer", "allowed_channels": ["http"], "allowed_projects": ["sample_proj"]},
            ],
        ),
    )
    core = BridgeCore(config)
    core.db.register_project("sample_proj", str(tmp_path / "sample_proj"))

    adapter = HTTPChannelAdapter(host="127.0.0.1", port=port)
    core.register_channel_adapter(adapter)
    adapter.start()

    yield core, adapter, f"http://127.0.0.1:{port}"

    adapter.stop()


def test_http_channel_health_and_auth(http_setup):
    core, adapter, base_url = http_setup

    # 1. Health check without auth
    req = urllib.request.Request(f"{base_url}/health")
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert data["channel"] == "http"

    # 2. Unauthorized request
    proj_req = urllib.request.Request(f"{base_url}/api/v1/projects")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(proj_req)
    assert exc_info.value.code == 401


def test_http_channel_task_lifecycle(http_setup):
    core, adapter, base_url = http_setup
    auth_headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret_token_123",
    }

    # 1. List projects
    req_proj = urllib.request.Request(f"{base_url}/api/v1/projects", headers=auth_headers)
    with urllib.request.urlopen(req_proj) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert any(p["name"] == "sample_proj" for p in data["projects"])

    # 2. Submit task
    task_payload = json.dumps({
        "project_id": "sample_proj",
        "goal": "Write unit tests for authentication",
    }).encode("utf-8")

    req_submit = urllib.request.Request(
        f"{base_url}/api/v1/tasks",
        data=task_payload,
        headers=auth_headers,
        method="POST",
    )
    with urllib.request.urlopen(req_submit) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 201
        assert "task_id" in res_data
        task_id = res_data["task_id"]

    # 3. Query task
    req_get = urllib.request.Request(f"{base_url}/api/v1/tasks/{task_id}", headers=auth_headers)
    with urllib.request.urlopen(req_get) as resp:
        task_data = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert task_data["id"] == task_id
        assert task_data["status"] == "QUEUED"

    # 4. Cancel task
    cancel_payload = json.dumps({"reason": "Testing cancellation"}).encode("utf-8")
    req_cancel = urllib.request.Request(
        f"{base_url}/api/v1/tasks/{task_id}/cancel",
        data=cancel_payload,
        headers=auth_headers,
        method="POST",
    )
    with urllib.request.urlopen(req_cancel) as resp:
        cancel_data = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert cancel_data["cancelled"] is True


def test_http_channel_approval_resolution(http_setup):
    core, adapter, base_url = http_setup
    auth_headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret_token_123",
    }

    # Insert task & pending approval
    task_id = core.db.insert_task({
        "project_id": "sample_proj",
        "user_id": "http:secret_token_123",
        "source_channel": "http",
        "goal": "Approval test",
    })
    app_id = core.db.insert_approval({
        "task_id": task_id,
        "action": "MERGE_MAIN",
        "status": "pending",
    })

    # Resolve approval via HTTP POST
    resolve_payload = json.dumps({
        "approval_id": app_id,
        "approved": True,
        "reason": "Verified by HTTP client",
    }).encode("utf-8")

    req_resolve = urllib.request.Request(
        f"{base_url}/api/v1/approvals/{app_id}/resolve",
        data=resolve_payload,
        headers=auth_headers,
        method="POST",
    )
    with urllib.request.urlopen(req_resolve) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert res_data["resolved"] is True
