"""Shared, read-only view of a parsed document for the analysis modules.

Everything here is derived from the Milestone 1 ``ScanResult``; nothing is re-parsed.
Locations are taken from the parser's own spans/lines/blocks. When the parser has no
coordinates (DOCX), locations stay ``None`` and are never estimated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pps_ats.analysis.sections import logical_lines, segment_lines
from pps_ats.config import Settings
from pps_ats.schemas.common import BBox
from pps_ats.schemas.content import Block, BlockKind, ExtractedContent, Line, Region
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    LocationPrecision,
    RubricCriterion,
)
from pps_ats.schemas.result import ScanResult, Section

BULLET_CHARS = "•·▪▫◦‣∙●○■□►▸➢➤✓✔-–—*>"
_BULLET_RE = re.compile(rf"^\s*[{re.escape(BULLET_CHARS)}]\s*")


@dataclass
class TextLine:
    order: int
    text: str  # stripped line text (original characters preserved)
    raw_text: str  # line text exactly as extracted
    block: Block
    line: Line
    line_index: int
    section: str | None
    is_heading: bool = False
    heading_remainder: str = ""
    size: float | None = None
    bold_ratio: float = 0.0

    @property
    def block_id(self) -> str:
        return self.block.id

    @property
    def page(self) -> int | None:
        return self.block.page

    @property
    def bbox(self) -> BBox | None:
        return self.line.bbox

    @property
    def region(self) -> Region:
        return self.block.region

    @property
    def is_bullet(self) -> bool:
        fmt = self.block.format
        return bool(_BULLET_RE.match(self.text)) or bool(fmt and fmt.is_list_item)

    @property
    def body_text(self) -> str:
        """Text without a leading bullet glyph."""
        return _BULLET_RE.sub("", self.text, count=1).strip()

    @property
    def content_text(self) -> str:
        """Text that counts as section content (heading lines contribute only "Skills: ...")."""
        return self.heading_remainder if self.is_heading else self.text


@dataclass
class Location:
    page: int | None = None
    bbox: BBox | None = None
    precision: LocationPrecision = LocationPrecision.NONE
    block_id: str | None = None
    span_id: str | None = None


def _union(boxes: list[BBox]) -> BBox:
    return (
        round(min(b[0] for b in boxes), 2),
        round(min(b[1] for b in boxes), 2),
        round(max(b[2] for b in boxes), 2),
        round(max(b[3] for b in boxes), 2),
    )


def locate(tl: TextLine, start: int | None = None, end: int | None = None) -> Location:
    """Location of ``tl.text[start:end]`` (offsets in the stripped text). Uses the bounding
    boxes of the spans that contain the text; falls back to the line box. Never invents."""
    lead = len(tl.raw_text) - len(tl.raw_text.lstrip())
    spans = tl.line.spans
    if start is None or end is None:
        start, end = 0, len(tl.text)
    a, b = start + lead, end + lead

    covered: list[int] = []
    pos = 0
    for index, span in enumerate(spans):
        s_start, s_end = pos, pos + len(span.text)
        pos = s_end
        if s_end > a and s_start < b:
            covered.append(index)
    span_id = f"{tl.block_id}:l{tl.line_index}:s{covered[0]}" if covered else None

    boxes = [spans[i].bbox for i in covered if spans[i].bbox is not None]
    if boxes and len(boxes) == len(covered):
        return Location(tl.page, _union(boxes), LocationPrecision.SPAN, tl.block_id, span_id)
    if tl.bbox is not None:
        return Location(tl.page, tl.bbox, LocationPrecision.LINE, tl.block_id, span_id)
    return Location(tl.page, None, LocationPrecision.NONE, tl.block_id, span_id)


def block_location(block: Block) -> Location:
    if block.bbox is not None:
        return Location(block.page, block.bbox, LocationPrecision.BLOCK, block.id)
    return Location(block.page, None, LocationPrecision.NONE, block.id)


@dataclass
class ManifestRecord:
    """One row of the optional authoritative student manifest."""

    file_name: str
    student_name: str | None = None
    student_id: str | None = None
    email: str | None = None


@dataclass
class AnalysisContext:
    scan: ScanResult
    content: ExtractedContent
    settings: Settings
    lines: list[TextLine]  # body lines, reading order, with section assignment
    hf_lines: list[TextLine]  # header/footer lines
    sections: dict[str, Section]
    manifest: ManifestRecord | None = None
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def has_coordinates(self) -> bool:
        return self.content.diagnostics.coordinates_available

    @property
    def body_size(self) -> float | None:
        return self.content.stats.body_font_size

    def section_lines(self, name: str, include_heading_remainder: bool = True) -> list[TextLine]:
        out = []
        for tl in self.lines:
            if tl.section != name:
                continue
            if tl.is_heading and not (include_heading_remainder and tl.heading_remainder):
                continue
            out.append(tl)
        return out

    def all_lines(self) -> list[TextLine]:
        return self.lines + self.hf_lines


def build_context(
    scan: ScanResult, settings: Settings, manifest: ManifestRecord | None = None
) -> AnalysisContext:
    if scan.content is None:
        raise ValueError("scan has no extracted content")
    content = scan.content
    lines: list[TextLine] = []
    for order, assignment in enumerate(segment_lines(content, settings.sections)):
        ll = assignment.line
        assert ll.block is not None and ll.line is not None
        lines.append(
            TextLine(
                order=order,
                text=ll.text,
                raw_text=ll.line.text,
                block=ll.block,
                line=ll.line,
                line_index=ll.line_index,
                section=assignment.section,
                is_heading=assignment.heading is not None,
                heading_remainder=assignment.heading.remainder if assignment.heading else "",
                size=ll.size,
                bold_ratio=ll.bold_ratio,
            )
        )
    hf_lines: list[TextLine] = []
    for ll in logical_lines(content, include_header_footer=True):
        assert ll.block is not None and ll.line is not None
        if ll.block.region is Region.BODY and ll.block.kind not in (
            BlockKind.HEADER,
            BlockKind.FOOTER,
        ):
            continue
        hf_lines.append(
            TextLine(
                order=len(lines) + len(hf_lines),
                text=ll.text,
                raw_text=ll.line.text,
                block=ll.block,
                line=ll.line,
                line_index=ll.line_index,
                section=None,
                size=ll.size,
                bold_ratio=ll.bold_ratio,
            )
        )
    return AnalysisContext(
        scan=scan,
        content=content,
        settings=settings,
        lines=lines,
        hf_lines=hf_lines,
        sections={s.name: s for s in scan.sections},
        manifest=manifest,
    )


def truncate(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def make_finding(
    *,
    rule_id: str,
    category: FindingCategory,
    finding_class: FindingClass,
    severity: FindingSeverity,
    rubric: RubricCriterion,
    explanation: str,
    method: DetectionMethod,
    confidence: float,
    correction: str | None = None,
    detected_text: str | None = None,
    location: Location | None = None,
    section: str | None = None,
    review_required: bool = False,
    ats_impact: bool = False,
    evidence: dict[str, Any] | None = None,
) -> Finding:
    loc = location or Location()
    return Finding(
        issue_id="",  # assigned by the orchestrator (deterministic)
        rule_id=rule_id,
        category=category,
        finding_class=finding_class,
        severity=severity,
        rubric_criterion=rubric,
        ats_impact=ats_impact,
        detected_text=truncate(detected_text),
        explanation=explanation,
        suggested_correction=correction,
        section=section,
        page_number=loc.page,
        bounding_box=loc.bbox,
        location_precision=loc.precision,
        block_id=loc.block_id,
        span_id=loc.span_id,
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        detection_method=method,
        review_required=review_required,
        evidence=evidence or {},
    )
