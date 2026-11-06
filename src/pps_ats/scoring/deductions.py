"""Turn Milestone 2 findings into per-criterion deductions without double counting.

Pipeline (each step is recorded per finding in ``FindingContribution``):
  1. route    - criterion from the finding (``rubric_criterion``), optionally re-routed by
                rule config. ATS-only findings (criterion ``none``) never touch CA1.
  2. points   - rule points, else class x severity table; optionally x confidence.
  3. supersede- a section-level finding absorbs the line-level findings it summarises
                (e.g. SEC_PLACEHOLDER_ONLY covers the PH_* lines in that section).
  4. location - several findings of one group on the same line count once (the largest).
  5. rule cap - only ``max_count`` occurrences of a rule are counted.
  6. group cap- the total of a group per criterion is capped (``groups.<name>.max_points``).
"""

from __future__ import annotations

from collections import defaultdict
from fnmatch import fnmatch

from pps_ats.config import ScoringSettings
from pps_ats.schemas.findings import Finding, RubricCriterion
from pps_ats.schemas.scoring import ContributionStatus, FindingContribution, GroupDeduction

NO_CRITERION = RubricCriterion.NONE.value


def _round(value: float) -> float:
    return round(value + 0.0, 3)


def line_key(finding: Finding) -> str | None:
    if finding.span_id:
        return finding.span_id.rsplit(":s", 1)[0]
    return None


def _same_text(a: Finding, b: Finding) -> bool:
    """Two findings describe the same text (one detected text contains the other)."""
    ta, tb = (a.detected_text or "").lower(), (b.detected_text or "").lower()
    if not ta or not tb:
        return not ta and not tb
    return ta in tb or tb in ta


def nominal_points(finding: Finding, cfg: ScoringSettings) -> float:
    rule = cfg.rules.get(finding.rule_id)
    if rule is not None and rule.points is not None:
        points = rule.points
    else:
        points = cfg.class_points.get(finding.finding_class.value, {}).get(
            finding.severity.value, 0.0
        )
    if cfg.scale_by_confidence:
        points *= finding.confidence
    return _round(points)


def compute_deductions(
    findings: list[Finding], cfg: ScoringSettings
) -> tuple[list[FindingContribution], list[GroupDeduction]]:
    contributions: dict[str, FindingContribution] = {}
    candidates: list[Finding] = []
    for f in findings:
        rule = cfg.rules.get(f.rule_id)
        criterion = (rule.criterion if rule and rule.criterion else None) or (
            f.rubric_criterion.value
        )
        if criterion == NO_CRITERION or criterion not in cfg.criteria:
            continue  # ATS-only or identity findings: never part of CA1
        group = (rule.group if rule and rule.group else None) or f.category.value
        points = nominal_points(f, cfg)
        contributions[f.issue_id] = FindingContribution(
            issue_id=f.issue_id,
            rule_id=f.rule_id,
            criterion=criterion,
            group=group,
            points=points,
            status=ContributionStatus.APPLIED if points > 0 else ContributionStatus.ZERO,
            reason=None if points > 0 else "rule carries no points",
        )
        if points > 0:
            candidates.append(f)

    def absorb(f: Finding, status: ContributionStatus, reason: str) -> None:
        c = contributions[f.issue_id]
        c.status, c.reason = status, reason

    live = lambda f: contributions[f.issue_id].status is ContributionStatus.APPLIED  # noqa: E731

    # 3. section-level findings supersede the line-level ones they summarise.
    for cover in candidates:
        patterns = cfg.supersedes.get(cover.rule_id)
        if not patterns or not live(cover):
            continue
        for other in candidates:
            if other is cover or not live(other) or other.section != cover.section:
                continue
            if contributions[other.issue_id].criterion != contributions[cover.issue_id].criterion:
                continue
            if (
                cover.rule_id == "CQ_PROJECT_NO_DESCRIPTION"
                and other.rule_id.startswith("CQ_")
                and other.block_id != cover.block_id
            ):
                continue  # only findings about the same project entry
            if any(fnmatch(other.rule_id, p) for p in patterns):
                absorb(other, ContributionStatus.ABSORBED, f"covered by {cover.issue_id}")

    # 4. one deduction per group per text line.
    by_location: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for f in candidates:
        key = line_key(f)
        if key and live(f):
            c = contributions[f.issue_id]
            by_location[(c.criterion, c.group, key)].append(f)
    for group in by_location.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda f: (-contributions[f.issue_id].points, f.issue_id))
        kept: list[Finding] = []
        for f in group:
            twin = next((k for k in kept if _same_text(k, f)), None)
            if twin is None:
                kept.append(f)  # a different problem on the same line still counts
            else:
                absorb(
                    f,
                    ContributionStatus.ABSORBED,
                    f"same text already penalised by {twin.issue_id}",
                )

    # 5. per-rule occurrence caps.
    by_rule: dict[str, list[Finding]] = defaultdict(list)
    for f in candidates:
        if live(f):
            by_rule[f.rule_id].append(f)
    for rule_id, items in by_rule.items():
        rule = cfg.rules.get(rule_id)
        if rule is None or rule.max_count is None or len(items) <= rule.max_count:
            continue
        items.sort(key=lambda f: (-contributions[f.issue_id].points, f.issue_id))
        for f in items[rule.max_count :]:
            absorb(
                f,
                ContributionStatus.RULE_CAP,
                f"{rule_id} is counted at most {rule.max_count} times",
            )

    # 6. group caps per criterion.
    grouped: dict[tuple[str, str], list[FindingContribution]] = defaultdict(list)
    for c in contributions.values():
        if c.status is ContributionStatus.APPLIED:
            grouped[(c.criterion, c.group)].append(c)
    groups: list[GroupDeduction] = []
    for (criterion, group), items in sorted(grouped.items()):
        cap = cfg.groups[group].max_points if group in cfg.groups else cfg.default_group_cap
        raw = _round(sum(c.points for c in items))
        applied = min(raw, cap)
        scale = applied / raw if raw else 0.0
        for c in items:
            c.applied_points = _round(c.points * scale)
        groups.append(
            GroupDeduction(
                criterion=criterion,
                group=group,
                raw_points=raw,
                max_points=cap,
                applied_points=_round(applied),
                capped=raw > cap,
                issue_ids=[c.issue_id for c in items],
            )
        )
    return list(contributions.values()), groups
