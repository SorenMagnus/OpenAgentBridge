"""Unit tests for offline prompt evaluator."""

from __future__ import annotations

from openagent_bridge.prompt_evaluator import (
    BenchmarkCase,
    PromptBenchmarkRunner,
)


def test_prompt_benchmark_runner_all_passed():
    cases = [
        BenchmarkCase(
            case_id="case-1",
            description="Simple task",
            task_goal="Fix bug in calculation",
            expected_output_contains=["def calculate", "return result"],
            forbidden_output_contains=["rm -rf"],
            is_holdout=False,
        ),
        BenchmarkCase(
            case_id="case-2",
            description="Holdout task",
            task_goal="Write unit tests",
            expected_output_contains=["test_"],
            is_holdout=True,
        ),
    ]

    runner = PromptBenchmarkRunner(cases)

    def dummy_renderer(goal: str) -> str:
        return f"System prompt: follow rules. Goal: {goal}"

    def dummy_model(prompt: str) -> str:
        if "Fix bug" in prompt:
            return "def calculate():\n    return result"
        return "def test_something():\n    assert True"

    report = runner.run_benchmark(dummy_renderer, dummy_model)
    assert report.total_cases == 2
    assert report.passed_cases == 2
    assert report.failed_cases == 0
    assert report.score == 1.0
    assert report.holdout_score == 1.0


def test_prompt_benchmark_runner_failure_reasons():
    cases = [
        BenchmarkCase(
            case_id="case-1",
            description="Failing test",
            task_goal="Add feature",
            expected_output_contains=["REQUIRED_STRING"],
            forbidden_output_contains=["DANGEROUS_CALL"],
            is_holdout=True,
        )
    ]

    runner = PromptBenchmarkRunner(cases)

    def dummy_renderer(goal: str) -> str:
        return goal

    def dummy_model(prompt: str) -> str:
        return "I will use DANGEROUS_CALL here"

    report = runner.run_benchmark(dummy_renderer, dummy_model)
    assert report.total_cases == 1
    assert report.passed_cases == 0
    assert report.failed_cases == 1
    assert report.score == 0.0
    assert report.holdout_score == 0.0
    assert len(report.details[0]["reasons"]) == 2
    assert "Missing expected text: REQUIRED_STRING" in report.details[0]["reasons"]
    assert "Contains forbidden text: DANGEROUS_CALL" in report.details[0]["reasons"]
