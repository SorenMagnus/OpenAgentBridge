"""Objective evaluation, failure signatures, and progress comparison."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from openagent_bridge.models import EvaluationReport


def compute_failure_signature(report: EvaluationReport, extra_errors: list[str] | None = None) -> str:
    """Generate a stable hash signature of the failures in an evaluation report.

    Signature includes:
    - Names of failed checks
    - Exit codes
    - Normalized error messages
    """
    tokens: list[str] = []

    for check in report.checks:
        if not check.passed:
            tokens.append(f"check:{check.check_name}:{check.exit_code}")

    for fail in report.critical_failures:
        tokens.append(f"crit:{fail}")

    if extra_errors:
        for err in extra_errors:
            # Normalize memory addresses and temporary paths
            norm_err = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", err)
            norm_err = re.sub(r"[\w\-\\/\.]+pytest-[\w\-\\/\.]+", "TEMP_PYTEST_PATH", norm_err)
            tokens.append(f"err:{norm_err.strip()}")

    tokens.sort()
    raw = "|".join(tokens)
    if not raw:
        return "ALL_PASSED"

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ProgressEvaluator:
    """Evaluates whether consecutive repair iterations are making progress or stalled."""

    def __init__(self, stall_threshold: int = 2) -> None:
        self.stall_threshold = stall_threshold
        self.history: list[dict[str, Any]] = []
        self.identical_signature_count: int = 0
        self.last_signature: str | None = None

    def record_run(self, report: EvaluationReport, extra_errors: list[str] | None = None) -> dict[str, Any]:
        """Record an evaluation outcome and determine progress status.

        Returns a dictionary with:
        - pass_count, fail_count, overall_pass
        - signature
        - is_stalled: bool
        - improved: bool
        - regressed: bool
        """
        signature = compute_failure_signature(report, extra_errors)
        pass_count = sum(1 for c in report.checks if c.passed)
        fail_count = len(report.critical_failures)

        improved = False
        regressed = False

        if self.history:
            prev = self.history[-1]
            prev_pass = prev["pass_count"]
            prev_fail = prev["fail_count"]

            if pass_count > prev_pass or fail_count < prev_fail:
                improved = True
            elif pass_count < prev_pass or fail_count > prev_fail:
                regressed = True

            if signature == self.last_signature and not improved:
                self.identical_signature_count += 1
            else:
                self.identical_signature_count = 0
        else:
            self.identical_signature_count = 0

        self.last_signature = signature
        is_stalled = self.identical_signature_count >= self.stall_threshold

        entry = {
            "signature": signature,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "overall_pass": report.overall_pass,
            "improved": improved,
            "regressed": regressed,
            "is_stalled": is_stalled,
            "identical_rounds": self.identical_signature_count,
        }
        self.history.append(entry)
        return entry
