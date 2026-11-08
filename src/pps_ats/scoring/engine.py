"""Provisional CA1 scoring from Milestone 2 findings (+ optional bounded AI adjustment).

Score per criterion = clamp(min(max_points - capped deductions, ceiling), floor, max).
* Nothing is scored for documents the analysis skipped (invalid, non-resume, unreadable).
* Documents with any review flag get provisional marks but status ``needs_review`` and
  ``releasable = false``; a faculty member must approve or override them.
* The same inputs and the same ``[scoring]`` config always give the same marks
  (``config_hash`` is stored with every result).
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from pps_ats import __version__
from pps_ats.analysis.context import ManifestRecord
from pps_ats.analysis.orchestrator import analyze_scan
from pps_ats.config import ScoringSettings, Settings, default_settings
from pps_ats.pipeline import scan_file
from pps_ats.schemas.findings import AnalysisResult
from pps_ats.schemas.result import ScanResult
from pps_ats.schemas.scoring import (
    AIAdjustment,
    AuditEntry,
    Band,
    CA1Score,
    ContributionStatus,
    CriterionScore,
    ScoreStatus,
    ScoringResult,
)
from pps_ats.scoring.ai import (
    AIEvaluationRequest,
    ContextualEvaluator,
    redact,
    verify_response,
)
from pps_ats.scoring.ats import compute_ats
from pps_ats.scoring.deductions import compute_deductions

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(math.floor(value / step + 0.5) * step, 2)


def config_hash(cfg: ScoringSettings) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()[:16]


def band_for(score: float, cfg: ScoringSettings) -> Band:
    for band in sorted(cfg.bands, key=lambda b: -b.min_score):
        if score >= band.min_score:
            return Band(band.name)
    return Band.NEEDS_IMPROVEMENT


def _near_boundary(value: float, cfg: ScoringSettings) -> bool:
    return any(
        b.min_score > 0 and abs(value - b.min_score) <= cfg.boundary_margin for b in cfg.bands
    )


def _ai_request(
    analysis: AnalysisResult, scan: ScanResult, cfg: ScoringSettings, scores: dict[str, float]
) -> AIEvaluationRequest:
    identity = analysis.identity
    names = (
        [identity.extracted.candidate_name]
        if identity and identity.extracted.candidate_name
        else []
    )
    ids = identity.extracted.student_ids if identity else []
    sections = {}
    for section in scan.sections:
        if section.content_text:
            text = section.content_text
            text = text[: cfg.ai.max_section_chars]
            sections[section.name] = (
                redact(text, names, ids) if cfg.ai.redact_personal_data else text
            )
    return AIEvaluationRequest(
        rubric_version=cfg.rubric_version,
        sections=sections,
        deterministic_scores=scores,
        finding_rules=sorted({f.rule_id for f in analysis.findings}),
    )


def score_analysis(
    analysis: AnalysisResult,
    settings: Settings | None = None,
    *,
    scan: ScanResult | None = None,
    evaluator: ContextualEvaluator | None = None,
) -> ScoringResult:
    settings = settings or default_settings()
    cfg = settings.scoring
    common = {
        "analysis_schema_version": analysis.analysis_schema_version,
        "engine_version": __version__,
        "generated_at": now_iso(),
        "config_source": settings.source,
        "config_hash": config_hash(cfg),
        "file_name": analysis.document.file_name,
        "document_sha256": analysis.document.sha256,
        "ai_enabled": cfg.ai.enabled,
    }

    if analysis.analysis_status == "skipped":
        reasons = analysis.review_reasons or [analysis.skipped_reason or "not analysed"]
        return ScoringResult(
            **common,
            status=ScoreStatus.NOT_GRADED,
            releasable=False,
            review_reasons=reasons,
            audit=[
                AuditEntry(
                    timestamp=common["generated_at"],
                    actor="system",
                    action="not_graded",
                    details={
                        "reason": analysis.skipped_reason,
                        "validation_status": analysis.validation.status.value,
                    },
                )
            ],
        )

    contributions, groups = compute_deductions(analysis.findings, cfg)
    findings_by_id = {f.issue_id: f for f in analysis.findings}
    criteria: list[CriterionScore] = []
    raw_values: dict[str, float] = {}
    review_reasons = list(analysis.review_reasons)

    for name, crit in cfg.criteria.items():
        crit_groups = [g for g in groups if g.criterion == name]
        deductions = round(sum(g.applied_points for g in crit_groups), 3)
        raw = crit.max_points - deductions
        capped = min(raw, crit.ceiling)
        raw_values[name] = max(crit.floor, capped)
        rationale = [
            f"{g.group}: -{g.applied_points:g} from {len(g.issue_ids)} finding(s)"
            + (f" (capped at {g.max_points:g}, raw {g.raw_points:g})" if g.capped else "")
            for g in crit_groups
        ]
        if not crit_groups:
            rationale.append("no deductions")
        ceiling_note = None
        if raw > crit.ceiling:
            ceiling_note = crit.ceiling_note or f"deterministic ceiling {crit.ceiling:g}"
        contributing = [
            c
            for c in contributions
            if c.criterion == name and c.status is ContributionStatus.APPLIED
        ]
        review = any(findings_by_id[c.issue_id].review_required for c in contributing)
        det = round_step(raw_values[name], cfg.rounding)
        criteria.append(
            CriterionScore(
                criterion=name,
                label=crit.label,
                max_points=crit.max_points,
                deterministic_score=det,
                deductions=deductions,
                ceiling=crit.ceiling,
                ceiling_note=ceiling_note,
                provisional_score=det,
                final_score=det,
                band=band_for(det, cfg),
                near_band_boundary=_near_boundary(raw_values[name], cfg),
                review_required=review,
                rationale=rationale,
                groups=crit_groups,
            )
        )

    audit = [
        AuditEntry(
            timestamp=common["generated_at"],
            actor="system",
            action="scored",
            details={
                "rubric_version": cfg.rubric_version,
                "config_hash": common["config_hash"],
                "deterministic_total": round(sum(c.final_score for c in criteria), 2),
            },
        )
    ]

    # Optional contextual evaluation: evidence-checked, bounded, confidence-gated, recorded.
    # The model never sees invalid/ungraded documents (returned above) and cannot change
    # validity, findings or the final (faculty) marks.
    ai_model: str | None = None
    ai_observations = []
    ai_rejected: list[str] = []
    if cfg.ai.enabled and evaluator is not None:
        if scan is None:
            review_reasons.append("AI_SKIPPED: no extracted sections supplied to the evaluator")
        else:
            try:
                request = _ai_request(
                    analysis, scan, cfg, {c.criterion: c.final_score for c in criteria}
                )
                response = evaluator.evaluate(request)
            except Exception as exc:  # the AI is optional: never block deterministic marks
                log.warning(
                    "ai evaluator failed file=%s error=%s",
                    analysis.document.file_name,
                    type(exc).__name__,
                )
                audit.append(
                    AuditEntry(
                        timestamp=now_iso(),
                        actor="system",
                        action="ai_failed",
                        details={"error": type(exc).__name__},
                    )
                )
            else:
                ai_model = response.model_id
                proposals, ai_observations, ai_rejected = verify_response(
                    response,
                    request,
                    set(cfg.criteria),
                    cfg.ai.min_evidence_chars,
                    cfg.ai.require_evidence,
                )
                by_name = {c.criterion: c for c in criteria}
                for proposal in proposals:
                    crit_score = by_name.get(proposal.criterion)
                    if crit_score is None:
                        continue
                    crit = cfg.criteria[proposal.criterion]
                    clipped = max(
                        -cfg.ai.max_adjustment, min(cfg.ai.max_adjustment, proposal.adjustment)
                    )
                    accepted = (
                        proposal.confidence >= cfg.ai.min_confidence and not cfg.ai.advisory_only
                    )
                    reason = None
                    if cfg.ai.advisory_only:
                        reason = "advisory only: recorded for faculty review, not applied"
                    elif not accepted:
                        reason = f"confidence {proposal.confidence:g} < {cfg.ai.min_confidence:g}"
                    elif clipped != proposal.adjustment:
                        reason = f"clipped to +/-{cfg.ai.max_adjustment:g}"
                    applied = clipped if accepted else 0.0
                    value = max(
                        crit.floor, min(crit.max_points, raw_values[proposal.criterion] + applied)
                    )
                    provisional = round_step(value, cfg.rounding)
                    crit_score.ai = AIAdjustment(
                        model_id=response.model_id,
                        proposed=proposal.adjustment,
                        applied=round(provisional - (crit_score.deterministic_score or 0), 2),
                        confidence=proposal.confidence,
                        accepted=accepted,
                        rationale=proposal.rationale[:500],
                        reason=reason,
                        evidence=[q[:300] for q in proposal.evidence[:5]],
                    )
                    crit_score.provisional_score = crit_score.final_score = provisional
                    crit_score.band = band_for(provisional, cfg)
                    crit_score.near_band_boundary = _near_boundary(value, cfg)
                    if abs(proposal.adjustment) >= cfg.ai.review_disagreement:
                        crit_score.review_required = True
                        review_reasons.append(
                            f"AI_DISAGREEMENT: {proposal.criterion} proposal "
                            f"{proposal.adjustment:+g} vs deterministic evidence"
                        )
                audit.append(
                    AuditEntry(
                        timestamp=now_iso(),
                        actor=f"ai:{response.model_id}",
                        action="ai_evaluated",
                        details={
                            "proposals": len(response.proposals),
                            "accepted": sum(1 for c in criteria if c.ai and c.ai.accepted),
                            "observations": len(ai_observations),
                            "rejected_claims": len(ai_rejected),
                        },
                    )
                )

    for c in criteria:
        if c.review_required:
            review_reasons.append(f"CRITERION_REVIEW: {c.criterion}")
    needs_review = analysis.review_required or any(c.review_required for c in criteria)
    status = ScoreStatus.NEEDS_REVIEW if needs_review else ScoreStatus.PROVISIONAL
    total = round(sum(c.final_score for c in criteria), 2)
    result = ScoringResult(
        **common,
        status=status,
        releasable=status is ScoreStatus.PROVISIONAL,
        review_reasons=list(dict.fromkeys(review_reasons)),
        ca1=CA1Score(
            rubric_version=cfg.rubric_version,
            criteria=criteria,
            total=total,
            max_total=sum(c.max_points for c in cfg.criteria.values()),
        ),
        ats=compute_ats(analysis.findings, cfg.ats),
        contributions=contributions,
        ai_model=ai_model,
        ai_observations=ai_observations,
        ai_rejected=ai_rejected,
        audit=audit,
    )
    log.info(
        "scored file=%s status=%s total=%s ats=%s",
        analysis.document.file_name,
        status.value,
        total,
        result.ats.score if result.ats else None,
    )
    return result


def score_file(
    path: str | Path,
    settings: Settings | None = None,
    manifest: dict[str, ManifestRecord] | None = None,
    evaluator: ContextualEvaluator | None = None,
) -> tuple[AnalysisResult, ScoringResult]:
    settings = settings or default_settings()
    if evaluator is None and settings.scoring.ai.enabled:
        from pps_ats.scoring.llm import build_evaluator  # None unless a provider is set

        evaluator = build_evaluator(settings.scoring.ai)
    path = Path(path)
    scan = scan_file(path, settings)
    record = manifest.get(path.name.lower()) if manifest else None
    analysis = analyze_scan(scan, settings, record)
    return analysis, score_analysis(analysis, settings, scan=scan, evaluator=evaluator)
