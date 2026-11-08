"""Heading-based resume section segmentation.

Status semantics (kept distinct on purpose):
  detected  - heading found and followed by meaningful content
  empty     - heading found but no content, or only template placeholders
  uncertain - weak heading evidence, or very little content
  missing   - no evidence of the section at all
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pps_ats.config import SectionSettings
from pps_ats.parsers.text_utils import normalize_for_match
from pps_ats.schemas.common import BBox, SectionStatus
from pps_ats.schemas.content import Block, BlockKind, ExtractedContent, Line, Region
from pps_ats.schemas.result import Section

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s().-]{8,17}\d")
PROFILE_URL_RE = re.compile(r"(?:linkedin\.com/in/|github\.com/)[\w-]+", re.IGNORECASE)
_DATE_RANGE_RE = re.compile(r"^(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}$")
_HEADING_STYLE_PREFIXES = ("heading", "title", "subtitle")
# Inline "Label: value" lines that are sub-labels of the active section, not new sections.
_INLINE_SUBLABELS = {
    "skills": {"languages", "certifications"},
    "experience": {"projects", "skills"},
    "projects": {"skills", "experience"},
}


@dataclass
class LogicalLine:
    text: str
    block_id: str
    page: int | None
    bbox: BBox | None
    size: float | None
    bold_ratio: float
    style: str | None
    block: Block | None = None
    line: Line | None = None
    line_index: int = 0


_LogicalLine = LogicalLine  # backwards-compatible alias


@dataclass
class HeadingMatch:
    canonical: str
    heading_text: str
    remainder: str  # inline content after "Heading:"
    formatted: bool
    weak: bool
    kind: str = "exact"  # exact | compound | head_noun
    also: list[str] = field(default_factory=list)  # other sections of a combined heading


@dataclass
class ContactSignals:
    emails: int = 0
    phones: int = 0
    profile_urls: int = 0


@dataclass
class SectionAnalysis:
    sections: list[Section]
    contact: ContactSignals
    headings_found: list[str] = field(default_factory=list)


def logical_lines(
    content: ExtractedContent, include_header_footer: bool = False
) -> list[LogicalLine]:
    lines: list[LogicalLine] = []
    for block in sorted(content.blocks, key=lambda b: b.reading_order):
        in_body = block.region is Region.BODY and block.kind not in (
            BlockKind.HEADER,
            BlockKind.FOOTER,
        )
        if not in_body and not include_header_footer:
            continue
        for line_index, line in enumerate(block.lines):
            if not line.text.strip():
                continue
            visible = [s for s in line.spans if s.text.strip()]
            total = sum(len(s.text.strip()) for s in visible) or 1
            bold = sum(len(s.text.strip()) for s in visible if s.bold) / total
            sizes = [s.size for s in visible if s.size]
            lines.append(
                LogicalLine(
                    text=line.text.strip(),
                    block_id=block.id,
                    page=block.page,
                    bbox=line.bbox,
                    size=max(sizes) if sizes else None,
                    bold_ratio=bold,
                    style=block.style_name,
                    block=block,
                    line=line,
                    line_index=line_index,
                )
            )
    return lines


_JOINER_RE = re.compile(r"\s*(?:&|/|\||\+|,|\band\b)\s*", re.IGNORECASE)


class HeadingMatcher:
    def __init__(self, cfg: SectionSettings, body_size: float | None) -> None:
        self.cfg = cfg
        self.body_size = body_size
        self.variants: dict[str, str] = {}
        for canonical, variants in cfg.canonical.items():
            for variant in variants:
                norm = normalize_for_match(variant)
                self.variants.setdefault(norm, canonical)
                self.variants.setdefault(norm.replace(" ", ""), canonical)
        self.head_nouns: dict[str, str] = {
            noun.casefold(): canonical
            for canonical, nouns in cfg.head_nouns.items()
            for noun in nouns
        }

    def _head_noun(self, text: str) -> str | None:
        """'Major Project', 'Online Certifications', 'Projects Undertaken': a configured
        section noun as the first or last word of a short phrase (at most 3 modifiers)."""
        tokens = normalize_for_match(text).split()
        if not 1 <= len(tokens) <= 4:
            return None
        if tokens[-1] in self.head_nouns:  # 'Major Project', 'Online Certifications'
            return self.head_nouns[tokens[-1]]
        if len(tokens) <= 2 and tokens[0] in self.head_nouns:  # 'Projects Undertaken'
            return self.head_nouns[tokens[0]]
        return None  # 'PROJECT MANAGER INTERN' is a job title, not a heading

    def _compound(self, text: str) -> list[str] | None:
        """'Projects & Achievements', 'COURSES/CERTIFICATIONS': every part must itself be
        a section name (exact or head noun); returns the sections in order."""
        parts = [p.strip(" .:-") for p in _JOINER_RE.split(text) if p.strip(" .:-")]
        if len(parts) < 2 or any(len(p.split()) > 4 for p in parts):
            return None
        names = []
        for part in parts:
            name = self._lookup(part) or self._head_noun(part)
            if name is None:
                return None
            if name not in names:
                names.append(name)
        return names

    def _strongly_formatted(self, line: LogicalLine, heading_text: str) -> bool:
        """Upper case, larger font or a heading style - bold alone is not enough (bold
        is also used for project and job titles)."""
        letters = [c for c in heading_text if c.isalpha()]
        if letters and all(c.isupper() for c in letters) and len(letters) > 2:
            return True
        if line.style and line.style.lower().startswith(_HEADING_STYLE_PREFIXES):
            return True
        return bool(line.size and self.body_size and line.size >= self.body_size * 1.12)

    def _extended(self, line: LogicalLine, text: str, inline: bool) -> HeadingMatch | None:
        heading_text = text.strip()
        names = self._compound(heading_text)
        if names and (inline or self._formatted(line, heading_text)):
            return HeadingMatch(names[0], heading_text, "", True, False, "compound", names[1:])
        name = self._head_noun(heading_text)
        if (
            name
            and self._strongly_formatted(line, heading_text)
            and (len(normalize_for_match(heading_text).split()) > 1 or inline)
        ):
            return HeadingMatch(name, heading_text, "", True, False, "head_noun")
        return None

    def _lookup(self, text: str) -> str | None:
        norm = normalize_for_match(text)
        if not norm or len(norm.split()) > self.cfg.max_heading_words:
            return None
        tokens = norm.split()
        if len(tokens) > 2 and all(len(t) == 1 for t in tokens):  # "E D U C A T I O N"
            norm = "".join(tokens)
        return self.variants.get(norm) or self.variants.get(norm.replace(" ", ""))

    def _formatted(self, line: LogicalLine, heading_text: str) -> bool:
        letters = [c for c in heading_text if c.isalpha()]
        if letters and all(c.isupper() for c in letters) and len(letters) > 2:
            return True
        if line.bold_ratio >= 0.6:
            return True
        if line.style and line.style.lower().startswith(_HEADING_STYLE_PREFIXES):
            return True
        return bool(line.size and self.body_size and line.size >= self.body_size * 1.12)

    def match(self, line: LogicalLine) -> HeadingMatch | None:
        text = line.text.strip().strip("•·-–—|:").strip()
        canonical = self._lookup(text)
        remainder = ""
        heading_text = text
        if canonical is None and ":" in line.text:
            head, _, rest = line.text.partition(":")
            canonical = self._lookup(head)
            if canonical is None:
                extended = self._extended(line, head.strip(" •·-–—|"), inline=True)
                if extended is None:
                    return None
                extended.remainder = rest.strip()
                return extended
            heading_text, remainder = head.strip(), rest.strip()
        if canonical is None:
            return self._extended(line, text, inline=False)
        formatted = self._formatted(line, heading_text)
        single_word = len(normalize_for_match(heading_text).split()) == 1
        # A plain single word on its own line ("Leadership") is weak evidence; inline
        # "Skills: ..." labels and formatted headings are not.
        weak = not formatted and single_word and not remainder
        return HeadingMatch(canonical, heading_text, remainder, formatted, weak)


def find_phone_numbers(text: str) -> list[str]:
    phones = []
    for candidate in PHONE_CANDIDATE_RE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 13 and not _DATE_RANGE_RE.match(candidate.strip()):
            phones.append(candidate.strip())
    return phones


def _content_status(
    text: str, cfg: SectionSettings, placeholders: list[re.Pattern[str]]
) -> tuple[SectionStatus, int, int, str]:
    if not text.strip():
        return SectionStatus.EMPTY, 0, 0, "heading present but no content follows"
    hits = 0
    stripped = text
    for pattern in placeholders:
        hits += len(pattern.findall(stripped))
        stripped = pattern.sub(" ", stripped)
    letters = sum(1 for c in stripped if c.isalnum())
    if letters == 0 or (hits and letters < cfg.min_content_chars):
        return SectionStatus.EMPTY, letters, hits, "content is template placeholder text only"
    if letters < cfg.min_content_chars:
        return SectionStatus.UNCERTAIN, letters, hits, "very little content under heading"
    return SectionStatus.DETECTED, letters, hits, "heading with content"


@dataclass
class LineAssignment:
    """One body line with the section it belongs to (None before the first heading)."""

    line: LogicalLine
    section: str | None
    heading: HeadingMatch | None


def segment_lines(content: ExtractedContent, cfg: SectionSettings) -> list[LineAssignment]:
    """Assign every body line to a canonical section. Shared by Milestone 1 section
    extraction and the Milestone 2 analysis modules, so both see identical boundaries."""
    matcher = HeadingMatcher(cfg, content.stats.body_font_size)
    assignments: list[LineAssignment] = []
    current: str | None = None
    for line in logical_lines(content):
        heading = matcher.match(line)
        if (
            heading is not None
            and current is not None
            and heading.kind != "exact"
            and heading.canonical == current
        ):
            # 'Final Year Project' inside Projects is an entry title, not a new heading.
            heading = None
        if (
            heading is not None
            and current is not None
            and (heading.remainder or heading.weak)
            and (
                heading.canonical == current
                or heading.canonical in _INLINE_SUBLABELS.get(current, ())
            )
        ):
            # Sub-labels, not new sections: "Project: X" inside Projects,
            # "Languages: Python" or a plain "Languages" table cell inside Skills.
            heading = None
        if heading is not None:
            current = heading.canonical
        assignments.append(LineAssignment(line=line, section=current, heading=heading))
    return assignments


def extract_sections(content: ExtractedContent, cfg: SectionSettings) -> SectionAnalysis:
    placeholders = [re.compile(p) for p in cfg.placeholder_patterns]

    found: dict[str, Section] = {}
    weak_heading: dict[str, bool] = {}
    buffers: dict[str, list[str]] = {}
    combined: list[tuple[str, str, LogicalLine, HeadingMatch]] = []  # (also, primary, ...)

    for assignment in segment_lines(content, cfg):
        line, heading, current = assignment.line, assignment.heading, assignment.section
        if heading is not None:
            section = found.get(current)
            if section is None:
                section = Section(
                    name=current,
                    status=SectionStatus.MISSING,
                    heading_text=heading.heading_text,
                    heading_block_id=line.block_id,
                    page=line.page,
                    bbox=line.bbox,
                )
                found[current] = section
                buffers[current] = []
                weak_heading[current] = heading.weak
            else:
                weak_heading[current] = weak_heading[current] and heading.weak
            section.occurrences += 1
            combined += [(name, current, line, heading) for name in heading.also]
            if heading.kind != "exact":
                section.evidence.append(f"{heading.kind.replace('_', ' ')} heading")
            if not heading.formatted:
                section.evidence.append(
                    f"heading '{heading.heading_text}' has no heading formatting"
                )
            if heading.remainder:
                buffers[current].append(heading.remainder)
            continue
        if current is not None:
            buffers[current].append(line.text)
            if line.block_id not in found[current].content_block_ids:
                found[current].content_block_ids.append(line.block_id)

    for name, section in found.items():
        text = "\n".join(buffers[name]).strip()
        status, letters, hits, reason = _content_status(text, cfg, placeholders)
        if status is SectionStatus.DETECTED and weak_heading[name]:
            status, reason = SectionStatus.UNCERTAIN, "weak heading (plain single word)"
        section.status = status
        section.content_text = text
        section.content_char_count = letters
        section.placeholder_hits = hits
        section.evidence.insert(0, reason)
        if section.occurrences > 1:
            section.evidence.append(f"heading appears {section.occurrences} times")

    # A combined heading ('Projects & Achievements') also evidences its other sections.
    for name, primary, line, heading in combined:
        if name in found:
            continue
        source = found[primary]
        found[name] = Section(
            name=name,
            status=source.status,
            heading_text=heading.heading_text,
            heading_block_id=line.block_id,
            page=line.page,
            bbox=line.bbox,
            content_text=source.content_text,
            content_char_count=source.content_char_count,
            content_block_ids=list(source.content_block_ids),
            occurrences=1,
            evidence=[f"shares the combined heading with '{primary}'"],
        )

    # Contact information is usually unlabelled, so detect it from the text itself.
    all_text = f"{content.full_text}\n{content.header_footer_text}"
    contact = ContactSignals(
        emails=len(EMAIL_RE.findall(all_text)),
        phones=len(find_phone_numbers(all_text)),
        profile_urls=len(PROFILE_URL_RE.findall(all_text)),
    )
    contact_section = found.get("contact") or Section(name="contact", status=SectionStatus.MISSING)
    evidence = [
        f"{label}: {count}"
        for label, count in (
            ("emails", contact.emails),
            ("phone numbers", contact.phones),
            ("profile urls", contact.profile_urls),
        )
        if count
    ]
    if contact.emails or contact.phones:
        contact_section.status = SectionStatus.DETECTED
    elif contact.profile_urls:
        contact_section.status = SectionStatus.UNCERTAIN
    contact_section.evidence = evidence + [
        e for e in contact_section.evidence if e not in evidence
    ] or ["no email, phone or profile URL found"]
    found["contact"] = contact_section

    ordered: list[Section] = []
    names = ["contact", *[n for n in cfg.canonical if n != "contact"]]
    for name in names:
        section = found.get(name) or Section(
            name=name, status=SectionStatus.MISSING, evidence=["no heading found"]
        )
        section.required = name in cfg.required
        ordered.append(section)

    return SectionAnalysis(
        sections=ordered,
        contact=contact,
        headings_found=[n for n in found if found[n].occurrences > 0],
    )
