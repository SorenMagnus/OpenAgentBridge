"""Exercise the offline operator workflow with version-bound replay evidence."""

import json

from openagent_bridge.cli import main
from openagent_bridge.db import Database
from openagent_bridge.prompt_registry import PromptRegistry


def test_prompt_cli_rejects_overlap_and_regressions_until_operator_approval(tmp_path, capsys):
    config = tmp_path / "bridge.yaml"
    config.write_text(f"data_dir: {tmp_path.as_posix()}\n", encoding="utf-8")
    guidance = tmp_path / "guidance.txt"
    guidance.write_text("Candidate guidance", encoding="utf-8")
    fixtures = tmp_path / "cases.json"
    recordings = tmp_path / "recordings.json"
    db = Database(tmp_path / "bridge.db")
    db.insert_task({"goal": "historical task", "original_goal": "Historical task"})
    db.close()

    def prompt(*args):
        return main(["--config", str(config), "prompt", *map(str, args)])

    assert prompt("bootstrap") == 0
    assert prompt("propose", 2, guidance) == 0
    capsys.readouterr()
    cases = [
        {
            "case_id": f"case-{index}",
            "description": f"Scenario {index}",
            "task_goal": f"Evaluate scenario {index}",
            "expected_output_contains": ["confirmed"],
            "forbidden_output_contains": ["forbidden text"],
            "is_holdout": index >= 15,
            "critical": index == 0,
            "security": index == 1,
        }
        for index in range(20)
    ]
    distinct_goal = cases[4]["task_goal"]
    cases[4]["task_goal"] = " historical  TASK "
    fixtures.write_text(json.dumps(cases), encoding="utf-8")
    assert prompt("inputs", 2, fixtures) == 1
    assert "overlap" in capsys.readouterr().err
    cases[4]["task_goal"] = cases[-1]["task_goal"]
    fixtures.write_text(json.dumps(cases), encoding="utf-8")
    assert prompt("inputs", 2, fixtures) == 1
    assert "must be unique" in capsys.readouterr().err

    cases[4]["task_goal"] = distinct_goal
    fixtures.write_text(json.dumps(cases), encoding="utf-8")
    assert prompt("inputs", 2, fixtures) == 0
    outputs = json.loads(capsys.readouterr().out)
    for version in outputs.values():
        for row in version.values():
            assert "Task Contract v1" in row.pop("prompt")
            row["output"] = "confirmed"
    outputs["1"]["case-3"]["output"] = "omitted"
    outputs["1"]["case-4"]["output"] = "omitted"

    def evaluate():
        recordings.write_text(json.dumps(outputs), encoding="utf-8")
        return prompt("evaluate", 2, fixtures, recordings)

    digest = outputs["2"]["case-0"]["prompt_sha256"]
    outputs["2"]["case-0"]["prompt_sha256"] = "wrong"
    assert evaluate() == 1
    assert "Recorded prompt does not match" in capsys.readouterr().err
    outputs["2"]["case-0"]["prompt_sha256"] = digest
    outputs["2"]["case-1"]["output"] = "confirmed forbidden text"
    outputs["2"]["case-17"]["output"] = "omitted"
    assert evaluate() == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["security_regressions"] == 1
    assert blocked["holdout_degradation"] > 0.01
    assert prompt("approve", 2, "--approved-by", "operator") == 1
    capsys.readouterr()

    outputs["2"]["case-1"]["output"] = "confirmed"
    outputs["2"]["case-17"]["output"] = "confirmed"
    assert evaluate() == 0
    registry = PromptRegistry(tmp_path / "prompts")
    assert registry.get_champion("coding_task").version == 1
    assert prompt("approve", 2, "--approved-by", "operator") == 0
    assert registry.get_champion("coding_task").version == 2
    metadata_path = registry.get_template_dir("coding_task") / "metadata.json"
    candidate = json.loads(metadata_path.read_text(encoding="utf-8"))["v002"]
    assert candidate["approved_by"] == "operator"
    assert len(candidate["evidence"]["cases"]) == 20
