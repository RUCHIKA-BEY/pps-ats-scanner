"""Deterministic content-quality checks.

Definite defects: a project with a title but no description, duplicated lines.
Potential issues: vague descriptions, education entries missing year/institution,
a very short or cut-off career objective, near-duplicate lines.
Recommendations: missing technologies / individual contribution, weak opening verbs,
filler phrases, certification issuer/date, missing scores.

Nothing here invents content: suggestions say *what* is missing, never *what to write*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pps_ats.analysis.context import AnalysisContext, TextLine, locate, make_finding
from pps_ats.analysis.language import builtin_allowlist
from pps_ats.analysis.placeholders import core_text
from pps_ats.parsers.text_utils import normalize_for_match
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    RubricCriterion,
)

CR = RubricCriterion.CONTENT_RELEVANCE
LT = RubricCriterion.LANGUAGE_TONE
_INLINE_TITLE_RE = re.compile(r"^(?P<title>[A-Z0-9][^:–—|]{2,80}?)\s*[-–—|:]\s+(?P<rest>\S.*)$")
_YEAR_RE = re.compile(
    r"\b(?:19|20)\d{2}\b|\b(?:present|pursuing|ongoing|expected|current)\b", re.IGNORECASE
)
_MONTH_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*(?:19|20)\d{2}\b",
    re.IGNORECASE,
)
_SCORE_RE = re.compile(
    r"\b(?:c?gpa|sgpa|cpi|spi|percent(?:age)?|grade|marks|score)\b|%|\b\d{1,2}(?:\.\d{1,2})?\s*/\s*10\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z+#.'-]*")
_LABEL_ONLY_RE = re.compile(r"^[A-Za-z ]{2,30}:\s*$")


_NAMED_TECH_RE = re.compile(r"^(?:[A-Z][A-Za-z0-9.+#-]*|[a-z]+[0-9.+#][\w.+#-]*)$")
_OUTCOME_NUMBER_RE = re.compile(
    r"\d[\d,.]*\s?(?:%|x\b|\+|k\b|users|students|members|downloads|customers|visitors|"
    r"ms\b|seconds|secs?\b|minutes|mins?\b|hours|hrs?\b|days|accuracy|rank|records|"
    r"requests|queries|transactions)",
    re.IGNORECASE,
)


def _term_in(text: str, term: str) -> bool:
    term = term.strip().casefold()
    return bool(term) and re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None


def _named_tech_after(full: str, marker: str, tech_terms: set[str]) -> bool:
    """A generic marker ('using', 'tools') followed within 3 words by a named
    technology (a known tech term or a proper-noun-like token such as 'Flask', 'C++')."""
    for m in re.finditer(rf"(?<![a-z]){re.escape(marker.strip())}", full.casefold()):
        following = re.split(r"[\s,/()|]+", full[m.end() :].strip())[:3]
        for token in following:
            word = token.strip(".,;:")
            if not word:
                continue
            if word.casefold() in tech_terms or _NAMED_TECH_RE.match(word):
                return True
    return False


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _letters(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = phrase.lower()
    if phrase.endswith(" "):  # markers like "i " / "led " must start a word
        return re.search(rf"(?<![a-z]){re.escape(phrase)}", text) is not None
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


@dataclass
class Entry:
    lines: list[TextLine]
    title: str
    inline_rest: str = ""
    keys: list[str] = field(default_factory=list)
    extra_text: str = ""  # table values that are not a description (e.g. technologies)

    @property
    def first(self) -> TextLine:
        return self.lines[0]

    @property
    def text(self) -> str:
        return " ".join(t.content_text for t in self.lines)


def _key(tl: TextLine) -> tuple[str, int]:
    return (tl.block_id, tl.line_index)


def _is_entry_header(
    tl: TextLine, nxt: TextLine | None, labels: frozenset[str] = frozenset()
) -> tuple[bool, str, str]:
    text = tl.body_text if tl.is_bullet else tl.text
    if not text or not text[0].isupper() and not text[0].isdigit():
        return False, "", ""
    if text.rstrip(" :").lower() in labels or _LABEL_ONLY_RE.match(text):
        return False, "", ""  # a field label such as "Description:" belongs to the entry
    inline = _INLINE_TITLE_RE.match(text)
    if inline and inline.group("title").strip(" :").lower() in labels:
        return False, "", ""  # "Technologies: React", "My role: ..." are fields of the entry
    if (
        inline
        and len(inline.group("title").split()) <= 8
        and len(_words(inline.group("rest"))) >= 2
    ):
        return True, inline.group("title").strip(), inline.group("rest").strip()
    if tl.is_bullet:
        return False, "", ""
    words = text.split()
    if tl.bold_ratio >= 0.6 and len(words) <= 14:
        return True, text.rstrip(":"), ""
    if (
        len(words) <= 8
        and not text.rstrip().endswith((".", ",", ";"))
        and nxt is not None
        and (nxt.is_bullet or _LABEL_ONLY_RE.match(nxt.text) or nxt.text[:1].isupper())
    ):
        return True, text.rstrip(":"), ""
    return False, "", ""


def split_entries(lines: list[TextLine], labels: frozenset[str] = frozenset()) -> list[Entry]:
    entries: list[Entry] = []
    for i, tl in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        is_header, title, rest = _is_entry_header(tl, nxt, labels)
        if tl.is_heading:  # "Projects: X" inline remainder
            is_header, title, rest = True, tl.heading_remainder, ""
        if is_header or not entries:
            entries.append(Entry(lines=[tl], title=title or tl.text, inline_rest=rest))
        else:
            entries[-1].lines.append(tl)
    return entries


_DESCRIPTION_LABELS = {
    "description",
    "project description",
    "responsibilities",
    "key responsibilities",
    "outcome",
    "result",
    "details",
    "summary",
}
_TITLE_LABELS = {"project title", "project name", "title", "name", "project"}


def _table_entries(ctx: AnalysisContext, lines: list[TextLine]) -> list[Entry]:
    """Projects laid out as label/value tables: one entry per 'Project Title' row."""
    entries: list[Entry] = []
    rows: dict[tuple[str, int], list[TextLine]] = {}
    for tl in lines:
        row = tl.block.table_cell[0] if tl.block.table_cell else 0
        rows.setdefault((tl.block.table_id or "", row), []).append(tl)
    for (_table, _row), cells in rows.items():
        cells.sort(key=lambda t: t.block.table_cell[1] if t.block.table_cell else 0)
        label = cells[0].text.strip().rstrip(":").lower()
        value = " ".join(t.text for t in cells[1:]).strip()
        if len(cells) >= 2 and label in _TITLE_LABELS:
            entries.append(Entry(lines=[cells[1], *cells[2:]], title=value))
            continue
        if not entries:
            entries.append(Entry(lines=list(cells), title=cells[0].text))
            continue
        if len(cells) >= 2:
            entries[-1].keys.append(f"{label}:{value}")
            if label in _DESCRIPTION_LABELS:
                entries[-1].inline_rest = f"{entries[-1].inline_rest} {value}".strip()
            else:
                entries[-1].extra_text = f"{entries[-1].extra_text} {label}: {value}".strip()
        else:
            entries[-1].lines.extend(cells)
    return entries


def _items(lines: list[TextLine]) -> list[list[TextLine]]:
    """Group physical lines into logical items (bullets / sentences that wrap)."""
    items: list[list[TextLine]] = []
    for tl in lines:
        start_new = (
            not items
            or tl.is_bullet
            or (tl.block_id != items[-1][-1].block_id and tl.text[:1].isupper())
            or (tl.text[:1].isupper() and items[-1][-1].text.rstrip().endswith((".", "!", "?")))
        )
        if start_new:
            items.append([tl])
        else:
            items[-1].append(tl)
    return items


def _lead(item: list[TextLine]) -> TextLine:
    """The first line of an item that carries text. Some PDFs store the bullet glyph as its
    own line; locating or reading that line would point at the bullet, not the text."""
    for tl in item:
        if any(ch.isalnum() for ch in tl.body_text):
            return tl
    return item[0]


# --------------------------------------------------------------------------- checks


def check_projects(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    only_ph = ctx.cache.get("placeholder_only_lines", set())
    tech_terms, _ = builtin_allowlist()
    action_verbs = {v.lower() for v in cfg.action_verbs}
    findings: list[Finding] = []
    value_labels = {v.lower() for v in ctx.settings.analysis.placeholders.value_labels}

    lines = ctx.section_lines("projects")
    entries = _table_entries(ctx, [t for t in lines if t.block.table_id])
    entries += split_entries([t for t in lines if not t.block.table_id], frozenset(value_labels))
    for entry in entries:
        if _key(entry.first) in only_ph:
            continue  # the title itself is template text: reported by the placeholder module
        desc_parts = [entry.inline_rest] if entry.inline_rest else []
        for tl in entry.lines[1:]:
            if _key(tl) in only_ph:
                continue
            label, sep, value = tl.content_text.partition(":")
            label = label.strip().lstrip("•·-–* ").lower()
            if sep and label in value_labels:
                if label in _DESCRIPTION_LABELS:
                    desc_parts.append(value)
                continue  # "Technologies: ...", "Duration: ..." are not a description
            desc_parts.append(tl.body_text)
        description = " ".join(desc_parts).strip()
        title = entry.title.strip()
        location = locate(entry.first)
        entry_keys = [f"{t.block_id}:l{t.line_index}" for t in entry.lines]

        if _letters(description) < cfg.min_description_chars:
            findings.append(
                make_finding(
                    rule_id="CQ_PROJECT_NO_DESCRIPTION",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.DEFINITE_DEFECT,
                    severity=FindingSeverity.HIGH,
                    rubric=CR,
                    explanation=f"The project '{title}' has a title but no description.",
                    correction=(
                        f"Describe '{title}': what it does, the technologies used and your "
                        "own contribution."
                    ),
                    method=DetectionMethod.STRUCTURE,
                    confidence=0.85,
                    detected_text=title,
                    location=location,
                    section="projects",
                    evidence={"entry_lines": entry_keys},
                )
            )
            continue

        full = f"{title} {description} {entry.extra_text} " + " ".join(
            t.content_text for t in entry.lines[1:]
        )
        lowered = full.lower()
        tokens = {w.lower().strip(".,;:()") for w in re.split(r"[\s,/()|]+", full) if w}
        generic = {g.casefold() for g in cfg.generic_tech_markers}
        has_tech = (
            bool(tokens & tech_terms)
            or any(
                _contains_phrase(lowered, m)
                for m in cfg.technology_markers
                if m.casefold() not in generic
            )
            or any(_named_tech_after(full, m, tech_terms) for m in generic)
        )
        if not has_tech:
            findings.append(
                make_finding(
                    rule_id="CQ_PROJECT_NO_TECH",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.RECOMMENDATION,
                    severity=FindingSeverity.LOW,
                    rubric=CR,
                    explanation=(
                        f"The project '{title}' does not mention any technologies or tools."
                    ),
                    correction="Name the languages, frameworks or tools you actually used.",
                    method=DetectionMethod.PATTERN,
                    confidence=0.7,
                    detected_text=title,
                    location=location,
                    section="projects",
                )
            )
        has_contribution = any(
            _contains_phrase(" " + lowered, m) for m in cfg.contribution_markers
        ) or bool({w.lower() for w in _words(description)} & action_verbs)
        if not has_contribution:
            findings.append(
                make_finding(
                    rule_id="CQ_PROJECT_NO_CONTRIBUTION",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.RECOMMENDATION,
                    severity=FindingSeverity.LOW,
                    rubric=CR,
                    explanation=(
                        f"The description of '{title}' does not say what you personally did."
                    ),
                    correction=(
                        "State your own role using an action verb (e.g. 'Designed ...', "
                        "'Implemented ...') that matches what you actually did."
                    ),
                    method=DetectionMethod.PATTERN,
                    confidence=0.6,
                    detected_text=title,
                    location=location,
                    section="projects",
                )
            )
        padded = " " + lowered + " "
        team = [m for m in cfg.team_markers if _contains_phrase(padded, m)]
        individual = [m for m in cfg.individual_markers if _contains_phrase(padded, m)]
        if has_contribution and team and not individual:
            findings.append(
                make_finding(
                    rule_id="CQ_PROJECT_TEAM_ROLE_UNCLEAR",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.MEDIUM,
                    rubric=CR,
                    explanation=(
                        f"'{title}' is described as team work but does not say which part "
                        "was yours."
                    ),
                    correction=(
                        "Add one line on your own contribution (e.g. 'My role: built the "
                        "REST API and the database schema'), describing only what you did."
                    ),
                    method=DetectionMethod.PATTERN,
                    confidence=0.65,
                    detected_text=title,
                    location=location,
                    section="projects",
                    evidence={
                        "team_markers": [t.strip() for t in team],
                        "individual_markers": [],
                    },  # fmt: skip
                )
            )
        word_count = len(_words(description))
        has_outcome = bool(_OUTCOME_NUMBER_RE.search(full)) or any(
            _term_in(lowered, m) for m in cfg.outcome_markers
        )
        if word_count >= cfg.outcome_min_words and not has_outcome:
            findings.append(
                make_finding(
                    rule_id="CQ_PROJECT_NO_OUTCOME",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.RECOMMENDATION,
                    severity=FindingSeverity.LOW,
                    rubric=CR,
                    explanation=(
                        f"The description of '{title}' explains the work but states no "
                        "result or outcome."
                    ),
                    correction=(
                        "If the project had a result you can verify (users, accuracy, time "
                        "saved, deployment, an award), state it. Do not add figures you "
                        "cannot support."
                    ),
                    method=DetectionMethod.PATTERN,
                    confidence=0.55,
                    detected_text=title,
                    location=location,
                    section="projects",
                    evidence={"word_count": word_count, "outcome_markers": []},
                )
            )
        if word_count < cfg.vague_min_words:
            findings.append(
                make_finding(
                    rule_id="CQ_PROJECT_VAGUE",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=CR,
                    explanation=(
                        f"The description of '{title}' is very short ({word_count} words) and "
                        "does not explain the project."
                    ),
                    correction=(
                        "Explain the problem solved, how it works and your part in it. Mention "
                        "a result only if it is real and you can support it."
                    ),
                    method=DetectionMethod.STRUCTURE,
                    confidence=0.6,
                    detected_text=title,
                    location=location,
                    section="projects",
                    evidence={"word_count": word_count},
                )
            )
    return findings


def check_repeated_lines(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    only_ph = ctx.cache.get("placeholder_only_lines", set())
    lines = [t for t in ctx.lines if not t.is_heading and _key(t) not in only_ph]
    items = []
    for item in _items(lines):
        text = " ".join(t.body_text for t in item)
        norm = normalize_for_match(text)
        if len(norm) >= cfg.duplicate_min_chars:
            items.append((norm, text, item))
    findings: list[Finding] = []
    first_seen: dict[str, list[TextLine]] = {}
    for norm, text, item in items:
        if norm in first_seen:
            original = first_seen[norm][0]
            findings.append(
                make_finding(
                    rule_id="CQ_REPEATED_LINE",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.DEFINITE_DEFECT,
                    severity=FindingSeverity.MEDIUM,
                    rubric=CR,
                    explanation=(
                        "This line repeats an earlier line word for word"
                        + (
                            f" (first in the {original.section} section)."
                            if original.section
                            else "."
                        )
                    ),
                    correction=(
                        "Remove the duplicate or rewrite it to describe something different."
                    ),
                    method=DetectionMethod.PATTERN,
                    confidence=0.95,
                    detected_text=text,
                    location=locate(_lead(item)),
                    section=_lead(item).section,
                    evidence={"first_occurrence_block": original.block_id},
                )
            )
        else:
            first_seen[norm] = item

    unique = list(first_seen.items())
    for i, (norm_a, item_a) in enumerate(unique):
        if len(norm_a.split()) < 6:
            continue
        for norm_b, item_b in unique[i + 1 :]:
            if len(norm_b.split()) < 6:
                continue
            matcher = SequenceMatcher(None, norm_a, norm_b)
            if matcher.quick_ratio() < cfg.near_duplicate_ratio:
                continue
            ratio = matcher.ratio()
            if ratio < cfg.near_duplicate_ratio:
                continue
            findings.append(
                make_finding(
                    rule_id="CQ_NEAR_DUPLICATE_LINE",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=CR,
                    explanation=f"This line is almost identical ({ratio:.0%}) to an earlier line.",
                    correction="Merge the two lines or make each describe a different point.",
                    method=DetectionMethod.PATTERN,
                    confidence=0.7,
                    detected_text=" ".join(t.body_text for t in item_b),
                    location=locate(item_b[0]),
                    section=item_b[0].section,
                    evidence={"similar_to_block": item_a[0].block_id, "ratio": round(ratio, 3)},
                )
            )
    return findings


def check_weak_verbs(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    openers = sorted({o.lower() for o in cfg.weak_openers}, key=len, reverse=True)
    findings: list[Finding] = []
    for section in ("experience", "projects", "achievements", "activities"):
        for item in _items(ctx.section_lines(section)):
            tl = _lead(item)
            text = tl.body_text
            lowered = text.lower()
            opener = next((o for o in openers if re.match(rf"{re.escape(o)}\b", lowered)), None)
            if opener is None:
                continue
            start = tl.text.find(text)
            findings.append(
                make_finding(
                    rule_id="CQ_WEAK_VERB",
                    category=FindingCategory.CONTENT_QUALITY,
                    finding_class=FindingClass.RECOMMENDATION,
                    severity=FindingSeverity.LOW,
                    rubric=LT,
                    explanation=f"The line starts with the weak phrase '{text[: len(opener)]}'.",
                    correction=(
                        "Start with a specific action verb that accurately describes what you "
                        "did (e.g. 'Developed', 'Implemented', 'Analysed')."
                    ),
                    method=DetectionMethod.PATTERN,
                    confidence=0.75,
                    detected_text=text[: len(opener)],
                    location=locate(tl, start, start + len(opener)),
                    section=section,
                )
            )
    return findings


def check_filler(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    hits: list[tuple[TextLine, int, int, str]] = []
    for tl in ctx.lines:
        if tl.is_heading:
            continue
        lowered = tl.text.lower()
        for phrase in cfg.filler_phrases:
            for m in re.finditer(rf"(?<![a-z]){re.escape(phrase.lower())}(?![a-z])", lowered):
                hits.append((tl, m.start(), m.end(), phrase))
    distinct = {h[3].lower().replace("-", " ") for h in hits}
    if len(distinct) < cfg.filler_min_hits:
        return []
    return [
        make_finding(
            rule_id="CQ_FILLER_PHRASE",
            category=FindingCategory.CONTENT_QUALITY,
            finding_class=FindingClass.RECOMMENDATION,
            severity=FindingSeverity.LOW,
            rubric=LT,
            explanation=(
                f"'{tl.text[a:b]}' is a generic phrase; the resume uses {len(distinct)} such "
                "phrases, which adds length without evidence."
            ),
            correction="Replace generic claims with a concrete example or remove them.",
            method=DetectionMethod.PATTERN,
            confidence=0.7,
            detected_text=tl.text[a:b],
            location=locate(tl, a, b),
            section=tl.section,
        )
        for tl, a, b, _ in hits
    ]


def check_education(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    lines = ctx.section_lines("education")
    if not lines:
        return []
    degree_re = (
        re.compile(
            r"(?<![a-z])(?:"
            + "|".join(re.escape(k.lower()) for k in cfg.degree_keywords)
            + r")(?![a-z])"
        )
        if cfg.degree_keywords
        else None
    )
    entries: list[list[TextLine]] = []
    for tl in lines:
        if not entries or (degree_re and degree_re.search(tl.text.lower())):
            entries.append([tl])
        else:
            entries[-1].append(tl)
    findings: list[Finding] = []
    for entry in entries:
        text = " ".join(t.content_text for t in entry)
        lowered = text.lower()
        missing = []
        if not _YEAR_RE.search(text):
            missing.append("year / duration")
        if not any(_contains_phrase(lowered, k) for k in cfg.institution_keywords):
            missing.append("institution")
        no_score = not _SCORE_RE.search(text)
        if not missing and not no_score:
            continue
        definite_fields = list(missing)
        if no_score:
            missing.append("score (CGPA / percentage)")
        findings.append(
            make_finding(
                rule_id="CQ_EDUCATION_INCOMPLETE",
                category=FindingCategory.CONTENT_QUALITY,
                finding_class=FindingClass.POTENTIAL_ISSUE
                if definite_fields
                else FindingClass.RECOMMENDATION,
                severity=FindingSeverity.MEDIUM if definite_fields else FindingSeverity.LOW,
                rubric=CR,
                explanation=("This education entry does not show: " + ", ".join(missing) + "."),
                correction=(
                    "Add the missing details (" + ", ".join(missing) + ") if they apply; "
                    "do not add a score you do not want to disclose."
                ),
                method=DetectionMethod.PATTERN,
                confidence=0.65,
                detected_text=entry[0].content_text,
                location=locate(entry[0]),
                section="education",
                evidence={"missing": missing},
            )
        )
    return findings


def check_certifications(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    only_ph = ctx.cache.get("placeholder_only_lines", set())
    findings: list[Finding] = []
    for item in _items(ctx.section_lines("certifications")):
        if _key(_lead(item)) in only_ph:
            continue
        text = " ".join(t.content_text for t in item)
        if _letters(text) < 6:
            continue
        lowered = text.lower()
        has_issuer = any(_contains_phrase(lowered, k) for k in cfg.certification_issuers) or bool(
            re.search(r"\b(?:by|from|-|–|\|)\s+[A-Z]", text)
        )
        has_date = bool(_YEAR_RE.search(text) or _MONTH_RE.search(text))
        missing = [n for n, ok in (("issuer", has_issuer), ("date", has_date)) if not ok]
        if not missing:
            continue
        findings.append(
            make_finding(
                rule_id="CQ_CERTIFICATION_DETAILS",
                category=FindingCategory.CONTENT_QUALITY,
                finding_class=FindingClass.RECOMMENDATION,
                severity=FindingSeverity.LOW,
                rubric=CR,
                explanation=("This certification does not show the " + " or ".join(missing) + "."),
                correction=(
                    "Add the " + " and ".join(missing) + " if this information is available "
                    "on your certificate."
                ),
                method=DetectionMethod.PATTERN,
                confidence=0.6,
                detected_text=text,
                location=locate(_lead(item)),
                section="certifications",
                evidence={"missing": missing},
            )
        )
    return findings


def check_objective(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.content
    only_ph = ctx.cache.get("placeholder_only_lines", set())
    lines = [t for t in ctx.section_lines("summary") if _key(t) not in only_ph]
    if not lines:
        return []  # missing / empty / placeholder-only: reported by completeness
    text = " ".join(core_text(t.content_text) or t.content_text for t in lines).strip()
    words = _words(text)
    findings: list[Finding] = []
    location = locate(lines[0])
    last = re.sub(r"[^a-z]", "", words[-1].lower()) if words else ""
    if words and last in {e.lower() for e in cfg.incomplete_endings} and not text.endswith("."):
        findings.append(
            make_finding(
                rule_id="CQ_OBJECTIVE_INCOMPLETE",
                category=FindingCategory.CONTENT_QUALITY,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM,
                rubric=CR,
                explanation=(
                    f"The career objective appears to stop mid-sentence (ends with '{words[-1]}')."
                ),
                correction="Complete the sentence.",
                method=DetectionMethod.PATTERN,
                confidence=0.75,
                detected_text=text,
                location=location,
                section="summary",
            )
        )
    if len(words) < cfg.objective_min_words:
        findings.append(
            make_finding(
                rule_id="CQ_OBJECTIVE_TOO_SHORT",
                category=FindingCategory.CONTENT_QUALITY,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM,
                rubric=CR,
                explanation=(
                    f"The career objective has only {len(words)} words and does not state a "
                    "target role and relevant strengths."
                ),
                correction=(
                    "Write 1-3 sentences naming the role you are seeking and the skills or "
                    "interests that make you suitable."
                ),
                method=DetectionMethod.STRUCTURE,
                confidence=0.7,
                detected_text=text,
                location=location,
                section="summary",
                evidence={"word_count": len(words)},
            )
        )
    if words and not any(_term_in(text.casefold(), t) for t in cfg.objective_target_terms):
        findings.append(
            make_finding(
                rule_id="CQ_OBJECTIVE_NO_TARGET",
                category=FindingCategory.CONTENT_QUALITY,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.LOW,
                rubric=CR,
                explanation="The career objective does not name a target role or field.",
                correction=(
                    "Name the role or field you are applying for (e.g. 'a software "
                    "engineering internship in backend development')."
                ),
                method=DetectionMethod.PATTERN,
                confidence=0.6,
                detected_text=text,
                location=location,
                section="summary",
                evidence={
                    "note": "only the presence of a target is checked; whether the wording "
                    "suits a specific job is left to optional contextual review"
                },
            )
        )
    if len(words) > cfg.objective_max_words:
        findings.append(
            make_finding(
                rule_id="CQ_OBJECTIVE_TOO_LONG",
                category=FindingCategory.CONTENT_QUALITY,
                finding_class=FindingClass.RECOMMENDATION,
                severity=FindingSeverity.LOW,
                rubric=CR,
                explanation=f"The career objective is long ({len(words)} words).",
                correction="Shorten it to 2-3 focused sentences.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.7,
                detected_text=text,
                location=location,
                section="summary",
            )
        )
    return findings


def check_content(ctx: AnalysisContext) -> list[Finding]:
    findings: list[Finding] = []
    for check in (
        check_projects,
        check_repeated_lines,
        check_weak_verbs,
        check_filler,
        check_education,
        check_certifications,
        check_objective,
    ):
        findings.extend(check(ctx))
    return findings
