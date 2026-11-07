"""Optional contextual evaluation interface (no model is bundled or called by default).

A ``ContextualEvaluator`` receives a redacted request (section texts, deterministic
scores, rubric descriptors, finding rule ids) and returns at most one bounded adjustment
per criterion with a confidence and rationale. The scoring engine - not the model -
decides whether an adjustment is applied (see ``[scoring.ai]``). Implementations must run
locally unless the institution explicitly approves otherwise.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, Field

from pps_ats.analysis.sections import EMAIL_RE, PHONE_CANDIDATE_RE
from pps_ats.schemas.scoring import AIObservation

ASPECTS = {
    "career_objective": "Is the career objective/summary clear, specific and relevant?",
    "project_description": "Do project descriptions state the problem, the student's own "
    "contribution, technologies and outcome?",
    "professional_tone": "Is the language professional (no slang, casual or exaggerated phrasing)?",
    "contextual_grammar": "Grammar or word-choice errors a spell checker cannot see "
    "(agreement, tense, wrong word).",
}

RUBRIC_DESCRIPTORS = {
    "content_relevance": {
        "excellent": "All key sections complete, accurate and tailored to the role/industry; "
        "clear goals and strengths.",
        "good": "Most sections present and relevant, but customisation or clarity is limited.",
        "needs_improvement": "Several sections missing, irrelevant or inaccurate.",
    },
    "structure_formatting": {
        "excellent": "Clean, professional layout with excellent alignment, spacing and balance.",
        "good": "Mostly consistent with minor formatting problems.",
        "needs_improvement": "Poor organisation, inconsistent fonts/layout or hard to read.",
    },
    "language_professional_tone": {
        "excellent": "Clear, concise, grammatically correct, strong action verbs, professional.",
        "good": "Minor grammar or tone issues.",
        "needs_improvement": "Frequent language issues or unprofessional tone.",
    },
}


class AIEvaluationRequest(BaseModel):
    rubric_version: str
    rubric: dict[str, dict[str, str]] = Field(default_factory=lambda: RUBRIC_DESCRIPTORS)
    sections: dict[str, str]  # canonical section -> (redacted) text
    deterministic_scores: dict[str, float]
    finding_rules: list[str]
    target_role: str | None = None  # optional JD / role for relevance (later milestone)
    aspects: dict[str, str] = Field(default_factory=lambda: dict(ASPECTS))


class CriterionProposal(BaseModel):
    criterion: str
    adjustment: float  # proposed change to the deterministic score, in marks
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)  # verbatim quotes from the sections


class AIEvaluationResponse(BaseModel):
    model_id: str
    proposals: list[CriterionProposal] = Field(default_factory=list)
    observations: list[AIObservation] = Field(default_factory=list)


class ContextualEvaluator(Protocol):
    model_id: str

    def evaluate(self, request: AIEvaluationRequest) -> AIEvaluationResponse: ...


class NullEvaluator:
    """Default: proposes nothing."""

    model_id = "none"

    def evaluate(self, request: AIEvaluationRequest) -> AIEvaluationResponse:
        return AIEvaluationResponse(model_id=self.model_id)


def redact(text: str, names: list[str], ids: list[str]) -> str:
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = PHONE_CANDIDATE_RE.sub(
        lambda m: "[PHONE]" if len(re.sub(r"\D", "", m.group(0))) >= 10 else m.group(0), text
    )
    text = re.sub(r"(?:linkedin|github)\.com/\S+", "[PROFILE_URL]", text, flags=re.IGNORECASE)
    for value in ids:
        if value:
            text = text.replace(value, "[STUDENT_ID]")
    for token in sorted(
        {t for n in names for t in n.split() if len(t) >= 2}, key=len, reverse=True
    ):
        text = re.sub(rf"\b{re.escape(token)}\b", "[NAME]", text, flags=re.IGNORECASE)
    return text


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def quote_found(quote: str, sections: dict[str, str], min_chars: int) -> bool:
    """True if ``quote`` occurs verbatim (whitespace/case-insensitive) in a section."""
    needle = _norm(quote.strip().strip('"').strip("'"))
    if len(needle) < min_chars:
        return False
    return any(needle in _norm(text) for text in sections.values())


def verify_response(
    response: AIEvaluationResponse,
    request: AIEvaluationRequest,
    criteria: set[str],
    min_chars: int,
    require_evidence: bool = True,
) -> tuple[list[CriterionProposal], list[AIObservation], list[str]]:
    """Keep only claims backed by verbatim evidence from the request text.

    Returns (proposals, observations, rejected reasons). A proposal keeps only its
    verified quotes; without any it is rejected. Unknown criteria/aspects are rejected.
    """
    rejected: list[str] = []
    proposals: list[CriterionProposal] = []
    seen: set[str] = set()
    for p in response.proposals:
        if p.criterion not in criteria:
            rejected.append(f"proposal for unknown criterion '{p.criterion}'")
            continue
        if p.criterion in seen:
            rejected.append(f"duplicate proposal for {p.criterion}")
            continue
        seen.add(p.criterion)
        good = [q for q in p.evidence if quote_found(q, request.sections, min_chars)]
        bad = len(p.evidence) - len(good)
        if bad:
            rejected.append(f"{p.criterion}: {bad} quote(s) not found in the document")
        if require_evidence and not good:
            rejected.append(
                f"{p.criterion}: adjustment {p.adjustment:+g} has no verifiable evidence"
            )
            continue
        proposals.append(p.model_copy(update={"evidence": good}))
    observations: list[AIObservation] = []
    for o in response.observations:
        if o.aspect not in request.aspects:
            rejected.append(f"observation with unknown aspect '{o.aspect}'")
        elif not quote_found(o.evidence, request.sections, min_chars):
            rejected.append(f"{o.aspect}: evidence not found in the document")
        else:
            observations.append(o)
    return proposals, observations, rejected
