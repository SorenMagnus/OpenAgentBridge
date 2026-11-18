"""Tests for failure signature generation and stall detection."""

from openagent_bridge.evaluator import ProgressEvaluator, compute_failure_signature
from openagent_bridge.models import EvaluationReport, VerificationResult


def _make_report(passed: bool, failed_checks: list[str]) -> EvaluationReport:
    checks = []
    for fc in failed_checks:
        checks.append(
            VerificationResult(
                check_name=fc,
                kind="command",
                exit_code=1,
                passed=False,
            )
        )
    if passed:
        checks.append(
            VerificationResult(
                check_name="pytest_pass",
                kind="command",
                exit_code=0,
                passed=True,
            )
        )
    return EvaluationReport(
        task_id="t-eval-1",
        run_id="r-eval-1",
        base_commit="abc",
        head_commit="def",
        checks=checks,
        critical_failures=failed_checks,
        overall_pass=passed,
    )


def test_failure_signature_stable():
    rep1 = _make_report(False, ["test_auth", "test_db"])
    rep2 = _make_report(False, ["test_db", "test_auth"])

    sig1 = compute_failure_signature(rep1)
    sig2 = compute_failure_signature(rep2)
    assert sig1 == sig2
    assert sig1 != "ALL_PASSED"


def test_stall_detection_after_consecutive_identical_signatures():
    evaluator = ProgressEvaluator(stall_threshold=2)

    rep = _make_report(False, ["test_auth_failure"])

    res1 = evaluator.record_run(rep)
    assert res1["is_stalled"] is False
    assert res1["identical_rounds"] == 0

    res2 = evaluator.record_run(rep)
    assert res2["is_stalled"] is False
    assert res2["identical_rounds"] == 1

    res3 = evaluator.record_run(rep)
    assert res3["is_stalled"] is True
    assert res3["identical_rounds"] == 2
