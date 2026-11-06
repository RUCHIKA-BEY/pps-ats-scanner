"""Local spelling and basic grammar checks (no network, no external service).

Spelling: ``pyspellchecker`` frequency dictionary + a technology allow-list
(``resources/tech_terms.txt`` + ``[analysis.language] extra_allowed_words``).
* URLs, e-mails, handles, identifiers (digits, underscores, dotted names), CamelCase and
  ALL-CAPS tokens are excluded.
* Capitalised words inside a sentence are treated as proper nouns (names, institutions,
  cities) and are only flagged when they are a near-miss of a known technology name
  ("Pyhton" -> "Python").
* A capitalised word that starts a sentence-like line ("Devloped a web app") is checked
  against the dictionary.

Grammar: deterministic checks only (repeated words, lower-case "i", spacing around
punctuation). Contextual grammar and tone need an AI/LanguageTool stage (later milestone).
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from importlib import resources

from pps_ats.analysis.context import AnalysisContext, TextLine, locate, make_finding
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    RubricCriterion,
)

log = logging.getLogger(__name__)

LT = RubricCriterion.LANGUAGE_TONE
_MASK_RE = re.compile(
    r"https?://\S+|www\.\S+|\S+@\S+\.\S+|(?:linkedin|github|gitlab|behance|kaggle)\.com/\S*"
    r"|\b[\w-]+\.(?:com|org|net|io|in|edu|dev|ai|app|co)\b\S*",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’]*(?:-[A-Za-z][A-Za-z'’]*)*")
_IDENT_NEIGHBOUR = set("._/@#\\:+=")
_REPEAT_RE = re.compile(r"\b([A-Za-z]{2,})\s+\1\b", re.IGNORECASE)
_LOWER_I_RE = re.compile(r"(?:^|(?<=\s))i(?=\s|['’](?:m|ve|ll|d)\b)")
_SPACE_BEFORE_RE = re.compile(r"(?<=\w)\s+([,;])(?=\s|$)")
_NO_SPACE_AFTER_RE = re.compile(r"\b([a-z]{2,})([,;])([A-Za-z]{2,})\b")
_PROSE_SECTIONS = {"summary", "experience", "projects", "achievements", "activities"}


@lru_cache(maxsize=1)
def builtin_allowlist() -> tuple[frozenset[str], tuple[str, ...]]:
    text = resources.files("pps_ats").joinpath("resources/tech_terms.txt").read_text("utf-8")
    terms = [t.strip() for t in text.splitlines() if t.strip() and not t.startswith("#")]
    lowered: set[str] = set()
    for term in terms:
        lowered.add(term.lower())
        lowered.update(p.lower() for p in re.split(r"[\s./-]+", term) if p)
    return frozenset(lowered), tuple(terms)


@lru_cache(maxsize=2)
def _spellchecker(language: str):  # type: ignore[no-untyped-def]
    try:
        from spellchecker import SpellChecker
    except ImportError:  # optional at import time; reported as a skipped module
        return None
    return SpellChecker(language=language, distance=2)


def spelling_available(language: str = "en") -> bool:
    return _spellchecker(language) is not None


def _distance(a: str, b: str) -> int:
    """Optimal string alignment (Damerau-Levenshtein) distance."""
    a, b = a.lower(), b.lower()
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _masked(text: str) -> str:
    return _MASK_RE.sub(lambda m: " " * len(m.group(0)), text)


def _is_identifier(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    after2 = text[end + 1] if end + 1 < len(text) else " "
    before2 = text[start - 2] if start > 1 else " "
    if before.isdigit() or after.isdigit() or before == "_" or after == "_":
        return True
    if after in _IDENT_NEIGHBOUR and after2.isalnum():
        return True
    return before in _IDENT_NEIGHBOUR and before2.isalnum()


def _tech_suggestion(word: str, terms: tuple[str, ...]) -> str | None:
    best: tuple[int, str] | None = None
    limit = 1 if len(word) < 6 else 2
    for term in terms:
        if not term.isalpha() or abs(len(term) - len(word)) > limit or len(term) < 4:
            continue
        dist = _distance(word, term)
        if 0 < dist <= limit and (best is None or dist < best[0]):
            best = (dist, term)
    return best[1] if best else None


def check_spelling(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.language
    if not cfg.enabled:
        return []
    spell = _spellchecker(cfg.dictionary_language)
    if spell is None:
        raise RuntimeError("pyspellchecker is not installed; spelling check skipped")
    allow, terms = builtin_allowlist()
    allow = allow | {w.lower() for w in cfg.extra_allowed_words}
    skip_blocks = {ctx.cache.get("name_block_id")}
    placeholder_lines = ctx.cache.get("placeholder_lines", set())

    findings: list[Finding] = []
    flagged: set[str] = set()
    for tl in ctx.all_lines():
        if tl.section in cfg.skip_sections or tl.block_id in skip_blocks:
            continue
        if tl.is_heading and not tl.heading_remainder:
            continue
        if (tl.block_id, tl.line_index) in placeholder_lines:
            continue
        text = _masked(tl.text)
        tokens = list(_TOKEN_RE.finditer(text))
        for index, match in enumerate(tokens):
            word = match.group(0).strip("'’")
            if len(word) < cfg.min_word_length or _is_identifier(text, match.start(), match.end()):
                continue
            if word.isupper() or re.search(r"[a-z][A-Z]", word):
                continue  # acronyms, CamelCase identifiers
            for part in word.split("-"):
                part = re.sub(r"['’]s$", "", part)
                lower = part.lower()
                if len(part) < cfg.min_word_length or lower in allow or lower in flagged:
                    continue
                known = lower in spell
                capitalised = part[0].isupper()
                suggestion: str | None = None
                confidence = 0.0
                if capitalised:
                    sentence_start = index == 0 or text[: match.start()].rstrip().endswith(
                        (".", ":", "•", "-", "–")
                    )
                    next_lower = index + 1 < len(tokens) and tokens[index + 1].group(0)[0].islower()
                    tech = None if known else _tech_suggestion(part, terms)
                    if tech:
                        suggestion, confidence = tech, 0.8
                    elif sentence_start and next_lower and not known:
                        cand = spell.correction(lower)
                        if cand and cand != lower and _distance(lower, cand) == 1:
                            suggestion, confidence = cand.capitalize(), 0.6
                elif not known:
                    cand = spell.correction(lower)
                    if cand and cand != lower:
                        dist = _distance(lower, cand)
                        suggestion = cand
                        confidence = 0.85 if dist == 1 and len(lower) >= 5 else 0.6
                if suggestion is None:
                    continue
                flagged.add(lower)
                offset = match.start() + match.group(0).find(part)
                definite = confidence >= 0.85
                findings.append(
                    make_finding(
                        rule_id="LANG_SPELLING",
                        category=FindingCategory.SPELLING,
                        finding_class=FindingClass.DEFINITE_DEFECT
                        if definite
                        else FindingClass.POTENTIAL_ISSUE,
                        severity=FindingSeverity.MEDIUM if definite else FindingSeverity.LOW,
                        rubric=LT,
                        explanation=f"'{part}' appears to be misspelled.",
                        correction=f"Did you mean '{suggestion}'?",
                        method=DetectionMethod.DICTIONARY,
                        confidence=confidence,
                        detected_text=part,
                        location=locate(tl, offset, offset + len(part)),
                        section=tl.section,
                        evidence={"suggestion": suggestion, "line_text": tl.text[:160]},
                    )
                )
                if len(findings) >= cfg.max_findings:
                    return findings
    return findings


def _grammar_finding(
    rule: str,
    tl: TextLine,
    start: int,
    end: int,
    explanation: str,
    correction: str,
    cls: FindingClass,
    confidence: float,
) -> Finding:
    return make_finding(
        rule_id=rule,
        category=FindingCategory.GRAMMAR,
        finding_class=cls,
        severity=FindingSeverity.LOW,
        rubric=LT,
        explanation=explanation,
        correction=correction,
        method=DetectionMethod.PATTERN,
        confidence=confidence,
        detected_text=tl.text[start:end],
        location=locate(tl, start, end),
        section=tl.section,
        evidence={"line_text": tl.text[:160]},
    )


def check_grammar(ctx: AnalysisContext) -> list[Finding]:
    findings: list[Finding] = []
    placeholder_lines = ctx.cache.get("placeholder_lines", set())
    for tl in ctx.lines:
        if tl.is_heading or (tl.block_id, tl.line_index) in placeholder_lines:
            continue
        text = _masked(tl.text)
        for m in _REPEAT_RE.finditer(text):
            findings.append(
                _grammar_finding(
                    "LANG_REPEATED_WORD",
                    tl,
                    m.start(),
                    m.end(),
                    f"The word '{m.group(1)}' is repeated.",
                    f"Remove the duplicate '{m.group(1)}'.",
                    FindingClass.DEFINITE_DEFECT,
                    0.85,
                )
            )
        for m in _LOWER_I_RE.finditer(text):
            findings.append(
                _grammar_finding(
                    "LANG_LOWERCASE_I",
                    tl,
                    m.start(),
                    m.end(),
                    "The pronoun 'I' is written in lower case.",
                    "Write 'I' as a capital letter (or rephrase without first person).",
                    FindingClass.DEFINITE_DEFECT,
                    0.8,
                )
            )
        if tl.section in _PROSE_SECTIONS:
            for m in _SPACE_BEFORE_RE.finditer(text):
                findings.append(
                    _grammar_finding(
                        "LANG_SPACE_BEFORE_PUNCT",
                        tl,
                        max(0, m.start() - 1),
                        m.end(),
                        f"There is a space before '{m.group(1)}'.",
                        f"Remove the space before '{m.group(1)}'.",
                        FindingClass.RECOMMENDATION,
                        0.7,
                    )
                )
            for m in _NO_SPACE_AFTER_RE.finditer(text):
                findings.append(
                    _grammar_finding(
                        "LANG_NO_SPACE_AFTER_PUNCT",
                        tl,
                        m.start(),
                        m.end(),
                        f"No space after '{m.group(2)}' in '{m.group(0)}'.",
                        f"Add a space: '{m.group(1)}{m.group(2)} {m.group(3)}'.",
                        FindingClass.RECOMMENDATION,
                        0.7,
                    )
                )
    return findings
