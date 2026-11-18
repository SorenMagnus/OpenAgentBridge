"""Unit tests for Telegram Bot Channel Adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openagent_bridge.channels.telegram_bot import TelegramBotAdapter
from openagent_bridge.config import AuthConfig, BridgeConfig, TelegramConfig
from openagent_bridge.core import BridgeCore


class MockTelegramAdapter(TelegramBotAdapter):
    """Subclass of TelegramBotAdapter that mocks network API requests."""

    def __init__(self, telegram_config: TelegramConfig | None = None) -> None:
        super().__init__(telegram_config)
        self.outbound_messages: list[dict[str, Any]] = []

    def _api_request(
        self, method: str, payload: dict[str, Any] | None = None, timeout: int = 20
    ) -> dict[str, Any]:
        if method == "sendMessage":
            self.outbound_messages.append(payload or {})
            return {"ok": True, "result": {"message_id": 999}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 123456, "is_bot": True, "first_name": "OpenAgentBot", "username": "openagent_test_bot"}}
        return {"ok": True, "result": []}


@pytest.fixture
def bridge_setup(tmp_path: Path):
    config = BridgeConfig(
        data_dir=tmp_path / "data",
        auth=AuthConfig(
            enabled=True,
            allow_anonymous_cli=True,
            users=[
                {"id": "telegram:1001", "role": "developer", "allowed_channels": ["telegram"], "allowed_projects": ["sample_app"]},
            ],
        ),
    )
    core = BridgeCore(config)
    core.db.register_project("sample_app", str(tmp_path / "sample_app"))

    tg_config = TelegramConfig(
        enabled=True,
        bot_token="TEST_BOT_TOKEN",
        allowed_user_ids=[1001],
        allowed_chat_ids=[2001],
    )
    adapter = MockTelegramAdapter(tg_config)
    core.register_channel_adapter(adapter)
    return core, adapter


def test_telegram_unauthorized_user(bridge_setup):
    core, adapter = bridge_setup

    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 9999, "first_name": "Intruder"},
            "chat": {"id": 5555},
            "text": "/task sample_app Delete everything",
        },
    }
    adapter.handle_incoming_update(update)

    # Should send access denied
    assert len(adapter.outbound_messages) == 1
    last_msg = adapter.outbound_messages[-1]
    assert last_msg["chat_id"] == 5555
    assert "Access Denied" in last_msg["text"]


def test_telegram_help_and_projects(bridge_setup):
    core, adapter = bridge_setup

    # /help
    adapter.handle_incoming_update({
        "update_id": 2,
        "message": {
            "message_id": 11,
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "/help",
        },
    })
    assert len(adapter.outbound_messages) == 1
    assert "OpenAgent Bridge" in adapter.outbound_messages[-1]["text"]

    # /projects
    adapter.handle_incoming_update({
        "update_id": 3,
        "message": {
            "message_id": 12,
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "/projects",
        },
    })
    assert len(adapter.outbound_messages) == 2
    assert "sample_app" in adapter.outbound_messages[-1]["text"]


def test_telegram_submit_status_cancel_lifecycle(bridge_setup):
    core, adapter = bridge_setup

    # 1. Submit task
    adapter.handle_incoming_update({
        "update_id": 4,
        "message": {
            "message_id": 13,
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": "/task sample_app Fix login edge case",
        },
    })
    assert len(adapter.outbound_messages) == 1
    submit_reply = adapter.outbound_messages[-1]["text"]
    assert "Task Enqueued Successfully" in submit_reply

    # Extract task ID from reply
    # "Task ID: `...`"
    task_id = submit_reply.split("`")[1]

    # Verify task in DB
    task = core.db.get_task(task_id)
    assert task is not None
    assert task["goal"] == "Fix login edge case"
    assert task["user_id"] == "telegram:1001"
    assert task["source_channel"] == "telegram"

    # 2. Check /status
    adapter.handle_incoming_update({
        "update_id": 5,
        "message": {
            "message_id": 14,
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": f"/status {task_id}",
        },
    })
    status_reply = adapter.outbound_messages[-1]["text"]
    assert "Task Status" in status_reply
    assert "QUEUED" in status_reply

    # 3. Check /cancel
    adapter.handle_incoming_update({
        "update_id": 6,
        "message": {
            "message_id": 15,
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": f"/cancel {task_id}",
        },
    })
    cancel_reply = adapter.outbound_messages[-1]["text"]
    assert "cancelled successfully" in cancel_reply

    updated_task = core.db.get_task(task_id)
    assert updated_task["status"] == "CANCELLED"


def test_telegram_approvals(bridge_setup):
    core, adapter = bridge_setup

    task_id = core.db.insert_task({
        "project_id": "sample_app",
        "user_id": "telegram:1001",
        "source_channel": "telegram",
        "goal": "Prepare production deploy",
    })

    app_id = core.db.insert_approval({
        "task_id": task_id,
        "action": "DEPLOY_PROD",
        "status": "pending",
    })

    # Test /approve
    adapter.handle_incoming_update({
        "update_id": 7,
        "message": {
            "message_id": 16,
            "from": {"id": 1001},
            "chat": {"id": 2001},
            "text": f"/approve {app_id}",
        },
    })
    approve_reply = adapter.outbound_messages[-1]["text"]
    assert "APPROVED" in approve_reply


def test_telegram_outbound_dispatch(bridge_setup):
    core, adapter = bridge_setup

    task = {
        "id": "t_outbound",
        "status": "SUCCEEDED",
        "iteration": 2,
        "reply_target": {"chat_id": 2001},
    }
    adapter.send_task_update(task, "All 12 acceptance tests passed!")

    assert len(adapter.outbound_messages) == 1
    msg = adapter.outbound_messages[-1]
    assert msg["chat_id"] == 2001
    assert "All 12 acceptance tests passed!" in msg["text"]
