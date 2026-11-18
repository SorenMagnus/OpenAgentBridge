"""Unit tests for transport mechanisms (Manual File, SSH wrapper, Local HTTP)."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from openagent_bridge.db import Database
from openagent_bridge.queue import TaskQueue
from openagent_bridge.transport.http import LocalHTTPTransport
from openagent_bridge.transport.manual import ManualFileTransport
from openagent_bridge.transport.ssh_cli import SSHCommandWrapper


@pytest.fixture
def temp_env(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    queue = TaskQueue(db)
    return tmp_path, db, queue


def test_manual_file_transport(temp_env):
    tmp_path, db, queue = temp_env
    transport = ManualFileTransport(tmp_path, queue)

    # Place a valid task spec file
    task_spec = {
        "project_name": "test_project",
        "goal": "Write a test file",
        "original_goal": "Write a test file",
        "idempotency_key": "test-manual-1",
    }
    in_file = transport.inbox / "task_spec_001.json"
    in_file.write_text(json.dumps(task_spec), encoding="utf-8")

    enqueued = transport.poll_inbox()
    assert len(enqueued) == 1
    task_id = enqueued[0]

    assert not in_file.exists()
    receipt_file = transport.outbox / "task_spec_001.receipt.json"
    assert receipt_file.exists()
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert receipt["task_id"] == task_id
    assert receipt["status"] == "QUEUED"

    # Test bad json handling
    bad_file = transport.inbox / "bad_file.json"
    bad_file.write_text("{invalid json", encoding="utf-8")
    transport.poll_inbox()
    err_file = transport.outbox / "bad_file.error.txt"
    assert err_file.exists()


def test_ssh_command_wrapper():
    # Allowed openagent CLI commands
    ok, err = SSHCommandWrapper.validate_command("openagent doctor")
    assert ok is True and err is None

    ok, err = SSHCommandWrapper.validate_command("openagent task submit --file task.json")
    assert ok is True and err is None

    ok, err = SSHCommandWrapper.validate_command("python -m openagent_bridge.cli status")
    assert ok is True and err is None

    # Blocked commands
    ok, err = SSHCommandWrapper.validate_command("")
    assert ok is False

    ok, err = SSHCommandWrapper.validate_command("bash -i")
    assert ok is False
    assert "Prohibited executable" in err

    ok, err = SSHCommandWrapper.validate_command("openagent format-hard-drive")
    assert ok is False
    assert "Prohibited subcommand" in err

    ok, err = SSHCommandWrapper.validate_command("openagent doctor; rm -rf /")
    assert ok is False
    assert "metacharacter" in err


def test_local_http_transport(temp_env):
    tmp_path, db, queue = temp_env
    # Pick a port unlikely to collide
    port = 18991
    server = LocalHTTPTransport(db, host="127.0.0.1", port=port)
    server.start()

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Health check
        req = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert data["status"] == "ok"

        # 2. Submit task
        task_payload = json.dumps({
            "project_name": "http_test",
            "goal": "Test via HTTP",
            "original_goal": "Test via HTTP",
            "idempotency_key": "http-key-1",
        }).encode("utf-8")

        post_req = urllib.request.Request(
            f"{base_url}/tasks",
            data=task_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(post_req) as resp:
            post_data = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 201
            assert "task_id" in post_data
            task_id = post_data["task_id"]

        # 3. Query task
        get_req = urllib.request.Request(f"{base_url}/tasks/{task_id}")
        with urllib.request.urlopen(get_req) as resp:
            task_info = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert task_info["id"] == task_id
            assert task_info["status"] == "QUEUED"

        # 4. Cancel task
        cancel_req = urllib.request.Request(
            f"{base_url}/tasks/{task_id}/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(cancel_req) as resp:
            cancel_info = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert cancel_info["cancelled"] is True

    finally:
        server.stop()
