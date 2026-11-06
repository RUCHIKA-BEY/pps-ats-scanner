"""Section completeness: present / missing / empty / placeholder-only / uncertain.

Builds on the Milestone 1 section statuses and the placeholder module's line map.
Only *required* sections produce defects when missing; *recommended* ones produce a
recommendation; *optional* ones are reported in the assessment only.
"""

from __future__ import annotations

from pps_ats.analysis.context import AnalysisContext, Location, make_finding
from pps_ats.schemas.common import SectionStatus
from pps_ats.schemas.findings import (
    CompletenessStatus,
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    LocationPrecision,
    RubricCriterion,
    SectionAssessment,
    SectionImportance,
)
from pps_ats.schemas.result import Section

DISPLAY_NAMES = {
    "summary": "Career Objective / Professional Summary",
    "education": "Education",
    "skills": "Skills",
    "projects": "Projects",
    "experience": "Experience / Internships",
    "achievements": "Achievements",
    "certifications": "Certifications",
    "contact": "Contact information",
    "activities": "Activities",
    "languages": "Languages",
    "interests": "Interests",
    "declaration": "Declaration",
    "references": "References",
}

_WHAT_TO_ADD = {
    "summary": "a short career objective stating your target role and strengths",
    "education": "your degree, institution, years and score",
    "skills": "your technical skills (languages, frameworks, tools)",
    "projects": "your projects, each with a title, a description and the technologies used",
    "experience": "internships or work experience with role, organisation and dates",
    "contact": "your e-mail address and phone number near the top of the resume",
}


def display(name: str) -> str:
    return DISPLAY_NAMES.get(name, name.title())


def _importance(ctx: AnalysisContext, name: str) -> SectionImportance:
    cfg = ctx.settings.analysis.completeness
    if name in cfg.required:
        return SectionImportance.REQUIRED
    if name in cfg.recommended:
        return SectionImportance.RECOMMENDED
    return SectionImportance.OPTIONAL


def _heading_location(section: Section) -> Location:
    if section.bbox is not None:
        return Location(
            section.page, section.bbox, LocationPrecision.LINE, section.heading_block_id
        )
    return Location(section.page, None, LocationPrecision.NONE, section.heading_block_id)


def assess_sections(ctx: AnalysisContext) -> tuple[list[SectionAssessment], list[Finding]]:
    cfg = ctx.settings.analysis.completeness
    placeholder_only = ctx.cache.get("placeholder_only_lines", set())
    detected = sum(
        1 for s in ctx.scan.sections if s.status is SectionStatus.DETECTED and s.name != "contact"
    )
    structure_recognised = detected >= cfg.min_detected_for_definite

    ordered = list(dict.fromkeys([*cfg.required, *cfg.recommended, *cfg.optional]))
    ordered += [s.name for s in ctx.scan.sections if s.name not in ordered]

    assessments: list[SectionAssessment] = []
    findings: list[Finding] = []
    for name in ordered:
        section = ctx.sections.get(name) or Section(name=name, status=SectionStatus.MISSING)
        importance = _importance(ctx, name)
        lines = ctx.section_lines(name)
        ph_lines = [t for t in lines if (t.block_id, t.line_index) in placeholder_only]
        real_lines = [t for t in lines if (t.block_id, t.line_index) not in placeholder_only]

        if section.status is SectionStatus.MISSING:
            status = CompletenessStatus.MISSING
        elif section.status is SectionStatus.EMPTY:
            status = (
                CompletenessStatus.PLACEHOLDER_ONLY
                if section.placeholder_hits or ph_lines
                else CompletenessStatus.EMPTY
            )
        elif lines and not real_lines:
            status = CompletenessStatus.PLACEHOLDER_ONLY
        elif section.status is SectionStatus.DETECTED:
            status = CompletenessStatus.PRESENT
        else:
            status = CompletenessStatus.UNCERTAIN

        assessments.append(
            SectionAssessment(
                name=name,
                importance=importance,
                status=status,
                scan_status=section.status.value,
                heading_text=section.heading_text,
                heading_block_id=section.heading_block_id,
                page=section.page,
                bbox=section.bbox,
                content_char_count=section.content_char_count,
                placeholder_lines=len(ph_lines),
                evidence=list(section.evidence),
            )
        )
        finding = _finding_for(ctx, section, status, importance, structure_recognised, detected)
        if finding is not None:
            findings.append(finding)
    return assessments, findings


def _finding_for(
    ctx: AnalysisContext,
    section: Section,
    status: CompletenessStatus,
    importance: SectionImportance,
    structure_recognised: bool,
    detected: int,
) -> Finding | None:
    name = section.name
    title = display(name)
    heading = section.heading_text or title
    location = _heading_location(section)
    required = importance is SectionImportance.REQUIRED
    rubric = (
        RubricCriterion.STRUCTURE_FORMATTING
        if name == "contact"
        else RubricCriterion.CONTENT_RELEVANCE
    )
    common = {
        "category": FindingCategory.SECTION_COMPLETENESS,
        "rubric": rubric,
        "section": name,
        "evidence": {
            "importance": importance.value,
            "scan_status": section.status.value,
            "scan_evidence": section.evidence[:5],
        },
    }

    if status is CompletenessStatus.MISSING:
        if name == "contact":
            return make_finding(
                rule_id="SEC_CONTACT_MISSING",
                finding_class=FindingClass.DEFINITE_DEFECT,
                severity=FindingSeverity.HIGH,
                explanation="No e-mail address or phone number was found in the document.",
                correction=f"Add {_WHAT_TO_ADD['contact']}.",
                method=DetectionMethod.PATTERN,
                confidence=0.85,
                ats_impact=True,
                **common,
            )
        if required:
            definite = structure_recognised
            return make_finding(
                rule_id="SEC_REQUIRED_MISSING",
                finding_class=FindingClass.DEFINITE_DEFECT
                if definite
                else FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.HIGH if definite else FindingSeverity.MEDIUM,
                explanation=(
                    f"No '{title}' section was found."
                    + (
                        ""
                        if definite
                        else f" Only {detected} section heading(s) were recognised, so the section "
                        "may exist under an unusual heading."
                    )
                ),
                correction=f"Add a '{title}' section with {_WHAT_TO_ADD.get(name, 'its details')}.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.8 if definite else 0.5,
                review_required=not definite,
                **common,
            )
        if importance is SectionImportance.RECOMMENDED:
            return make_finding(
                rule_id="SEC_RECOMMENDED_MISSING",
                finding_class=FindingClass.RECOMMENDATION,
                severity=FindingSeverity.LOW,
                explanation=f"The resume has no '{title}' section.",
                correction=f"Consider adding {_WHAT_TO_ADD.get(name, 'this section')}.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.7,
                **common,
            )
        return None  # absent optional sections are not defects

    if status is CompletenessStatus.EMPTY:
        return make_finding(
            rule_id="SEC_EMPTY",
            finding_class=FindingClass.DEFINITE_DEFECT,
            severity=FindingSeverity.HIGH if required else FindingSeverity.MEDIUM,
            explanation=f"The heading '{heading}' is present but no content follows it.",
            correction=(
                f"Add {_WHAT_TO_ADD.get(name, 'content')} under '{heading}', or remove the "
                "empty heading."
            ),
            method=DetectionMethod.STRUCTURE,
            confidence=0.9,
            detected_text=heading,
            location=location,
            **common,
        )

    if status is CompletenessStatus.PLACEHOLDER_ONLY:
        return make_finding(
            rule_id="SEC_PLACEHOLDER_ONLY",
            finding_class=FindingClass.DEFINITE_DEFECT,
            severity=FindingSeverity.HIGH,
            explanation=(f"The '{heading}' section contains only template placeholder text."),
            correction=(
                f"Replace the template text under '{heading}' with "
                f"{_WHAT_TO_ADD.get(name, 'your own content')}, or remove the section."
            ),
            method=DetectionMethod.PATTERN,
            confidence=0.9,
            detected_text=section.content_text or heading,
            location=location,
            **common,
        )

    if status is CompletenessStatus.UNCERTAIN and name != "contact":
        return make_finding(
            rule_id="SEC_UNCERTAIN",
            finding_class=FindingClass.POTENTIAL_ISSUE,
            severity=FindingSeverity.LOW,
            explanation=(
                f"The '{heading}' section could not be confirmed: "
                + (section.evidence[0] if section.evidence else "little content")
                + "."
            ),
            correction=(
                f"Use a clear, formatted '{title}' heading and add enough detail under it."
            ),
            method=DetectionMethod.STRUCTURE,
            confidence=0.5,
            detected_text=heading,
            location=location,
            **common,
        )
    return None
