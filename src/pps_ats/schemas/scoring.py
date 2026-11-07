"""Milestone 3: provisional CA1 rubric scores, ATS compatibility score, AI and faculty audit.

``ScoringResult`` is a separate, versioned document. It references the analysis by the
document hash and never changes the Milestone 1/2 results it was computed from.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

SCORING_SCHEMA_VERSION = "1.1.0"  # 1.1: AI evidence, observations, rejections


class ScoreStatus(StrEnum):
    NOT_GRADED = "not_graded"  # invalid / non-resume / unreadable: no automatic marks
    NEEDS_REVIEW = "needs_review"  # provisional marks exist but a human must check first
    PROVISIONAL = "provisional"  # deterministic marks, no review flag; awaiting sign-off
    FACULTY_REVIEWED = "faculty_reviewed"  # a faculty member approved / overrode / graded


class Band(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    NEEDS_IMPROVEMENT = "needs_improvement"


class ContributionStatus(StrEnum):
    APPLIED = "applied"  # counted (possibly reduced by a group cap)
    ABSORBED = "absorbed"  # duplicate of / covered by another finding
    RULE_CAP = "rule_cap"  # beyond the per-rule occurrence limit
    ZERO = "zero"  # the rule carries no points


class FindingContribution(BaseModel):
    issue_id: str
    rule_id: str
    criterion: str
    group: str
    points: float  # nominal points before group caps
    applied_points: float = 0.0  # after the group cap
    status: ContributionStatus
    reason: str | None = None


class GroupDeduction(BaseModel):
    criterion: str
    group: str
    raw_points: float
    max_points: float
    applied_points: float
    capped: bool
    issue_ids: list[str] = Field(default_factory=list)


class AIAdjustment(BaseModel):
    model_id: str
    proposed: float
    applied: float = 0.0
    confidence: float
    accepted: bool
    rationale: str = ""
    reason: str | None = None  # why it was clipped / rejected
    evidence: list[str] = Field(default_factory=list)  # verbatim quotes, verified in the text


class AIObservation(BaseModel):
    """A contextual observation from the optional AI. Advisory only: it never changes the
    deterministic findings, validity or marks. ``evidence`` is a verbatim quote that was
    verified to occur in the (redacted) document text."""

    aspect: str  # career_objective | project_description | professional_tone | contextual_grammar
    section: str | None = None
    evidence: str
    issue: str
    suggestion: str | None = None
    severity: str = "low"  # low | medium | high
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class FacultyOverride(BaseModel):
    reviewer: str
    reason: str
    previous_score: float | None
    new_score: float
    timestamp: str


class CriterionScore(BaseModel):
    criterion: str
    label: str
    max_points: float
    deterministic_score: float | None  # None when graded manually only
    deductions: float = 0.0
    ceiling: float
    ceiling_note: str | None = None
    ai: AIAdjustment | None = None
    provisional_score: float | None  # deterministic (+ accepted AI adjustment)
    override: FacultyOverride | None = None
    final_score: float  # override if present, else provisional
    band: Band
    near_band_boundary: bool = False
    review_required: bool = False
    rationale: list[str] = Field(default_factory=list)
    groups: list[GroupDeduction] = Field(default_factory=list)


class CA1Score(BaseModel):
    rubric_version: str
    criteria: list[CriterionScore]
    total: float
    max_total: float = 30.0


class ATSScore(BaseModel):
    """ATS compatibility (0-100). Independent of, and never added to, the CA1 marks."""

    score: float
    max_score: float = 100.0
    band: str
    deductions: list[FindingContribution] = Field(default_factory=list)


class AuditEntry(BaseModel):
    timestamp: str
    actor: str  # "system" | "ai:<model>" | "faculty:<id>"
    action: str  # "scored" | "ai_evaluated" | "override" | "manual_grade" | "approved"
    details: dict[str, str | float | int | bool | None | list[str]] = Field(default_factory=dict)


class ScoringResult(BaseModel):
    scoring_schema_version: str = SCORING_SCHEMA_VERSION
    analysis_schema_version: str
    engine_version: str
    generated_at: str
    config_source: str
    config_hash: str  # sha256 of the [scoring] settings: identical inputs => identical marks
    file_name: str
    document_sha256: str
    status: ScoreStatus
    releasable: bool  # may be exported as marks without further review
    review_reasons: list[str] = Field(default_factory=list)
    ca1: CA1Score | None = None
    ats: ATSScore | None = None
    contributions: list[FindingContribution] = Field(default_factory=list)
    ai_enabled: bool = False
    ai_model: str | None = None
    ai_observations: list[AIObservation] = Field(default_factory=list)
    ai_rejected: list[str] = Field(default_factory=list)  # unsupported claims, with reasons
    audit: list[AuditEntry] = Field(default_factory=list)
    note: str = (
        "Provisional marks from deterministic evidence. Faculty review is the final authority."
    )
