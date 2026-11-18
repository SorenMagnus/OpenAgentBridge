"""Versioned task contract shared by initial execution and repair."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openagent_bridge.budget import BudgetLimits
from openagent_bridge.prompt_registry import PromptRegistry


class TaskPromptRenderer:
    @classmethod
    def for_execution(
        cls,
        task: dict[str, Any],
        limits: BudgetLimits,
        *,
        data_dir: Path,
        repo_path: Path | None,
        worktree: Path,
        policy: dict[str, Any],
    ) -> str:
        champion = PromptRegistry(data_dir / "prompts").get_champion("coding_task")
        return cls.render(
            task,
            limits,
            champion.content if champion else "",
            repo_path=repo_path,
            policy=policy,
            worktree=worktree,
            guidance_version=champion.version if champion else None,
        )

    @staticmethod
    def render(
        task: dict[str, Any],
        limits: BudgetLimits,
        guidance: str = "",
        *,
        repo_path: Path | None = None,
        worktree: Path | None = None,
        guidance_version: int | None = None,
        policy: dict[str, Any] | None = None,
    ) -> str:
        contract = {
            "schema_version": 1,
            "goal": task.get("original_goal") or task.get("goal", ""),
            "repository": {
                "project_id": task.get("project_id"),
                "path": str(repo_path) if repo_path else None,
                "worktree": str(worktree) if worktree else task.get("worktree_path"),
                "branch": task.get("branch"),
                "base_commit": task.get("base_commit"),
            },
            "effective_budget": asdict(limits),
            "administrator_policy": policy or {},
            "guidance_version": guidance_version,
            "evidence_requirements": (
                "Record changed files, checks run, observed results, and unverified criteria. "
                "Provide paths for relevant artifacts and logs."
            ),
            "stop_conditions": (
                "Stop at budget limits, missing permissions, or completion. "
                "Work within approvals, administrator policy, and task permissions."
            ),
        }
        for field, default in (
            ("constraints", []), ("non_goals", []), ("permissions", {}), ("acceptance", {})
        ):
            value = task.get(field, default)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass  # Preserve free-text requirements in historical tasks.
            contract[field] = value

        prompt = "OBJECTIVE (IMMUTABLE) – Task Contract v1\n" + json.dumps(
            contract, ensure_ascii=False, sort_keys=True, indent=2
        )
        if guidance:
            revision = f" v{guidance_version}" if guidance_version is not None else ""
            prompt += (
                f"\n\nCODING GUIDANCE{revision}\n"
                "Apply this guidance within the task contract above.\n" + guidance
            )
        return prompt
