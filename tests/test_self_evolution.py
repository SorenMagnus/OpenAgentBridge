"""Unit tests for Harness Self-Evolution Loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from openagent_bridge.config import BridgeConfig
from openagent_bridge.db import Database
from openagent_bridge.self_evolution import (
    EvolutionMetrics,
    HarnessEvolutionEngine,
    StrategyProposal,
)


@pytest.fixture
def evolution_setup(tmp_path: Path):
    db_path = tmp_path / "evo.db"
    db = Database(db_path)
    config = BridgeConfig(data_dir=tmp_path)
    engine = HarnessEvolutionEngine(db)
    return db, config, engine


def test_self_evolution_analysis_and_proposals(evolution_setup):
    db, config, engine = evolution_setup

    # Insert simulated historical tasks
    # 2 succeeded, 3 failed/stalled
    for i in range(2):
        db.insert_task({"project_id": "p1", "goal": f"Task succ {i}", "status": "SUCCEEDED"})
    for i in range(2):
        t_id = db.insert_task({"project_id": "p1", "goal": f"Task fail {i}", "status": "FAILED"})
        db.insert_verification_result({
            "task_id": t_id,
            "check_name": "pytest acceptance",
            "passed": 0,
            "exit_code": 1,
        })
    db.insert_task({"project_id": "p1", "goal": "Task stall", "status": "REVIEW_REQUIRED"})

    # Run analysis
    metrics = engine.analyze_task_history()
    assert metrics.total_tasks == 5
    assert metrics.succeeded_tasks == 2
    assert metrics.failed_tasks == 2
    assert metrics.stalled_tasks == 1
    assert metrics.success_rate == 0.4
    assert "pytest acceptance (exit=1)" in metrics.common_failure_signatures

    # Formulate proposals
    proposals = engine.propose_optimizations(metrics)
    assert len(proposals) > 0
    prop = proposals[0]
    assert "Calibrate Repair Iteration Limit" in prop.title

    # Verify safety bounds
    ok, msg = engine.verify_proposal(prop)
    assert ok is True

    # Bad proposal that exceeds safety bounds
    bad_prop = StrategyProposal(
        proposal_id="bad_1",
        title="Unsafe iteration limit",
        description="Dangerous parameter",
        parameter_updates={"autonomy.max_iterations": 999},  # exceeds bound 15
        justification="unsafe",
    )
    ok_bad, msg_bad = engine.verify_proposal(bad_prop)
    assert ok_bad is False
    assert "outside safe bounds" in msg_bad


def test_self_evolution_approval_gating(evolution_setup):
    db, config, engine = evolution_setup

    prop = StrategyProposal(
        proposal_id="prop_test",
        title="Test Proposal",
        description="Update max iterations",
        parameter_updates={"autonomy.max_iterations": 6},
        justification="Incremental optimization",
    )

    # 1. Submit for approval
    app_id = engine.submit_for_approval(prop)
    assert app_id.startswith("app-evo-")

    # 2. Cannot apply while pending
    applied, msg = engine.apply_approved_proposal(app_id, prop, config)
    assert applied is False
    assert "not 'APPROVED'" in msg

    # 3. Approve
    db.connection.execute(
        "UPDATE approvals SET status = 'APPROVED', resolved_by = 'admin_human' WHERE id = ?",
        (app_id,),
    )
    db.connection.commit()

    # 4. Now apply succeeds
    applied_ok, msg_ok = engine.apply_approved_proposal(app_id, prop, config)
    assert applied_ok is True
    assert config.autonomy.max_iterations == 6
    assert "admin_human" in msg_ok
