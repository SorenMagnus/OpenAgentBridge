"""Security policy enforcement for OpenAgent Bridge.

Code-enforced security boundaries that cannot be overridden by model prompts.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any


DEFAULT_FORBIDDEN_PATTERNS = [
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    ".ssh/*",
    "*/.ssh/*",
    "credentials/*",
    "*/credentials/*",
    "secrets/*",
    "*/secrets/*",
]

DANGEROUS_COMMAND_PATTERNS = [
    ["rm", "-rf", "/"],
    ["rmdir", "/s"],
    ["del", "/s"],
    ["format"],
    ["diskpart"],
    ["sudo"],
    ["runas"],
]


class PolicyViolationError(Exception):
    """Raised when an operation violates security policy."""


class Policy:
    """Security policy validator."""

    def __init__(self, forbidden_patterns: list[str] | None = None) -> None:
        self.forbidden_patterns = (
            forbidden_patterns if forbidden_patterns is not None else DEFAULT_FORBIDDEN_PATTERNS
        )

    def check_forbidden_paths(
        self, changed_files: list[str], extra_patterns: list[str] | None = None
    ) -> list[str]:
        """Check if any changed file matches forbidden patterns.

        Returns a list of violating file paths.
        """
        patterns = list(self.forbidden_patterns)
        if extra_patterns:
            patterns.extend(extra_patterns)

        violations: list[str] = []
        for file_path in changed_files:
            # Normalize path with forward slashes for matching
            norm_path = file_path.replace("\\", "/").lstrip("/")
            file_name = Path(norm_path).name

            for pat in patterns:
                pat_norm = pat.replace("\\", "/").lstrip("/")
                if fnmatch.fnmatch(norm_path, pat_norm) or fnmatch.fnmatch(file_name, pat_norm):
                    violations.append(file_path)
                    break
        return violations

    def check_dangerous_commands(self, argv: list[str]) -> bool:
        """Check if a command invocation contains dangerous or prohibited operations.

        Returns True if the command is considered dangerous/prohibited.
        """
        if not argv:
            return False

        lower_argv = [a.lower() for a in argv]
        cmd_str = " ".join(lower_argv)

        # Check explicit blacklisted commands
        for dangerous in DANGEROUS_COMMAND_PATTERNS:
            if all(d in lower_argv for d in dangerous):
                return True

        # Check dangerous git commands on main
        if "git" in lower_argv:
            if "--force" in lower_argv or "-f" in lower_argv:
                if "push" in lower_argv:
                    return True
            if "reset" in lower_argv and "--hard" in lower_argv:
                return True
            if "clean" in lower_argv and ("-fdx" in lower_argv or "-fd" in lower_argv):
                return True

        # Check destructive disk operations
        if any(term in cmd_str for term in ["format ", "diskpart", "del /s", "rmdir /s /q c:\\"]):
            return True

        return False

    def check_permissions(self, action: str, task_permissions: dict[str, Any]) -> bool:
        """Validate if an action is permitted by task permissions and policy.

        Returns True if allowed, False if denied.
        """
        action = action.lower()

        # Deploy is forbidden by default and requires explicit override
        if action == "deploy":
            return task_permissions.get("deploy") == "allow" or task_permissions.get("deploy") is True

        # Main branch merge requires human approval
        if action in ("merge", "merge_main"):
            merge_perm = task_permissions.get("merge", "human_approval")
            return merge_perm in ("allow", True)

        # Filesystem write outside task worktree is denied
        if action == "filesystem_write_main":
            return False

        # Network policy
        if action == "network_outbound":
            net_perm = task_permissions.get("network", "deny_by_default")
            return net_perm not in ("deny_by_default", "deny", False)

        return True
