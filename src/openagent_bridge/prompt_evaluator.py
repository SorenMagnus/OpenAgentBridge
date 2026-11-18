"""Offline prompt replay and benchmark evaluation runner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class BenchmarkCase:
    case_id: str
    description: str
    task_goal: str
    expected_output_contains: list[str] = field(default_factory=list)
    forbidden_output_contains: list[str] = field(default_factory=list)
    is_holdout: bool = False


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
