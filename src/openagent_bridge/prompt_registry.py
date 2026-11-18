"""Prompt Registry, versioning, and promotion policy for OpenAgent Bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GoalSpec:
    """Immutable user intent specification."""

    original_goal: str
    constraints: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)


@dataclass
class PromptTemplate:
    """Versioned template for guiding coding agents."""

    template_id: str
    version: int
    parent_version: int | None
    content: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "EXPERIMENTAL"  # EXPERIMENTAL | CHAMPION | RETIRED | REJECTED


@dataclass
class RunPrompt:
    """A rendered prompt instance for a specific task run."""

    template_id: str
    version: int
    rendered_text: str
    task_id: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class PromotionEvaluation:
    template_id: str
    champion_version: int
    challenger_version: int
    champion_score: float
    challenger_score: float
    critical_regressions: int
    security_regressions: int
    holdout_degradation: float
    total_cases: int
    eligible_for_promotion: bool
    requires_human_approval: bool = True
    reason: str = ""


class PromptRegistry:
    """Manages versioned prompts and enforces champion/challenger promotion guardrails."""

    def __init__(self, root_dir: Path | str | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else Path.cwd() / "prompts"
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def get_template_dir(self, template_id: str) -> Path:
        p = self.root_dir / template_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_template(self, template: PromptTemplate) -> Path:
        tdir = self.get_template_dir(template.template_id)
        fname = f"v{template.version:03d}.txt"
        file_path = tdir / fname
        file_path.write_text(template.content, encoding="utf-8")

        # Update metadata.json
        meta_file = tdir / "metadata.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        meta[f"v{template.version:03d}"] = {
            "template_id": template.template_id,
            "version": template.version,
            "parent_version": template.parent_version,
            "status": template.status,
            "created_at": template.created_at,
        }
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return file_path

    def get_champion(self, template_id: str) -> PromptTemplate | None:
        tdir = self.get_template_dir(template_id)
        meta_file = tdir / "metadata.json"
        if not meta_file.exists():
            return None

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        for ver_key, info in meta.items():
            if info.get("status") == "CHAMPION":
                content_file = tdir / f"{ver_key}.txt"
                if content_file.exists():
                    return PromptTemplate(
                        template_id=template_id,
                        version=info["version"],
                        parent_version=info.get("parent_version"),
                        content=content_file.read_text(encoding="utf-8"),
                        created_at=info.get("created_at", ""),
                        status="CHAMPION",
                    )
        return None

    def evaluate_promotion(
        self,
        template_id: str,
        champion_version: int,
        challenger_version: int,
        champion_score: float,
        challenger_score: float,
        critical_regressions: int,
        security_regressions: int,
        holdout_degradation: float,
        total_cases: int,
    ) -> PromotionEvaluation:
        """Enforce strict promotion rules for prompt candidates."""
        reasons = []
        eligible = True

        if critical_regressions > 0:
            eligible = False
            reasons.append(f"Critical regressions detected: {critical_regressions}")

        if security_regressions > 0:
            eligible = False
            reasons.append(f"Security regressions detected: {security_regressions}")

        score_diff = challenger_score - champion_score
        if score_diff < 0.03:
            eligible = False
            reasons.append(f"Score improvement ({score_diff:.3f}) is below minimum threshold (+0.03)")

        if holdout_degradation > 0.01:
            eligible = False
            reasons.append(f"Holdout degradation ({holdout_degradation:.3f}) exceeds tolerance (0.01)")

        if total_cases < 20:
            eligible = False
            reasons.append(f"Total benchmark cases ({total_cases}) is below minimum threshold (20)")

        reason_str = "; ".join(reasons) if reasons else "Eligible for human promotion"

        return PromotionEvaluation(
            template_id=template_id,
            champion_version=champion_version,
            challenger_version=challenger_version,
            champion_score=champion_score,
            challenger_score=challenger_score,
            critical_regressions=critical_regressions,
            security_regressions=security_regressions,
            holdout_degradation=holdout_degradation,
            total_cases=total_cases,
            eligible_for_promotion=eligible,
            requires_human_approval=True,
            reason=reason_str,
        )

    def promote(self, template_id: str, version: int, approved_by: str) -> bool:
        """Promote a challenger to champion after human approval."""
        tdir = self.get_template_dir(template_id)
        meta_file = tdir / "metadata.json"
        if not meta_file.exists():
            return False

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        target_key = f"v{version:03d}"
        if target_key not in meta:
            return False

        # Demote current champion
        for k, v in meta.items():
            if v.get("status") == "CHAMPION":
                v["status"] = "RETIRED"

        # Promote target
        meta[target_key]["status"] = "CHAMPION"
        meta[target_key]["promoted_at"] = datetime.now(timezone.utc).isoformat()
        meta[target_key]["approved_by"] = approved_by

        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return True
