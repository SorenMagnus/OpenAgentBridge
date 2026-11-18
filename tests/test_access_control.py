"""Unit tests for Multi-User Access Control and Role-Based Permissions."""

from __future__ import annotations

from openagent_bridge.access_control import AccessControlEngine, User
from openagent_bridge.config import AuthConfig


def test_access_control_authentication():
    config = AuthConfig(
        enabled=True,
        allow_anonymous_cli=True,
        users=[
            {"id": "alice", "name": "Alice Admin", "role": "admin", "allowed_channels": ["cli", "telegram"], "allowed_projects": ["*"]},
            {"id": "telegram:12345", "name": "Bob Dev", "role": "developer", "allowed_channels": ["telegram"], "allowed_projects": ["proj_alpha"]},
            {"id": "charlie", "name": "Charlie Viewer", "role": "viewer", "allowed_channels": ["http"], "allowed_projects": ["*"]},
        ],
    )
    acl = AccessControlEngine(config)

    # 1. Local CLI anonymous fallback
    cli_user = acl.authenticate("cli", "local_user")
    assert cli_user is not None
    assert cli_user.role == "admin"

    # 2. Whitelisted Telegram user
    tg_user = acl.authenticate("telegram", "12345")
    assert tg_user is not None
    assert tg_user.id == "telegram:12345"
    assert tg_user.role == "developer"

    # 3. Channel restriction
    # Bob is only allowed on telegram, not on http
    assert acl.authenticate("http", "12345") is None

    # 4. Unknown sender
    assert acl.authenticate("telegram", "99999") is None


def test_access_control_task_authorization():
    acl = AccessControlEngine(AuthConfig(enabled=True))
    admin = User(id="admin", role="admin", allowed_projects=["*"])
    dev = User(id="dev1", role="developer", allowed_projects=["proj_alpha"])
    viewer = User(id="viewer1", role="viewer", allowed_projects=["*"])

    # Viewer cannot submit
    ok, err = acl.authorize_task_submission(viewer, "proj_alpha")
    assert ok is False
    assert "viewer" in err

    # Dev can submit to assigned project
    ok, err = acl.authorize_task_submission(dev, "proj_alpha")
    assert ok is True

    # Dev cannot submit to unassigned project
    ok, err = acl.authorize_task_submission(dev, "proj_beta")
    assert ok is False
    assert "not authorized" in err

    # Non-admin cannot request direct production deploy
    ok, err = acl.authorize_task_submission(dev, "proj_alpha", permissions={"deploy": True})
    assert ok is False
    assert "deployment is forbidden" in err

    # Admin can do all
    ok, err = acl.authorize_task_submission(admin, "proj_beta", permissions={"deploy": True})
    assert ok is True


def test_access_control_task_actions():
    acl = AccessControlEngine(AuthConfig(enabled=True))
    admin = User(id="admin", role="admin", allowed_projects=["*"])
    dev = User(id="dev1", role="developer", allowed_projects=["proj_alpha"])
    other_dev = User(id="dev2", role="developer", allowed_projects=["proj_alpha"])

    task = {"id": "t1", "user_id": "dev1", "project_id": "proj_alpha"}

    # Owner can view and cancel
    ok, _ = acl.authorize_task_action(dev, task, "view")
    assert ok is True
    ok, _ = acl.authorize_task_action(dev, task, "cancel")
    assert ok is True

    # Non-owner cannot cancel
    ok, err = acl.authorize_task_action(other_dev, task, "cancel")
    assert ok is False
    assert "not the owner" in err

    # Admin can cancel any task
    ok, _ = acl.authorize_task_action(admin, task, "cancel")
    assert ok is True
