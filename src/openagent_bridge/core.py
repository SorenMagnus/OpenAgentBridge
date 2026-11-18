"""Central Bridge Core orchestrator unifying access control, routing, queue, and evolution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openagent_bridge.access_control import AccessControlEngine, User
from openagent_bridge.approvals import ApprovalGate
from openagent_bridge.channels.router import ChannelRouter
from openagent_bridge.config import BridgeConfig, load_config
from openagent_bridge.db import Database
from openagent_bridge.events import EventManager
from openagent_bridge.models import UnifiedTaskRequest
from openagent_bridge.queue import TaskQueue
from openagent_bridge.self_evolution import HarnessEvolutionEngine

logger = logging.getLogger(__name__)


class BridgeCore:
    """The central runtime hub of OpenAgent Bridge."""

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or load_config()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.config.data_dir / "bridge.db")
        self.queue = TaskQueue(self.db)
        self.events = EventManager(self.db)
        self.acl = AccessControlEngine(self.config.auth)
        self.router = ChannelRouter()
        self.approval_gate = ApprovalGate(self.db)
        self.evolution = HarnessEvolutionEngine(self.db)

    def register_channel_adapter(self, adapter: Any) -> None:
        """Attach a channel adapter and initialize it."""
        adapter.initialize(self.config, self)
        self.router.register(adapter)

    def submit_unified_task(
        self, request: UnifiedTaskRequest
    ) -> tuple[bool, str, str | None]:
        """Validate, authenticate, and enqueue an incoming task from any channel.

        Returns (success, task_id, error_message).
        """
        # 1. Authenticate user against channel
        user = self.acl.authenticate(request.source_channel, request.user_id)
        if not user:
            err = (
                f"Unauthorized: sender '{request.user_id}' is not authorized on channel '{request.source_channel}'."
            )
            logger.warning(err)
            return False, "", err

        # 2. Authorize project access and dangerous permissions
        ok, err = self.acl.authorize_task_submission(
            user, request.project_id, request.permissions
        )
        if not ok:
            logger.warning("Permission denied for %s on %s: %s", user.id, request.project_id, err)
            return False, "", err or "Access denied"

        # 3. Convert to internal TaskSpec dict
        spec = request.to_task_spec()

        # 4. Enqueue into database queue
        try:
            task_id = self.queue.enqueue(spec)
        except Exception as exc:
            logger.exception("Failed to enqueue task: %s", exc)
            return False, "", str(exc)

        # 5. Emit audit event
        self.events.emit(
            task_id=task_id,
            type="TASK_ENQUEUED",
            payload={
                "user_id": user.id,
                "source_channel": request.source_channel,
                "project_id": request.project_id,
                "goal": request.user_goal,
            },
            source=f"channel:{request.source_channel}",
        )

        return True, task_id, None

    def cancel_task(
        self, task_id: str, user_id: str, reason: str = "User requested cancellation"
    ) -> tuple[bool, str]:
        """Cancel a pending or running task after checking permissions."""
        task = self.db.get_task(task_id)
        if not task:
            return False, f"Task '{task_id}' not found."

        channel = task.get("source_channel", "cli")
        user = self.acl.authenticate(channel, user_id)
        if not user:
            return False, f"Unauthorized: sender '{user_id}' cannot cancel tasks."

        ok, err = self.acl.authorize_task_action(user, task, "cancel")
        if not ok:
            return False, err or "Unauthorized"

        cancelled = self.queue.cancel(task_id, reason=reason)
        if cancelled:
            self.events.emit(
                task_id=task_id,
                type="TASK_CANCELLED",
                payload={"user_id": user.id, "reason": reason},
                source=f"channel:{channel}",
            )
            return True, f"Task '{task_id}' cancelled successfully."
        return False, f"Task '{task_id}' could not be cancelled (current status: {task.get('status')})."

    def get_task_report(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        """Fetch status and verification results for a task."""
        task = self.db.get_task(task_id)
        if not task:
            return None

        channel = task.get("source_channel", "cli")
        user = self.acl.authenticate(channel, user_id)
        if not user:
            return None

        ok, _ = self.acl.authorize_task_action(user, task, "report")
        if not ok:
            return None

        v_results = self.db.get_verification_results(task_id)
        runs = self.db.get_runs(task_id)

        return {
            "task_id": task_id,
            "status": task.get("status"),
            "iteration": task.get("iteration", 0),
            "project_id": task.get("project_id"),
            "goal": task.get("goal"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "verification_results": v_results,
            "runs_count": len(runs),
        }

    def resolve_approval(
        self, approval_id: str, user_id: str, approved: bool, reason: str = ""
    ) -> tuple[bool, str]:
        """Resolve a human approval request."""
        cur = self.db.connection.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        )
        row = cur.fetchone()
        if not row:
            return False, f"Approval '{approval_id}' not found."

        approval = dict(row)
        task_id = approval.get("task_id", "")
        task = self.db.get_task(task_id) or {}
        channel = task.get("source_channel", "cli")

        user = self.acl.authenticate(channel, user_id)
        if not user:
            return False, "Unauthorized sender."

        ok, err = self.acl.authorize_task_action(user, task, "approve" if approved else "reject")
        if not ok:
            return False, err or "Unauthorized"

        decision = "approved" if approved else "rejected"
        success = self.approval_gate.resolve(approval_id, decision=decision, resolved_by=user.id)
        if success:
            return True, f"Approval '{approval_id}' resolved: {decision.upper()}."
        return False, f"Failed to resolve approval '{approval_id}'."

    def get_projects(self, user_id: str) -> list[dict[str, Any]]:
        """Return projects accessible to the given user."""
        all_projects = self.db.list_projects()
        user = self.acl.authenticate("cli", user_id)
        if not user:
            return []
        if user.role == "admin" or "*" in user.allowed_projects:
            return all_projects
        return [p for p in all_projects if p["name"] in user.allowed_projects]

    def notify_task_update(self, task_id: str, message: str) -> None:
        """Send asynchronous task update to the originating channel."""
        task = self.db.get_task(task_id)
        if not task:
            return
        self.router.dispatch_task_update(task, message)

    def notify_approval_required(self, approval: dict[str, Any]) -> None:
        """Send approval alert to the originating channel."""
        task_id = approval.get("task_id", "")
        task = self.db.get_task(task_id) or {}
        self.router.dispatch_approval_request(approval, task)
