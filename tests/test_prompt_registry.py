"""Tests for PromptRegistry versioning, GoalSpec immutability, and promotion gates."""

from pathlib import Path
from openagent_bridge.prompt_evaluator import BenchmarkCase, evaluate_candidate
from openagent_bridge.prompt_registry import GoalSpec, PromptRegistry, PromptTemplate


def test_goal_spec_immutability():
    goal = GoalSpec(
        original_goal="Fix database deadlock",
        constraints=["Do not modify postgres configs"],
        non_goals=["Do not upgrade database driver"],
    )
    assert goal.original_goal == "Fix database deadlock"

    # Frozen dataclass prevents accidental mutation
    import pytest
    with pytest.raises(Exception):
        goal.original_goal = "Rewritten by LLM"  # type: ignore


def test_prompt_versioning_and_promotion(tmp_path: Path):
    registry = PromptRegistry(root_dir=tmp_path / "prompts")

    t1 = PromptTemplate(
        template_id="coding_task",
        version=1,
        parent_version=None,
        content="System instructions v1",
        status="CHAMPION",
    )
    registry.save_template(t1)

    t2 = PromptTemplate(
        template_id="coding_task",
        version=2,
        parent_version=1,
        content="System instructions v2 with better adherence",
        status="EXPERIMENTAL",
    )
    registry.save_template(t2)

    champ = registry.get_champion("coding_task")
    assert champ is not None
    assert champ.version == 1

    # 1. Evaluation fails: not enough benchmark cases (< 20)
    eval_fail_cases = registry.evaluate_promotion(
        template_id="coding_task",
        champion_version=1,
        challenger_version=2,
        champion_score=0.80,
        challenger_score=0.88,
        critical_regressions=0,
        security_regressions=0,
        holdout_degradation=0.0,
        total_cases=10,  # Below 20 threshold
    )
    assert eval_fail_cases.eligible_for_promotion is False

    # 2. Evaluation fails: critical regression > 0
    eval_fail_reg = registry.evaluate_promotion(
        template_id="coding_task",
        champion_version=1,
        challenger_version=2,
        champion_score=0.80,
        challenger_score=0.90,
        critical_regressions=1,
        security_regressions=0,
        holdout_degradation=0.0,
        total_cases=25,
    )
    assert eval_fail_reg.eligible_for_promotion is False

    # 3. Evaluation succeeds: meets all guardrails
    eval_ok = registry.evaluate_promotion(
        template_id="coding_task",
        champion_version=1,
        challenger_version=2,
        champion_score=0.80,
        challenger_score=0.85,
        critical_regressions=0,
        security_regressions=0,
        holdout_degradation=0.005,
        total_cases=25,
    )
    assert eval_ok.eligible_for_promotion is True
    assert eval_ok.requires_human_approval is True

    assert not registry.promote("coding_task", 2, "alice")
    cases = [
        BenchmarkCase(str(i), f"Case {i}", f"Goal {i}", ["ok"], is_holdout=i >= 15,
                      critical=i == 0, security=i == 1)
        for i in range(20)
    ]
    evaluation = evaluate_candidate(
        registry, "coding_task", 2, cases,
        lambda version, case_id, prompt: "ok" if version == 2 or case_id != "5" else "omitted",
    )
    assert evaluation.eligible_for_promotion
    assert registry.get_champion("coding_task").version == 1
    assert not registry.promote("coding_task", 2, "")

    # 4. Human promotion
    promoted = registry.promote("coding_task", version=2, approved_by="senior_engineer_alice")
    assert promoted is True

    new_champ = registry.get_champion("coding_task")
    assert new_champ is not None
    assert new_champ.version == 2
