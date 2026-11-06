"""Faculty review: overrides, manual grades, approval, and an append-only audit log.

Every function returns a *new* ``ScoringResult``; the system's original marks stay in
``deterministic_score`` / ``provisional_score`` and every change is appended to
``audit``. ``AuditLog`` additionally writes a hash-chained JSONL file so edits to past
entries are detectable (``verify``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pps_ats.config import ScoringSettings, default_settings
from pps_ats.schemas.scoring import (
    AuditEntry,
    CA1Score,
    CriterionScore,
    FacultyOverride,
    ScoreStatus,
    ScoringResult,
)
from pps_ats.scoring.engine import band_for, now_iso, round_step

MIN_REASON_CHARS = 5


class ReviewError(ValueError):
    pass


def _check(reviewer: str, reason: str) -> None:
    if not reviewer.strip():
        raise ReviewError("reviewer id is required")
    if len(reason.strip()) < MIN_REASON_CHARS:
        raise ReviewError("a reason of at least 5 characters is required")


def _check_score(cfg: ScoringSettings, criterion: str, score: float) -> None:
    if criterion not in cfg.criteria:
        raise ReviewError(f"unknown criterion '{criterion}'")
    if not 0 <= score <= cfg.criteria[criterion].max_points:
        raise ReviewError(f"score must be between 0 and {cfg.criteria[criterion].max_points:g}")


def _finalise(result: ScoringResult, cfg: ScoringSettings) -> None:
    assert result.ca1 is not None
    result.ca1.total = round(sum(c.final_score for c in result.ca1.criteria), 2)
    result.status = ScoreStatus.FACULTY_REVIEWED
    result.releasable = True


def apply_override(
    result: ScoringResult,
    criterion: str,
    new_score: float,
    reviewer: str,
    reason: str,
    cfg: ScoringSettings | None = None,
) -> ScoringResult:
    cfg = cfg or default_settings().scoring
    _check(reviewer, reason)
    _check_score(cfg, criterion, new_score)
    if result.ca1 is None:
        raise ReviewError("document was not graded automatically; use manual_grade")
    updated = result.model_copy(deep=True)
    assert updated.ca1 is not None
    target = next((c for c in updated.ca1.criteria if c.criterion == criterion), None)
    if target is None:
        raise ReviewError(f"criterion '{criterion}' not in this result")
    timestamp = now_iso()
    previous = target.final_score
    target.override = FacultyOverride(
        reviewer=reviewer,
        reason=reason.strip(),
        previous_score=previous,
        new_score=new_score,
        timestamp=timestamp,
    )
    target.final_score = round_step(new_score, cfg.rounding)
    target.band = band_for(target.final_score, cfg)
    target.review_required = False
    _finalise(updated, cfg)
    updated.audit.append(
        AuditEntry(
            timestamp=timestamp,
            actor=f"faculty:{reviewer}",
            action="override",
            details={
                "criterion": criterion,
                "previous": previous,
                "new": target.final_score,
                "reason": reason.strip(),
                "total": updated.ca1.total,
            },
        )
    )
    return updated


def manual_grade(
    result: ScoringResult,
    scores: dict[str, float],
    reviewer: str,
    reason: str,
    cfg: ScoringSettings | None = None,
) -> ScoringResult:
    """Grade a document the system did not grade (e.g. a scanned resume after review)."""
    cfg = cfg or default_settings().scoring
    _check(reviewer, reason)
    if set(scores) != set(cfg.criteria):
        raise ReviewError(f"scores are required for all criteria: {sorted(cfg.criteria)}")
    for name, value in scores.items():
        _check_score(cfg, name, value)
    timestamp = now_iso()
    criteria = []
    for name, crit in cfg.criteria.items():
        value = round_step(scores[name], cfg.rounding)
        criteria.append(
            CriterionScore(
                criterion=name,
                label=crit.label,
                max_points=crit.max_points,
                deterministic_score=None,
                ceiling=crit.max_points,
                provisional_score=None,
                override=FacultyOverride(
                    reviewer=reviewer,
                    reason=reason.strip(),
                    previous_score=None,
                    new_score=value,
                    timestamp=timestamp,
                ),
                final_score=value,
                band=band_for(value, cfg),
                rationale=["graded manually by faculty"],
            )
        )
    updated = result.model_copy(deep=True)
    updated.ca1 = CA1Score(
        rubric_version=cfg.rubric_version,
        criteria=criteria,
        total=0,
        max_total=sum(c.max_points for c in cfg.criteria.values()),
    )
    _finalise(updated, cfg)
    updated.audit.append(
        AuditEntry(
            timestamp=timestamp,
            actor=f"faculty:{reviewer}",
            action="manual_grade",
            details={
                "reason": reason.strip(),
                "total": updated.ca1.total,
                "previous_status": result.status.value,
            },
        )
    )
    return updated


def approve(
    result: ScoringResult, reviewer: str, note: str, cfg: ScoringSettings | None = None
) -> ScoringResult:
    """Accept the provisional marks as they are (resolves ``needs_review``)."""
    cfg = cfg or default_settings().scoring
    _check(reviewer, note)
    if result.ca1 is None:
        raise ReviewError("nothing to approve; use manual_grade")
    updated = result.model_copy(deep=True)
    for c in updated.ca1.criteria:  # type: ignore[union-attr]
        c.review_required = False
    _finalise(updated, cfg)
    updated.audit.append(
        AuditEntry(
            timestamp=now_iso(),
            actor=f"faculty:{reviewer}",
            action="approved",
            details={
                "note": note.strip(),
                "total": updated.ca1.total,  # type: ignore[union-attr]
                "previous_status": result.status.value,
            },
        )
    )
    return updated


class AuditLog:
    """Append-only JSONL with a SHA-256 hash chain. Keep it outside Git (``audit/``)."""

    GENESIS = "0" * 64

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return self.GENESIS
        last = self.GENESIS
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = json.loads(line)["entry_hash"]
        return last

    @staticmethod
    def _digest(prev: str, record: dict) -> str:
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()

    def append(self, result: ScoringResult, entry: AuditEntry) -> str:
        record = {
            "file_name": result.file_name,
            "document_sha256": result.document_sha256,
            "rubric_version": result.ca1.rubric_version if result.ca1 else None,
            "config_hash": result.config_hash,
            **entry.model_dump(mode="json"),
        }
        prev = self._last_hash()
        line = {**record, "prev_hash": prev, "entry_hash": self._digest(prev, record)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return line["entry_hash"]

    def verify(self) -> int | None:
        """Index of the first broken entry, or None if the chain is intact."""
        if not self.path.exists():
            return None
        prev = self.GENESIS
        with self.path.open(encoding="utf-8") as handle:
            for index, raw in enumerate(row for row in handle if row.strip()):
                line = json.loads(raw)
                record = {k: v for k, v in line.items() if k not in ("prev_hash", "entry_hash")}
                if line["prev_hash"] != prev or line["entry_hash"] != self._digest(prev, record):
                    return index
                prev = line["entry_hash"]
        return None
