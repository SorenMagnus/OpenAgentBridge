"""Tests for ApprovalGate creation, resolution, and authorization checks."""

from pathlib import Path
from openagent_bridge.approvals import ApprovalGate
from openagent_bridge.db import Database


def test_approval_request_and_resolution(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    gate = ApprovalGate(db)
    task_id = db.insert_task({
        "idempotency_key": "appr-01",
        "goal": "Approval test",
        "original_goal": "Approval test",
    })

    # Initially not approved
    assert gate.is_approved(task_id, "merge_main") is False

    # Request approval
    approval_id = gate.request(task_id, "merge_main")
    assert approval_id is not None

    pending = gate.list_pending(task_id)
    assert len(pending) == 1
    assert pending[0]["action"] == "merge_main"

    # Approve
    resolved = gate.resolve(approval_id, decision="approved", resolved_by="admin_alice")
    assert resolved is True

    # Now approved
    assert gate.is_approved(task_id, "merge_main") is True
    assert len(gate.list_pending(task_id)) == 0


def test_approval_rejection(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    gate = ApprovalGate(db)
    task_id = db.insert_task({
        "idempotency_key": "appr-02",
        "goal": "Reject test",
        "original_goal": "Reject test",
    })

    appr_id = gate.request(task_id, "deploy_production")
    resolved = gate.resolve(appr_id, decision="rejected", resolved_by="admin_bob")
    assert resolved is True

    assert gate.is_approved(task_id, "deploy_production") is False
