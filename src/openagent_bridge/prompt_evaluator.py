"""Offline prompt replay and benchmark evaluation runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from openagent_bridge.budget import BudgetLimits
from openagent_bridge.prompt_registry import (
    PromptRegistry, PromptTemplate, PromotionEvaluation, digest, goal_digest,
)
from openagent_bridge.task_prompt import TaskPromptRenderer


@dataclass
class BenchmarkCase:
    case_id: str
    description: str
    task_goal: str
    expected_output_contains: list[str] = field(default_factory=list)
    forbidden_output_contains: list[str] = field(default_factory=list)
    is_holdout: bool = False
    critical: bool = False
    security: bool = False


@dataclass
class PromptBenchmarkReport:
    total_cases: int
    passed_cases: int
    failed_cases: int
    score: float
    holdout_score: float
    details: list[dict[str, Any]]


class PromptBenchmarkRunner:
    """Runs deterministic test cases to grade candidate prompts."""

    def __init__(self, cases: list[BenchmarkCase]) -> None:
        self.cases = cases

    def run_benchmark(
        self,
        prompt_renderer: Callable[[str], str],
        model_runner: Callable[[str], str],
    ) -> PromptBenchmarkReport:
        """Run all test cases through the candidate prompt."""
        passed = 0
        failed = 0
        holdout_total = 0
        holdout_passed = 0
        details = []

        for c in self.cases:
            rendered = prompt_renderer(c.task_goal)
            output = model_runner(rendered)

            case_passed = True
            reasons = []

            for exp in c.expected_output_contains:
                if exp not in output:
                    case_passed = False
                    reasons.append(f"Missing expected text: {exp}")

            for forb in c.forbidden_output_contains:
                if forb in output:
                    case_passed = False
                    reasons.append(f"Contains forbidden text: {forb}")

            if case_passed:
                passed += 1
                if c.is_holdout:
                    holdout_passed += 1
            else:
                failed += 1

            if c.is_holdout:
                holdout_total += 1

            details.append({
                "case_id": c.case_id,
                "passed": case_passed,
                "reasons": reasons,
                "is_holdout": c.is_holdout,
            })

        total = len(self.cases)
        score = passed / total if total > 0 else 0.0
        holdout_score = holdout_passed / holdout_total if holdout_total > 0 else 1.0

        return PromptBenchmarkReport(
            total_cases=total,
            passed_cases=passed,
            failed_cases=failed,
            score=round(score, 3),
            holdout_score=round(holdout_score, 3),
            details=details,
        )


def benchmark_inputs(
    registry: PromptRegistry,
    template_id: str,
    version: int,
    cases: list[BenchmarkCase],
) -> tuple[PromptTemplate, PromptTemplate, dict[int, dict[str, str]]]:
    """Render the identical task contract used by the worker for both versions."""
    champion = registry.get_champion(template_id)
    if champion is None:
        raise ValueError("Register an initial champion before evaluating")
    candidate = registry.get_template(template_id, version)
    if candidate.parent_version != champion.version:
        raise ValueError("Candidate must descend from the current champion")
    for case in cases:
        if not all(isinstance(text, str) and text.strip() for text in (case.case_id, case.task_goal)):
            raise ValueError("Each benchmark case requires an ID and goal")
        if any(type(flag) is not bool for flag in (case.is_holdout, case.critical, case.security)):
            raise ValueError("Benchmark split and severity flags must be booleans")
        if not isinstance(case.expected_output_contains, list) or not isinstance(case.forbidden_output_contains, list):
            raise ValueError("Benchmark assertions must be lists of strings")
        assertions = [*case.expected_output_contains, *case.forbidden_output_contains]
        if not assertions or any(not isinstance(term, str) or not term.strip() for term in assertions):
            raise ValueError("Each benchmark case requires non-empty assertions")
    ids = {case.case_id for case in cases}
    goals = {goal_digest(case.task_goal) for case in cases}
    if len(ids) != len(cases) or len(goals) != len(cases):
        raise ValueError("Benchmark case IDs and goals must be unique across validation and holdout")
    if goals.intersection(candidate.training_goals):
        raise ValueError("Evaluation cases overlap the candidate training history")
    if not any(c.is_holdout for c in cases) or not any(not c.is_holdout for c in cases):
        raise ValueError("Independent validation and holdout cases are required")
    if not any(c.security for c in cases) or not any(c.critical for c in cases):
        raise ValueError("Critical and security assertions are required")
    rendered = {}
    for template in (champion, candidate):
        rendered[template.version] = {
            case.case_id: TaskPromptRenderer.render(
                {"original_goal": case.task_goal}, BudgetLimits(), template.content
            )
            for case in cases
        }
    return champion, candidate, rendered


def evaluate_candidate(
    registry: PromptRegistry,
    template_id: str,
    version: int,
    cases: list[BenchmarkCase],
    model_runner: Callable[[int, str, str], str],
) -> PromotionEvaluation:
    """Evaluate independent recorded outputs and persist evidence for human approval."""
    champion, candidate, rendered = benchmark_inputs(registry, template_id, version, cases)
    case_by_goal = {case.task_goal: case.case_id for case in cases}
    runner = PromptBenchmarkRunner(cases)
    evidence = {"champion_sha256": digest(champion.content), "challenger_sha256": digest(candidate.content)}

    def replay(template: PromptTemplate) -> PromptBenchmarkReport:
        prompts = rendered[template.version]
        output_evidence = {}
        prompt_to_case = {prompt: case_id for case_id, prompt in prompts.items()}

        def run(prompt: str) -> str:
            case_id = prompt_to_case[prompt]
            output = model_runner(template.version, case_id, prompt)
            if not isinstance(output, str) or not output.strip():
                raise ValueError(f"Missing model output for version {template.version}, case {case_id}")
            output_evidence[case_id] = {"prompt_sha256": digest(prompt), "output_sha256": digest(output)}
            return output

        report = runner.run_benchmark(lambda goal: prompts[case_by_goal[goal]], run)
        evidence[str(template.version)] = output_evidence
        return report

    def split_score(report: PromptBenchmarkReport, is_holdout: bool) -> float:
        split = [result for result in report.details if result["is_holdout"] == is_holdout]
        return sum(result["passed"] for result in split) / len(split)

    control, trial = replay(champion), replay(candidate)
    failed = [case for case, result in zip(cases, trial.details) if not result["passed"]]
    evaluation = registry.evaluate_promotion(
        template_id, champion.version, version, split_score(control, False),
        split_score(trial, False), sum(c.critical for c in failed),
        sum(c.security for c in failed), split_score(control, True) - split_score(trial, True),
        len(cases),
    )
    evidence["cases"] = [
        {
            "case_id": case.case_id,
            "goal_sha256": goal_digest(case.task_goal),
            "split": "holdout" if case.is_holdout else "validation",
            "critical": case.critical,
            "security": case.security,
            "expected_output_contains": case.expected_output_contains,
            "forbidden_output_contains": case.forbidden_output_contains,
            "champion_passed": before["passed"],
            "candidate_passed": after["passed"],
        }
        for case, before, after in zip(cases, control.details, trial.details)
    ]
    registry.record_review(evaluation, evidence)
    return evaluation
