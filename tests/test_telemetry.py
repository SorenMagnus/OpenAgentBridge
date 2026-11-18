import sqlite3

import pytest

from openagent_bridge.db import Database
from openagent_bridge.telemetry import collect_telemetry


@pytest.mark.parametrize(
    ("agent", "output", "expected"),
    [
        (
            "codex",
            '{"type":"turn.completed","usage":{"input_tokens":43,"cached_input_tokens":7,"output_tokens":0}}',
            {"model": None, "usage": {"input_tokens": 43, "cached_input_tokens": 7, "output_tokens": 0}},
        ),
        (
            "ClaudeCodeAdapter",
            '{"type":"system","subtype":"init","model":"fixture-model"}\n'
            '{"type":"result","usage":{"input_tokens":9,"output_tokens":-1,"cached_input_tokens":false}}',
            {"model": "fixture-model", "usage": {"input_tokens": 9}},
        ),
        (
            "codex",
            '{"type":"item.completed","model":"ignored","usage":{"input_tokens":9}}\n{"truncated"',
            {"model": None, "usage": None},
        ),
    ],
)
def test_structured_run_telemetry(tmp_path, agent, output, expected):
    path = tmp_path / "stdout.jsonl"
    path.write_text(output, encoding="utf-8")
    assert collect_telemetry(agent, path) == expected


def test_nullable_run_telemetry_migration(tmp_path):
    path = tmp_path / "bridge.db"
    db = Database(path)
    task_id = db.insert_task({"goal": "legacy task"})
    run_id = db.insert_run({"task_id": task_id, "iteration": 1})
    db.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE runs DROP COLUMN model")
        connection.execute("ALTER TABLE runs DROP COLUMN usage")
        connection.commit()
    finally:
        connection.close()

    db = Database(path)
    try:
        legacy, = db.get_runs(task_id)
        assert legacy["model"] is None
        assert legacy["usage"] is None
        db.update_run({"id": run_id, "model": "fixture", "usage": {"input_tokens": 2}})
        new = db.insert_run({"task_id": task_id, "iteration": 2, "usage": {"output_tokens": 8}})
        first, second = db.get_runs(task_id)
        assert first["model"] == "fixture"
        assert first["usage"] == {"input_tokens": 2}
        assert second["id"] == new
        assert second["usage"] == {"output_tokens": 8}
    finally:
        db.close()
