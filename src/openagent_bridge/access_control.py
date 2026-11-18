"""Multi-user and role-based access control (RBAC) engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openagent_bridge.config import AuthConfig


@dataclass
class User:
    """An authenticated user identity."""

    id: str
    name: str = ""
    role: str = "developer"  # "admin" | "developer" | "viewer"
    allowed_channels: list[str] = field(default_factory=lambda: ["*"])
    allowed_projects: list[str] = field(default_factory=lambda: ["*"])


class AccessControlEngine:
    """Enforces multi-user isolation, channel authentication, and project authorization."""

    def __init__(self, config: AuthConfig | None = None) -> None:
        self.config = config or AuthConfig()
        self._users: dict[str, User] = {}

        # Load users from config
        for u in self.config.users:
            user = User(
                id=str(u.get("id", "")),
                name=u.get("name", ""),
                role=u.get("role", "developer"),
                allowed_channels=list(u.get("allowed_channels", ["*"])),
                allowed_projects=list(u.get("allowed_projects", ["*"])),
            )
            if user.id:
                self._users[user.id] = user

    def add_user(self, user: User) -> None:
        """Register or update a user."""
        self._users[user.id] = user

    def get_user(self, user_id: str) -> User | None:
        """Retrieve a user by ID."""
        return self._users.get(user_id)

    def authenticate(self, channel: str, sender_id: str) -> User | None:
        """Authenticate an incoming message sender from a specific channel."""
        if not self.config.enabled:
            return User(id=sender_id or "anonymous", name="Anonymous", role="admin")

        # Try exact match or channel-prefixed match first
        candidates = [sender_id, f"{channel}:{sender_id}"]
        for cid in candidates:
            if cid in self._users:
                user = self._users[cid]
                # Verify channel permission
                if "*" in user.allowed_channels or channel in user.allowed_channels:
                    return user

        if channel == "cli" and self.config.allow_anonymous_cli:
            return User(id="cli:local", name="Local CLI Operator", role="admin")

        return None

    def authorize_task_submission(
        self, user: User, project_id: str, permissions: dict[str, Any] | None = None
    ) -> tuple[bool, str | None]:
        """Verify whether the user has permission to submit a task to the project."""
        if user.role == "viewer":
            return False, f"User '{user.id}' has role 'viewer' and cannot submit tasks."

        if "*" not in user.allowed_projects and project_id not in user.allowed_projects:
            return False, f"User '{user.id}' is not authorized to access project '{project_id}'."

        # Check risky permissions: only admin can request deploy=True
        perms = permissions or {}
        if perms.get("deploy") is True and user.role != "admin":
            return False, "Direct production deployment is forbidden without admin privilege."

        return True, None

    def authorize_task_action(
        self, user: User, task: dict[str, Any], action: str
    ) -> tuple[bool, str | None]:
        """Authorize an action on an existing task (view, cancel, report, approve)."""
        if user.role == "admin":
            return True, None

        task_owner = task.get("user_id")
        project_id = task.get("project_id", "")

        # Viewing or getting report
        if action in ("view", "report", "status"):
            if task_owner == user.id or "*" in user.allowed_projects or project_id in user.allowed_projects:
                return True, None
            return False, f"User '{user.id}' cannot view tasks of other users."

        # Cancelling task
        if action == "cancel":
            if task_owner == user.id:
                return True, None
            return False, f"User '{user.id}' is not the owner of task '{task.get('id')}'."

        # Approving operations
        if action in ("approve", "reject"):
            if user.role in ("admin", "developer") and (task_owner == user.id or user.role == "admin"):
                return True, None
            return False, f"User '{user.id}' cannot resolve approvals for this task."

        return False, f"Unrecognized or unauthorized action '{action}'."
