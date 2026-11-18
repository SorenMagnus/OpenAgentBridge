"""Coding Agent adapters for OpenAgent Bridge."""

from openagent_bridge.adapters.base import AgentAdapter
from openagent_bridge.adapters.codex import CodexAdapter
from openagent_bridge.adapters.claude_code import ClaudeCodeAdapter

__all__ = ["AgentAdapter", "CodexAdapter", "ClaudeCodeAdapter"]
