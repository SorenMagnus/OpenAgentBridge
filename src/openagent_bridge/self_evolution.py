"""Self-Evolution Loop: analyzes historical task feedback to improve harness strategies."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openagent_bridge.config import BridgeConfig
from openagent_bridge.db import Database


@dataclass
class EvolutionMetrics:
    """Aggregated operational metrics from historical runs."""

    total_tasks: int
    succeeded_tasks: int
    failed_tasks: int
    stalled_tasks: int
    success_rate: float
    common_failure_signatures: dict[str, int]
    bottlenecks: list[str]


@dataclass
class StrategyProposal:
    """A proposed improvement to harness operating rules or budgets."""

    proposal_id: str
    title: str
    description: str
    parameter_updates: dict[str, Any]
    justification: str
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class HarnessEvolutionEngine:
    """Analyzes task history, formulates harness optimizations, and gates self-updates behind human approval."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def analyze_task_history(self) -> EvolutionMetrics:
        """Scan historical tasks, verification results, and failure patterns."""
        cur = self.db.connection.execute(
            "SELECT id, status, iteration, budget FROM tasks WHERE status IN ('SUCCEEDED', 'FAILED', 'BUDGET_EXCEEDED', 'REVIEW_REQUIRED')"
        )
        rows = [dict(r) for r in cur.fetchall()]

        total = len(rows)
        succeeded = sum(1 for r in rows if r["status"] == "SUCCEEDED")
        failed = sum(1 for r in rows if r["status"] in ("FAILED", "BUDGET_EXCEEDED"))
        stalled = sum(1 for r in rows if r["status"] == "REVIEW_REQUIRED")
        success_rate = (succeeded / total) if total > 0 else 1.0

        # Scan verification results for failures
        cur_vr = self.db.connection.execute(
            "SELECT check_name, exit_code, COUNT(*) as cnt FROM verification_results WHERE passed = 0 GROUP BY check_name, exit_code"
        )
        sig_counts: dict[str, int] = {}
        for r in cur_vr.fetchall():
            sig = f"{r['check_name']} (exit={r['exit_code']})"
            sig_counts[sig] = r["cnt"]

        bottlenecks = []
        if stalled > 0 and stalled / (total or 1) > 0.2:
            bottlenecks.append("High stall rate: repair prompts may need better error localization.")
        if failed > 0 and failed / (total or 1) > 0.3:
            bottlenecks.append("High failure rate: acceptance test timeout or budget bounds may be too restrictive.")

        return EvolutionMetrics(
            total_tasks=total,
            succeeded_tasks=succeeded,
            failed_tasks=failed,
            stalled_tasks=stalled,
            success_rate=round(success_rate, 3),
            common_failure_signatures=sig_counts,
            bottlenecks=bottlenecks,
        )

    def propose_optimizations(self, metrics: EvolutionMetrics) -> list[StrategyProposal]:
        """Formulate bounded strategy adjustments based on historical metrics."""
        proposals = []

        # If success rate is low and budget exceeded is frequent, propose iteration calibration
        if metrics.failed_tasks > 0 and metrics.success_rate < 0.8:
            proposals.append(
                StrategyProposal(
                    proposal_id=f"prop-{uuid.uuid4().hex[:8]}",
                    title="Calibrate Repair Iteration Limit",
                    description="Increase default max repair iterations from 5 to 7 to allow multi-file fixes to converge.",
                    parameter_updates={"autonomy.max_iterations": 7},
                    justification=f"Current success rate is {metrics.success_rate * 100:.1f}%. Increasing iterations gives agent room to pass regression checks.",
                )
            )

        # If debounce storm detected or frequent runs
        if metrics.total_tasks > 10:
            proposals.append(
                StrategyProposal(
                    proposal_id=f"prop-{uuid.uuid4().hex[:8]}",
                    title="Optimize Watcher Debounce Window",
                    description="Extend watcher debounce window to 1500ms to reduce transient recompilations.",
                    parameter_updates={"watcher.debounce_ms": 1500},
                    justification="Historical volume justifies longer aggregation window to prevent event storms.",
                )
            )

        return proposals

    def verify_proposal(self, proposal: StrategyProposal) -> tuple[bool, str]:
        """Safety check to ensure proposal parameters remain within hard safety bounds."""
        updates = proposal.parameter_updates
        for k, v in updates.items():
            if k == "autonomy.max_iterations" and (not isinstance(v, int) or v > 15 or v < 1):
                return False, f"Iteration limit {v} outside safe bounds [1, 15]"
            if k == "watcher.debounce_ms" and (not isinstance(v, int) or v > 10000 or v < 100):
                return False, f"Debounce {v} outside safe bounds [100, 10000]"

        return True, "Proposal passed all safety constraint checks."

    def submit_for_approval(self, proposal: StrategyProposal) -> str:
        """Register an approval request in the database. Self-updates are strictly gated."""
        app_id = f"app-evo-{uuid.uuid4().hex[:8]}"
        now = datetime.utcnow().isoformat()

        # Insert placeholder system task to satisfy foreign key constraint
        task_id = self.db.insert_task({
            "project_id": "harness_system",
            "goal": f"Self-Evolution Proposal: {proposal.title}",
            "status": "REVIEW_REQUIRED",
        })

        self.db.connection.execute(
            """INSERT INTO approvals (id, task_id, action, status, requested_at)
               VALUES (?, ?, ?, 'PENDING', ?)""",
            (app_id, task_id, f"HARNESS_SELF_UPDATE: {proposal.title}", now),
        )
        self.db.connection.commit()
        return app_id

    def apply_approved_proposal(
        self, approval_id: str, proposal: StrategyProposal, config: BridgeConfig
    ) -> tuple[bool, str]:
        """Apply proposal changes if and only if approved by human."""
        cur = self.db.connection.execute(
            "SELECT status, resolved_by FROM approvals WHERE id = ?", (approval_id,)
        )
        row = cur.fetchone()
        if not row:
            return False, f"Approval record '{approval_id}' not found."
        if row["status"] != "APPROVED":
            return False, f"Cannot apply proposal: Approval status is '{row['status']}', not 'APPROVED'."

        for k, v in proposal.parameter_updates.items():
            parts = k.split(".")
            target_obj = config
            for p in parts[:-1]:
                target_obj = getattr(target_obj, p)
            setattr(target_obj, parts[-1], v)

        return True, f"Successfully applied strategy update approved by {row['resolved_by']}."
