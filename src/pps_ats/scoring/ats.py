"""ATS compatibility score (0-100). Kept separate from the CA1 faculty marks.

Only findings that affect machine parsing count: those flagged ``ats_impact`` (layout,
text boxes, garbled text, ...) plus configured rules for missing contact details or
sections that an ATS needs to fill a candidate profile.
"""

from __future__ import annotations

from collections import defaultdict

from pps_ats.config import ATSScoringSettings
from pps_ats.schemas.findings import Finding
from pps_ats.schemas.scoring import ATSScore, ContributionStatus, FindingContribution


def ats_band(score: float, cfg: ATSScoringSettings) -> str:
    for name, minimum in sorted(cfg.bands.items(), key=lambda kv: -kv[1]):
        if score >= minimum:
            return name
    return "poor"


def compute_ats(findings: list[Finding], cfg: ATSScoringSettings) -> ATSScore:
    per_rule: dict[str, float] = defaultdict(float)
    deductions: list[FindingContribution] = []
    for f in findings:
        if f.rule_id in cfg.rule_points:
            points = cfg.rule_points[f.rule_id]
        elif f.ats_impact:
            points = cfg.severity_points.get(f.severity.value, 0.0)
        else:
            continue
        points = round(points * f.confidence, 3)
        room = max(0.0, cfg.max_per_rule - per_rule[f.rule_id])
        applied = min(points, room)
        per_rule[f.rule_id] += applied
        deductions.append(
            FindingContribution(
                issue_id=f.issue_id,
                rule_id=f.rule_id,
                criterion="ats",
                group=f.category.value,
                points=points,
                applied_points=round(applied, 3),
                status=ContributionStatus.APPLIED if applied else ContributionStatus.RULE_CAP,
                reason=None if applied else f"{f.rule_id} already reached {cfg.max_per_rule}",
            )
        )
    score = max(0.0, round(cfg.base - sum(per_rule.values()), 1))
    return ATSScore(
        score=score, max_score=cfg.base, band=ats_band(score, cfg), deductions=deductions
    )
