"""Resume vs non-resume classification from multiple structural signals.

Positive evidence: recognised resume section headings and contact details.
Negative evidence: phrases typical of grade cards, certificates, letters, reports;
very long documents; numeric-heavy (mark-sheet-like) text.
The filename is never used.
"""

from __future__ import annotations

import re

from pps_ats.analysis.sections import SectionAnalysis
from pps_ats.config import ClassifierSettings
from pps_ats.parsers.text_utils import normalize_for_match
from pps_ats.schemas.common import DocumentClass, SectionStatus
from pps_ats.schemas.content import ExtractedContent
from pps_ats.schemas.result import ClassificationResult

CORE_SECTIONS = ("education", "experience", "projects", "skills")
_STRUCTURE_WEIGHT = {
    SectionStatus.DETECTED: 1.0,
    SectionStatus.UNCERTAIN: 0.6,
    SectionStatus.EMPTY: 0.5,
    SectionStatus.MISSING: 0.0,
}


def _phrase_hits(text: str, phrases: dict[str, float]) -> dict[str, float]:
    norm = normalize_for_match(text)
    hits: dict[str, float] = {}
    for phrase, weight in phrases.items():
        pattern = r"(?<![a-z0-9])" + re.escape(normalize_for_match(phrase)) + r"(?![a-z0-9])"
        if re.search(pattern, norm):
            hits[phrase] = weight
    return hits


def classify(
    content: ExtractedContent,
    analysis: SectionAnalysis,
    cfg: ClassifierSettings,
    page_count: int | None,
) -> ClassificationResult:
    by_name = {s.name: s for s in analysis.sections}

    core_score = sum(_STRUCTURE_WEIGHT[by_name[n].status] for n in CORE_SECTIONS if n in by_name)
    other_score = min(
        2.0,
        sum(
            0.5 * _STRUCTURE_WEIGHT[s.status]
            for s in analysis.sections
            if s.name not in CORE_SECTIONS and s.name != "contact"
        ),
    )
    contact = analysis.contact
    contact_score = (
        (1.0 if contact.emails else 0.0)
        + (1.0 if contact.phones else 0.0)
        + (0.5 if contact.profile_urls else 0.0)
    )
    resume_score = round(core_score + other_score + contact_score, 2)

    text = f"{content.full_text}\n{content.header_footer_text}"
    hits = _phrase_hits(text, cfg.negative_phrases)
    non_resume_score = sum(hits.values())
    if page_count and page_count > cfg.max_resume_pages:
        non_resume_score += 1.5
    visible = [c for c in text if not c.isspace()]
    digit_ratio = sum(c.isdigit() for c in visible) / len(visible) if visible else 0.0
    if digit_ratio > 0.25:
        non_resume_score += 1.0
    non_resume_score = round(non_resume_score, 2)

    words = content.stats.word_count
    if resume_score >= cfg.resume_min_score and non_resume_score < cfg.nonresume_min_score:
        doc_class = DocumentClass.RESUME
    elif (
        # clear negative evidence (grade card, certificate, letter, report ...)
        non_resume_score >= cfg.nonresume_min_score and resume_score < cfg.resume_min_score
    ) or (
        # substantial text but essentially no resume structure or contact details
        resume_score < cfg.weak_resume_score
        and (non_resume_score > 0 or words >= cfg.substantial_words)
    ):
        doc_class = DocumentClass.NON_RESUME
    else:
        doc_class = DocumentClass.UNCERTAIN

    margin = abs(resume_score - non_resume_score)
    confidence = (
        0.0 if doc_class is DocumentClass.UNCERTAIN else round(min(0.99, 0.5 + margin / 10), 2)
    )

    return ClassificationResult(
        document_class=doc_class,
        confidence=confidence,
        resume_score=resume_score,
        non_resume_score=non_resume_score,
        signals={
            "core_section_score": round(core_score, 2),
            "other_section_score": round(other_score, 2),
            "contact_score": contact_score,
            "headings_found": analysis.headings_found,
            "negative_phrases": sorted(hits),
            "digit_ratio": round(digit_ratio, 3),
            "page_count": page_count if page_count is not None else -1,
            "word_count": words,
        },
    )
