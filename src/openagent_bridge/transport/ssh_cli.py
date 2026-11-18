"""SSH command restriction wrapper (ForceCommand security gate)."""

from __future__ import annotations

import shlex
from typing import Any


ALLOWED_SUBCOMMANDS = {
    "doctor",
    "project",
    "task",
    "events",
    "cancel",
    "status",
    "report",
}


class SSHCommandWrapper:
    """Restricts SSH forced commands to non-interactive openagent CLI calls."""

    @staticmethod
    def validate_command(ssh_original_command: str) -> tuple[bool, str | None]:
        """Validate if SSH_ORIGINAL_COMMAND is permitted.

        Returns (is_allowed, reason).
        """
        if not ssh_original_command or not ssh_original_command.strip():
            return False, "Empty SSH command not allowed (interactive shell is blocked)"

        try:
            tokens = shlex.split(ssh_original_command)
        except ValueError as exc:
            return False, f"Malformed shell command string: {exc}"

        if not tokens:
            return False, "No command tokens found"

        prog = tokens[0].lower().replace(".exe", "")
        if prog not in ("openagent", "python"):
            return False, f"Prohibited executable '{tokens[0]}'; only 'openagent' is allowed"

        # Check for dangerous metacharacters
        for tok in tokens:
            if any(char in tok for char in [";", "&", "|", "`", "$", ">", "<"]):
                return False, f"Prohibited shell metacharacter in token: {tok}"

        # If python -m openagent_bridge.cli
        subcommand = ""
        if prog == "python":
            if len(tokens) >= 3 and tokens[1] == "-m" and "openagent" in tokens[2]:
                subcommand = tokens[3] if len(tokens) > 3 else ""
            else:
                return False, "Arbitrary python execution is blocked via SSH"
        else:
            subcommand = tokens[1] if len(tokens) > 1 else ""

        if subcommand.lower() not in ALLOWED_SUBCOMMANDS:
            return False, f"Prohibited subcommand '{subcommand}'; permitted: {sorted(ALLOWED_SUBCOMMANDS)}"

        return True, None
