"""Unit tests for BridgeCore orchestration and Unified Task Protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent_bridge.channels.cli_channel import CLIChannelAdapter
from openagent_bridge.config import AuthConfig, BridgeConfig
from openagent_bridge.core import BridgeCore
from openagent_bridge.models import UnifiedTaskRequest


@pytest.fixture
def core_instance(tmp_path: Path) -> BridgeCore:
    config = BridgeConfig(
        data_dir=tmp_path / "bridge_data",
        auth=AuthConfig(
            enabled=True,
            allow_anonymous_cli=True,
            users=[
                {"id": "developer_1", "role": "developer", "allowed_channels": ["cli", "http"], "allowed_projects": ["proj_demo"]},
                {"id": "viewer_1", "role": "viewer", "allowed_channels": ["cli"], "allowed_projects": ["*"]},
            ],
        ),
    )
    core = BridgeCore(config)
    core.db.register_project("proj_demo", str(tmp_path / "demo_repo"))
    return core


def test_core_submit_unified_task_success(core_instance: BridgeCore):
    cli_adapter = CLIChannelAdapter()
    core_instance.register_channel_adapter(cli_adapter)

    req = UnifiedTaskRequest(
        project_id="proj_demo",
        user_goal="Implement login endpoint",
        user_id="developer_1",
        source_channel="cli",
    )

    ok, task_id, err = core_instance.submit_unified_task(req)
    assert ok is True
    assert task_id != ""
    assert err is None

    # Check task in DB
    task = core_instance.db.get_task(task_id)
    assert task is not None
    assert task["project_id"] == "proj_demo"
    assert task["user_id"] == "developer_1"
    assert task["goal"] == "Implement login endpoint"
    assert task["original_goal"] == "Implement login endpoint"
    assert task["status"] == "QUEUED"


def test_core_submit_unified_task_unauthorized(core_instance: BridgeCore):
    # Unknown user
    req = UnifiedTaskRequest(
        project_id="proj_demo",
        user_goal="Do unauthorized work",
        user_id="intruder",
        source_channel="http",
    )
    ok, task_id, err = core_instance.submit_unified_task(req)
    assert ok is False
    assert "Unauthorized" in err

    # Viewer user cannot submit
    req2 = UnifiedTaskRequest(
        project_id="proj_demo",
        user_goal="Do work as viewer",
        user_id="viewer_1",
        source_channel="cli",
    )
    ok2, _, err2 = core_instance.submit_unified_task(req2)
    assert ok2 is False
    assert "viewer" in err2


def test_core_cancel_task(core_instance: BridgeCore):
    req = UnifiedTaskRequest(
        project_id="proj_demo",
        user_goal="Work to be cancelled",
        user_id="developer_1",
        source_channel="cli",
    )
    ok, task_id, _ = core_instance.submit_unified_task(req)
    assert ok is True

    # Cancel by owner
    cancel_ok, msg = core_instance.cancel_task(task_id, user_id="developer_1", reason="Changed mind")
    assert cancel_ok is True
    assert "cancelled successfully" in msg

    task = core_instance.db.get_task(task_id)
    assert task["status"] == "CANCELLED"


def test_core_approvals_and_reports(core_instance: BridgeCore):
    req = UnifiedTaskRequest(
        project_id="proj_demo",
        user_goal="Work with approval",
        user_id="developer_1",
        source_channel="cli",
    )
    ok, task_id, _ = core_instance.submit_unified_task(req)
    assert ok is True

    # Request approval
    app_id = core_instance.db.insert_approval({
        "task_id": task_id,
        "action": "MERGE_MAIN",
        "status": "PENDING",
    })

    # Approve
    app_ok, app_msg = core_instance.resolve_approval(app_id, user_id="developer_1", approved=True)
    assert app_ok is True
    assert "APPROVED" in app_msg

    # Query report
    report = core_instance.get_task_report(task_id, user_id="developer_1")
    assert report is not None
    assert report["task_id"] == task_id
    assert report["project_id"] == "proj_demo"
