"""Collect reported model and token counts from agent JSON output."""

import json
from pathlib import Path


_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def collect_telemetry(agent, stdout_path):
    """Leave fields null when the agent's output lacks structured telemetry."""
    model = usage = None
    if agent in ("codex", "CodexAdapter"):
        usage_type = "turn.completed"
        model_types = ("thread.started", "turn.started", usage_type)
    elif agent in ("claude-code", "ClaudeCodeAdapter"):
        usage_type = "result"
        model_types = ("system", usage_type)
    else:
        stdout_path = None

    if stdout_path:
        try:
            with Path(stdout_path).open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    reported_model = event.get("model")
                    if event_type in model_types and isinstance(reported_model, str) and reported_model.strip():
                        model = reported_model.strip()
                    reported_usage = event.get("usage")
                    if event_type == usage_type and isinstance(reported_usage, dict):
                        counts = {
                            key: value for key, value in reported_usage.items()
                            if key in _TOKEN_FIELDS and type(value) is int and value >= 0
                        }
                        if counts:
                            usage = counts
        except OSError:
            pass
    return {"model": model, "usage": usage}
