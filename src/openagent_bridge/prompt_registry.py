"""Prompt Registry, versioning, and promotion policy for OpenAgent Bridge."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from functools import wraps
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
    status: str = "EXPERIMENTAL"
    training_goals: tuple[str, ...] = ()


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


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def goal_digest(text: str) -> str:
    return digest(" ".join(text.casefold().split()))


def _serialized(method):
    """Serialize local registry writers across CLI processes."""
    @wraps(method)
    def write(self, *args, **kwargs):
        conn = sqlite3.connect(self.root_dir / ".registry.lock.db")
        try:
            conn.execute("BEGIN IMMEDIATE")
            return method(self, *args, **kwargs)
        finally:
            conn.close()
    return write


def _write_metadata(path: Path, metadata: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    tmp.replace(path)


class PromptRegistry:
    """Manages versioned prompts and enforces champion/challenger promotion guardrails."""

    def __init__(self, root_dir: Path | str | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else Path.cwd() / "prompts"
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def get_template_dir(self, template_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", template_id):
            raise ValueError("Invalid template ID")
        p = self.root_dir / template_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    @_serialized
    def save_template(self, template: PromptTemplate) -> Path:
        if type(template.version) is not int or template.version < 1:
            raise ValueError("Prompt version must be a positive integer")
        tdir = self.get_template_dir(template.template_id)
        fname = f"v{template.version:03d}.txt"
        file_path = tdir / fname
        # A version is visible only after the atomic metadata update.
        meta_file = tdir / "metadata.json"
        meta = {}
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))

        if f"v{template.version:03d}" in meta:
            raise ValueError("Prompt versions are immutable")
        if template.status == "CHAMPION" and (meta or template.parent_version is not None):
            raise ValueError("Only the initial baseline may bootstrap a champion")
        if template.status not in ("CHAMPION", "EXPERIMENTAL", "CHALLENGER"):
            raise ValueError("Invalid initial status")
        file_path.write_bytes(template.content.encode("utf-8"))
        meta[f"v{template.version:03d}"] = {
            "template_id": template.template_id,
            "version": template.version,
            "parent_version": template.parent_version,
            "status": template.status,
            "created_at": template.created_at,
            "content_sha256": digest(template.content),
            "training_goals": template.training_goals,
        }
        _write_metadata(meta_file, meta)
        return file_path

    def get_champion(self, template_id: str) -> PromptTemplate | None:
        tdir = self.get_template_dir(template_id)
        meta_file = tdir / "metadata.json"
        if not meta_file.exists():
            return None

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        for ver_key, info in meta.items():
            if info.get("status") == "CHAMPION":
                return self._read_template(tdir, ver_key, info)
        return None

    def get_template(self, template_id: str, version: int) -> PromptTemplate:
        tdir = self.get_template_dir(template_id)
        key = f"v{version:03d}"
        metadata = json.loads((tdir / "metadata.json").read_text(encoding="utf-8"))
        if key not in metadata:
            raise ValueError(f"Prompt version {version} was not found")
        return self._read_template(tdir, key, metadata[key])

    @staticmethod
    def _read_template(tdir: Path, key: str, info: dict) -> PromptTemplate:
        content = (tdir / f"{key}.txt").read_bytes().decode("utf-8")
        stored_hash = info.get("content_sha256")
        if stored_hash is not None and stored_hash != digest(content):
            raise ValueError(f"Prompt {tdir.name}/{key} changed outside the registry")
        return PromptTemplate(
            template_id=info["template_id"],
            version=info["version"],
            parent_version=info.get("parent_version"),
            content=content,
            created_at=info.get("created_at", ""),
            status=info["status"],
            training_goals=tuple(info.get("training_goals", ())),
        )

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
        if not math.isfinite(score_diff) or score_diff < 0.03 - 1e-12:
            eligible = False
            reasons.append(f"Score improvement ({score_diff:.3f}) is below minimum threshold (+0.03)")

        if not math.isfinite(holdout_degradation) or holdout_degradation > 0.01 + 1e-12:
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

    @_serialized
    def record_review(self, evaluation: PromotionEvaluation, evidence: dict) -> None:
        path = self.get_template_dir(evaluation.template_id) / "metadata.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        key = f"v{evaluation.challenger_version:03d}"
        entry = meta[key]
        champion = self.get_champion(evaluation.template_id)
        candidate = self._read_template(path.parent, key, entry)
        if not champion or champion.version != evaluation.champion_version or candidate.parent_version != champion.version:
            raise ValueError("Candidate must descend from the current champion")
        if evidence["champion_sha256"] != digest(champion.content) or evidence["challenger_sha256"] != digest(candidate.content):
            raise ValueError("Review evidence does not match the stored prompt versions")
        if entry["status"] not in ("CHALLENGER", "EXPERIMENTAL", "READY_FOR_REVIEW"):
            raise ValueError("Only a challenger may enter review")
        entry["evaluation"] = asdict(evaluation)
        entry["evidence"] = evidence
        entry["evaluated_at"] = datetime.now(timezone.utc).isoformat()
        entry["status"] = "READY_FOR_REVIEW" if evaluation.eligible_for_promotion else "CHALLENGER"
        _write_metadata(path, meta)

    @_serialized
    def promote(self, template_id: str, version: int, approved_by: str) -> bool:
        """Promote a challenger to champion after human approval."""
        tdir = self.get_template_dir(template_id)
        meta_file = tdir / "metadata.json"
        if not meta_file.exists():
            return False

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        target_key = f"v{version:03d}"
        if target_key not in meta or not approved_by.strip():
            return False
        entry = meta[target_key]
        current = self.get_champion(template_id)
        evaluation = entry.get("evaluation", {})
        if (
            entry["status"] != "READY_FOR_REVIEW" or not current
            or current.version != evaluation.get("champion_version")
            or not evaluation.get("eligible_for_promotion")
        ):
            return False
        candidate = self._read_template(tdir, target_key, entry)
        evidence = entry.get("evidence", {})
        if (
            evidence.get("champion_sha256") != digest(current.content)
            or evidence.get("challenger_sha256") != digest(candidate.content)
        ):
            return False

        # Demote current champion
        for k, v in meta.items():
            if v.get("status") == "CHAMPION":
                v["status"] = "RETIRED"

        # Promote target
        meta[target_key]["status"] = "CHAMPION"
        meta[target_key]["promoted_at"] = datetime.now(timezone.utc).isoformat()
        meta[target_key]["approved_by"] = approved_by

        _write_metadata(meta_file, meta)
        return True
