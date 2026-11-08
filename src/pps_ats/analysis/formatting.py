"""Formatting checks from measured parser metadata (fonts, sizes, spacing, alignment,
margins, garbled text). PDF checks use coordinates; DOCX checks use paragraph/run
properties. Thresholds live in ``[analysis.formatting]``.

Design rule: a single alternative font, a decorative heading font or a deliberate design
choice is not a defect; only measurable inconsistency or readability risk is reported.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

from pps_ats.analysis.context import (
    AnalysisContext,
    Location,
    TextLine,
    block_location,
    locate,
    make_finding,
)
from pps_ats.parsers.text_utils import is_garbled_char
from pps_ats.schemas.content import BlockKind, Region, Span
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    LocationPrecision,
    RubricCriterion,
)

_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_STYLE_SUFFIX_RE = re.compile(
    r"(?:PSMT|MT|PS|Bold|Italic|Oblique|Regular|Roman|Semibold|SemiBold|Light|Medium|Black|"
    r"Heavy|Demi|Book|Condensed|Narrow|BoldItalic|It|Bd|BI)$"
)
_CID_RE = re.compile(r"\(cid:\d+\)")

SF = RubricCriterion.STRUCTURE_FORMATTING


def font_family(name: str | None) -> str | None:
    """'ABCDEF+Calibri-Bold' -> 'Calibri'; 'TimesNewRomanPSMT' -> 'TimesNewRoman'."""
    if not name:
        return None
    family = _SUBSET_RE.sub("", name.strip())
    family = re.split(r"[-,]", family, maxsplit=1)[0].strip()
    previous = None
    while previous != family and family:
        previous = family
        family = _STYLE_SUFFIX_RE.sub("", family).strip()
    return family or name


def _is_icon_font(ctx: AnalysisContext, family: str) -> bool:
    lowered = family.lower()
    return any(p in lowered for p in ctx.settings.analysis.formatting.icon_font_patterns)


def _visible(span: Span) -> int:
    """Letters and digits only: bullet glyphs, separators and icons are not body text
    (many resumes draw bullets in a different font or size, or as separate lines)."""
    return sum(1 for c in span.text if c.isalnum())


def _has_text(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _body_lines(ctx: AnalysisContext) -> list[TextLine]:
    return [t for t in ctx.lines if t.region is Region.BODY]


# --------------------------------------------------------------------------- fonts


def check_fonts(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    family_chars: Counter[str] = Counter()
    for tl in _body_lines(ctx):
        for span in tl.line.spans:
            fam = font_family(span.font)
            if fam and not _is_icon_font(ctx, fam):
                family_chars[fam] += _visible(span)
    total = sum(family_chars.values())
    if not total:
        return findings
    significant = {f: c for f, c in family_chars.items() if c / total >= cfg.min_font_share}

    if len(significant) > cfg.max_font_families:
        findings.append(
            make_finding(
                rule_id="FMT_TOO_MANY_FONTS",
                category=FindingCategory.FORMATTING,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM,
                rubric=SF,
                explanation=(
                    f"The resume uses {len(significant)} different font families "
                    f"({', '.join(sorted(significant))}); more than {cfg.max_font_families} "
                    "makes the layout look inconsistent."
                ),
                correction="Use one font family for body text and at most one for headings.",
                method=DetectionMethod.FORMAT_METADATA,
                confidence=0.8,
                evidence={f"share_{k}": round(v / total, 3) for k, v in significant.items()},
            )
        )

    # Body-text role: regular (non-bold, non-heading) spans at the body size.
    body = ctx.body_size
    role_chars: Counter[str] = Counter()
    role_blocks: dict[str, list[TextLine]] = defaultdict(list)
    for tl in _body_lines(ctx):
        if tl.is_heading or tl.section is None:
            continue
        for span in tl.line.spans:
            fam = font_family(span.font)
            if not fam or _is_icon_font(ctx, fam) or span.bold:
                continue
            if body and span.size and abs(span.size - body) > 0.75:
                continue
            if not _visible(span):
                continue  # a bullet glyph in another font is not mixed body text
            role_chars[fam] += _visible(span)
            if not role_blocks[fam] or role_blocks[fam][-1].block_id != tl.block_id:
                role_blocks[fam].append(tl)
    role_total = sum(role_chars.values())
    if role_total and len(role_chars) > 1:
        main = role_chars.most_common(1)[0][0]
        for fam, chars in role_chars.most_common()[1:]:
            share = chars / role_total
            if share < cfg.body_family_min_share:
                continue
            for tl in role_blocks[fam]:
                span_idx = next(
                    (
                        i
                        for i, s in enumerate(tl.line.spans)
                        if font_family(s.font) == fam and _visible(s)
                    ),
                    0,
                )
                start = sum(len(s.text) for s in tl.line.spans[:span_idx])
                start = max(0, start - (len(tl.raw_text) - len(tl.raw_text.lstrip())))
                end = start + len(tl.line.spans[span_idx].text.strip())
                findings.append(
                    make_finding(
                        rule_id="FMT_MIXED_BODY_FONTS",
                        category=FindingCategory.FORMATTING,
                        finding_class=FindingClass.POTENTIAL_ISSUE,
                        severity=FindingSeverity.MEDIUM,
                        rubric=SF,
                        explanation=(
                            f"Body text here uses '{fam}' while most body text uses '{main}' "
                            f"({share:.0%} of body text is in '{fam}')."
                        ),
                        correction=f"Change this text to '{main}' so all body text matches.",
                        method=DetectionMethod.FORMAT_METADATA,
                        confidence=0.85,
                        detected_text=tl.text,
                        location=locate(tl, start, end),
                        section=tl.section,
                        evidence={
                            "font_family": fam,
                            "main_family": main,
                            "share": round(share, 3),
                        },
                    )
                )
    return findings


# --------------------------------------------------------------------------- sizes


def check_sizes(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    size_chars: Counter[float] = Counter()
    for tl in _body_lines(ctx):
        for span in tl.line.spans:
            if span.size:
                size_chars[round(span.size * 2) / 2] += _visible(span)
    total = sum(size_chars.values())
    distinct = sorted(s for s, c in size_chars.items() if total and c / total >= 0.02)
    if len(distinct) > cfg.max_distinct_sizes:
        findings.append(
            make_finding(
                rule_id="FMT_TOO_MANY_SIZES",
                category=FindingCategory.FORMATTING,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.LOW,
                rubric=SF,
                explanation=(
                    f"{len(distinct)} different font sizes are used "
                    f"({', '.join(f'{s:g}' for s in distinct)} pt)."
                ),
                correction="Use one size for body text, one for headings and one for your name.",
                method=DetectionMethod.FORMAT_METADATA,
                confidence=0.7,
            )
        )

    for tl in _body_lines(ctx):
        small = [s for s in tl.line.spans if s.size and s.size < cfg.min_body_font_pt]
        chars = sum(_visible(s) for s in small)
        if chars < cfg.min_small_text_chars:
            continue
        smallest = min(s.size for s in small if s.size)
        very = smallest < cfg.very_small_font_pt
        findings.append(
            make_finding(
                rule_id="FMT_SMALL_TEXT",
                category=FindingCategory.FORMATTING,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM if very else FindingSeverity.LOW,
                rubric=SF,
                ats_impact=very,
                explanation=(
                    f"Text is set at {smallest:g} pt, below the readable minimum of "
                    f"{cfg.min_body_font_pt:g} pt."
                ),
                correction=f"Increase this text to at least {cfg.min_body_font_pt:g}-10 pt.",
                method=DetectionMethod.FORMAT_METADATA,
                confidence=0.9,
                detected_text=tl.text,
                location=locate(tl),
                section=tl.section,
                evidence={"font_size_pt": smallest},
            )
        )

    # Body paragraphs whose size differs from the document body size (after the name/contact
    # area, excluding headings and bold lead-ins).
    body = ctx.body_size
    if body:
        by_block: dict[str, list[TextLine]] = defaultdict(list)
        for tl in _body_lines(ctx):
            if tl.section and not tl.is_heading and tl.bold_ratio < 0.5:
                by_block[tl.block_id].append(tl)
        for lines in by_block.values():
            sizes = [s.size for t in lines for s in t.line.spans if s.size and _visible(s)]
            letters = sum(len(t.text) for t in lines)
            if not sizes or letters < 20:
                continue
            size = statistics.median(sizes)
            if size < cfg.min_body_font_pt or abs(size - body) < 1.0 or size > body * 1.3:
                continue
            findings.append(
                make_finding(
                    rule_id="FMT_INCONSISTENT_BODY_SIZE",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        f"This paragraph is {size:g} pt while the rest of the body text is "
                        f"{body:g} pt."
                    ),
                    correction=f"Use the same size ({body:g} pt) for all body text.",
                    method=DetectionMethod.FORMAT_METADATA,
                    confidence=0.7,
                    detected_text=lines[0].text,
                    location=block_location(lines[0].block)
                    if ctx.has_coordinates
                    else locate(lines[0]),
                    section=lines[0].section,
                    evidence={"size_pt": size, "body_size_pt": body},
                )
            )
    return findings


# --------------------------------------------------------------------------- spacing


def _columns(ctx: AnalysisContext) -> dict[tuple[int, int | None], list[TextLine]]:
    groups: dict[tuple[int, int | None], list[TextLine]] = defaultdict(list)
    for tl in _body_lines(ctx):
        if tl.page is None or tl.bbox is None:
            continue
        groups[(tl.page, tl.block.column)].append(tl)
    # Full-width lines (column None) belong to every column sequence of their page.
    pages = {p for p, _ in groups}
    out: dict[tuple[int, int | None], list[TextLine]] = {}
    for page in pages:
        cols = [c for p, c in groups if p == page and c is not None]
        full = groups.get((page, None), [])
        if not cols:
            out[(page, 0)] = sorted(full, key=lambda t: t.bbox[1])
            continue
        for col in cols:
            out[(page, col)] = sorted(groups[(page, col)] + full, key=lambda t: t.bbox[1])
    return out


def check_spacing(ctx: AnalysisContext) -> list[Finding]:
    if not ctx.has_coordinates:
        return _docx_spacing(ctx)
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    body = ctx.body_size or 10.0

    # Line spacing inside paragraphs (PyMuPDF blocks with 2+ lines).
    ratios: dict[str, float] = {}
    block_lines: dict[str, list[TextLine]] = defaultdict(list)
    for tl in (t for t in _body_lines(ctx) if _has_text(t.text)):
        if not tl.is_heading:
            block_lines[tl.block_id].append(tl)
    for block_id, lines in block_lines.items():
        lines = sorted(lines, key=lambda t: t.bbox[1])
        pitches = [
            b.bbox[1] - a.bbox[1]
            for a, b in zip(lines, lines[1:], strict=False)
            if b.bbox[1] - a.bbox[1] > 0.5
        ]
        sizes = [t.size for t in lines if t.size]
        if pitches and sizes:
            ratios[block_id] = statistics.median(pitches) / statistics.median(sizes)
    if len(ratios) >= cfg.min_lines_for_spacing:
        typical = statistics.median(ratios.values())
        for block_id, ratio in ratios.items():
            if abs(ratio - typical) / typical <= cfg.line_spacing_tolerance:
                continue
            first = block_lines[block_id][0]
            findings.append(
                make_finding(
                    rule_id="FMT_LINE_SPACING",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        f"Line spacing in this paragraph ({ratio:.2f}x the font size) differs "
                        f"from the rest of the document ({typical:.2f}x)."
                    ),
                    correction="Use the same line spacing for all paragraphs.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.7,
                    detected_text=first.text,
                    location=block_location(first.block),
                    section=first.section,
                    evidence={
                        "line_pitch_ratio": round(ratio, 3),
                        "document_ratio": round(typical, 3),
                    },
                )
            )

    # Large vertical gaps between consecutive lines in the same column.
    images = [i for i in ctx.content.images if i.bbox is not None]
    unit = body * 1.2
    seen: set[tuple[int, float, float]] = set()
    pages = {p.number: p for p in ctx.content.pages}
    for (page_no, _col), lines in _columns(ctx).items():
        for prev, nxt in zip(lines, lines[1:], strict=False):
            gap = nxt.bbox[1] - prev.bbox[3]
            if gap < cfg.large_gap_lines * unit:
                continue
            key = (page_no, round(prev.bbox[3]), round(nxt.bbox[1]))
            if key in seen:
                continue
            seen.add(key)
            x0 = min(prev.bbox[0], nxt.bbox[0])
            x1 = max(prev.bbox[2], nxt.bbox[2])
            region = (round(x0, 2), round(prev.bbox[3], 2), round(x1, 2), round(nxt.bbox[1], 2))
            if any(
                i.page == page_no
                and i.bbox[1] < region[3]
                and i.bbox[3] > region[1]
                and i.bbox[0] < region[2]
                and i.bbox[2] > region[0]
                for i in images
            ):
                continue  # the space holds an image (photo, logo, chart)
            findings.append(
                make_finding(
                    rule_id="FMT_LARGE_GAP",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.MEDIUM
                    if gap > 2 * cfg.large_gap_lines * unit
                    else FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        f"There is an empty vertical gap of {gap:.0f} pt (about "
                        f"{gap / unit:.0f} lines) before this text."
                    ),
                    correction="Remove the extra blank space so sections follow each other evenly.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.8,
                    detected_text=nxt.text,
                    location=Location(page_no, region, LocationPrecision.REGION, nxt.block_id),
                    section=nxt.section,
                    evidence={"gap_pt": round(gap, 1), "previous_line": prev.text[:80]},
                )
            )

    # Non-final pages that end far above the bottom while the next page has text.
    last_y: dict[int, float] = {}
    for tl in _body_lines(ctx):
        if tl.page is not None and tl.bbox is not None:
            last_y[tl.page] = max(last_y.get(tl.page, 0.0), tl.bbox[3])
    for page_no, bottom in last_y.items():
        page = pages.get(page_no)
        if page is None or (page_no + 1) not in last_y:
            continue
        empty = (page.height_pt - bottom) / page.height_pt
        if empty >= cfg.trailing_whitespace_ratio:
            findings.append(
                make_finding(
                    rule_id="FMT_PAGE_UNDERFILLED",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        f"Page {page_no} ends with {empty:.0%} of the page empty although "
                        "content continues on the next page."
                    ),
                    correction="Remove the manual page break or blank lines so the text flows.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.7,
                    location=Location(
                        page_no,
                        (0.0, round(bottom, 2), page.width_pt, page.height_pt),
                        LocationPrecision.REGION,
                    ),
                    evidence={"empty_ratio": round(empty, 3)},
                )
            )
    return findings


def _docx_spacing(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    spacing_values: Counter[float] = Counter()
    for tl in _body_lines(ctx):
        fmt = tl.block.format
        if fmt is None or tl.is_heading or tl.block.kind is not BlockKind.PARAGRAPH:
            continue
        if fmt.line_spacing is not None and fmt.line_spacing_unit == "multiple":
            spacing_values[fmt.line_spacing] += 1
        big = max(fmt.space_before_pt or 0.0, fmt.space_after_pt or 0.0)
        if big >= cfg.docx_large_spacing_pt:
            findings.append(
                make_finding(
                    rule_id="FMT_LARGE_GAP",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=f"This paragraph has {big:g} pt of extra spacing around it.",
                    correction="Reduce the paragraph spacing to match the rest of the document.",
                    method=DetectionMethod.FORMAT_METADATA,
                    confidence=0.75,
                    detected_text=tl.text,
                    location=locate(tl),
                    section=tl.section,
                    evidence={
                        "space_before_pt": fmt.space_before_pt,
                        "space_after_pt": fmt.space_after_pt,
                    },
                )
            )
    common = {v for v, n in spacing_values.items() if n >= 2}
    if len(common) > cfg.docx_max_line_spacing_values:
        findings.append(
            make_finding(
                rule_id="FMT_LINE_SPACING",
                category=FindingCategory.FORMATTING,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.LOW,
                rubric=SF,
                explanation=(
                    "Body paragraphs use different line spacing values: "
                    + ", ".join(f"{v:g}" for v in sorted(common))
                    + "."
                ),
                correction="Apply one line-spacing value to all body paragraphs.",
                method=DetectionMethod.FORMAT_METADATA,
                confidence=0.7,
            )
        )
    return findings


# --------------------------------------------------------------------------- alignment


def check_alignment(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    if not ctx.has_coordinates:
        counts: Counter[str] = Counter()
        for tl in _body_lines(ctx):
            fmt = tl.block.format
            if (
                fmt
                and not tl.is_heading
                and tl.block.kind is BlockKind.PARAGRAPH
                and len(tl.text) > 40
            ):
                counts[
                    {"both": "justify", "distribute": "justify"}.get(
                        fmt.alignment or "left", fmt.alignment or "left"
                    )
                ] += 1
        total = sum(counts.values())
        main = [a for a, n in counts.items() if n >= 3 and n / total >= 0.2] if total else []
        if len(main) > 1:
            return [
                make_finding(
                    rule_id="FMT_MIXED_ALIGNMENT",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.RECOMMENDATION,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        "Body paragraphs mix alignments: "
                        + ", ".join(f"{a} ({counts[a]})" for a in sorted(main))
                        + "."
                    ),
                    correction="Use one alignment (usually left) for all body paragraphs.",
                    method=DetectionMethod.FORMAT_METADATA,
                    confidence=0.7,
                )
            ]
        return []

    pages = {p.number: p for p in ctx.content.pages}
    edges: dict[int | None, list[float]] = defaultdict(list)
    lines_at: dict[tuple[int | None, float], TextLine] = {}
    for tl in _body_lines(ctx):
        if tl.is_heading or tl.bbox is None or tl.page not in pages or not _has_text(tl.text):
            continue  # bullet-only lines have their own left edge but are not text
        width = pages[tl.page].width_pt
        centre = (tl.bbox[0] + tl.bbox[2]) / 2
        if abs(centre - width / 2) < 10 and tl.bbox[0] > 0.2 * width:
            continue  # centred line
        edges[tl.block.column].append(tl.bbox[0])
        lines_at.setdefault((tl.block.column, tl.bbox[0]), tl)
    findings: list[Finding] = []
    for column, xs in edges.items():
        clusters: list[list[float]] = []
        for x in sorted(xs):
            if clusters and x - clusters[-1][-1] <= 3.0:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        major = [c for c in clusters if len(c) >= 2]
        if len(major) > cfg.max_left_edge_clusters:
            rare = min(major, key=len)  # an example from the least used indentation
            example = lines_at.get((column, rare[0]))
            findings.append(
                make_finding(
                    rule_id="FMT_INCONSISTENT_ALIGNMENT",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        f"Text lines start at {len(major)} different left positions"
                        + (f" in column {column + 1}" if column is not None else "")
                        + ", suggesting inconsistent indentation."
                    ),
                    correction="Align body text and bullets to a small set of consistent indents.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.6,
                    detected_text=example.text if example else None,
                    location=locate(example) if example else None,
                    evidence={
                        "left_edges_pt": [str(round(statistics.median(c), 1)) for c in major][:10],
                        "left_edge_clusters": len(major),
                        "max_clusters": cfg.max_left_edge_clusters,
                        "cluster_tolerance_pt": 3.0,
                        "example": "a line from the least used indentation",
                    },
                )
            )
    return findings


# --------------------------------------------------------------------------- margins


def check_margins(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []

    def finding(
        rule: str, side: str, value: float, page: int | None, narrow: bool, limit: float
    ) -> Finding:
        return make_finding(
            rule_id=rule,
            category=FindingCategory.FORMATTING,
            finding_class=FindingClass.POTENTIAL_ISSUE,
            severity=FindingSeverity.LOW,
            rubric=SF,
            explanation=(
                f"The {side} margin is {value:.0f} pt ({value / 72:.2f} in), which is "
                + (
                    "very narrow; text may be cut off when printed."
                    if narrow
                    else "unusually wide and wastes space."
                )
            ),
            correction=("Use margins between about 0.5 and 1 inch (36-72 pt)."),
            method=DetectionMethod.GEOMETRY
            if ctx.has_coordinates
            else DetectionMethod.FORMAT_METADATA,
            confidence=0.75,
            location=Location(
                page, None, LocationPrecision.PAGE if page else LocationPrecision.NONE
            ),
            evidence={
                "side": side,
                "margin_pt": round(value, 1),
                "threshold_pt": round(limit, 1),
                "measured_from": "body text extents"
                if ctx.has_coordinates
                else "document section settings",
            },
        )

    if not ctx.has_coordinates:
        for sec in ctx.content.docx_sections[:1]:
            for side, value in (
                ("left", sec.margin_left_pt),
                ("right", sec.margin_right_pt),
                ("top", sec.margin_top_pt),
                ("bottom", sec.margin_bottom_pt),
            ):
                if value is None:
                    continue
                limit = (
                    cfg.max_top_margin_pt if side in ("top", "bottom") else cfg.max_side_margin_pt
                )
                if value > limit:
                    findings.append(finding("FMT_WIDE_MARGIN", side, value, None, False, limit))
                elif value < cfg.min_margin_pt:
                    findings.append(
                        finding("FMT_NARROW_MARGIN", side, value, None, True, cfg.min_margin_pt)
                    )
        return findings

    for page in ctx.content.pages:
        boxes = [
            b.bbox
            for b in ctx.content.blocks
            if b.page == page.number and b.bbox is not None and b.region is Region.BODY
        ]
        if not boxes:
            continue
        left = min(b[0] for b in boxes)
        right = page.width_pt - max(b[2] for b in boxes)
        top = min(b[1] for b in boxes)
        # Left/top margins are measurable from text extents. The right side is only
        # checked for text too close to the edge: ragged-right (left-aligned) lines make a
        # wide right "margin" indistinguishable from short lines.
        if left > cfg.max_side_margin_pt:
            findings.append(
                finding("FMT_WIDE_MARGIN", "left", left, page.number, False, cfg.max_side_margin_pt)
            )
        for side, value in (("left", left), ("right", right)):
            if value < cfg.min_margin_pt:
                findings.append(
                    finding("FMT_NARROW_MARGIN", side, value, page.number, True, cfg.min_margin_pt)
                )
        if page.number == 1 and top > cfg.max_top_margin_pt:
            findings.append(
                finding("FMT_WIDE_MARGIN", "top", top, page.number, False, cfg.max_top_margin_pt)
            )
        if top < cfg.min_margin_pt * 0.5:
            findings.append(
                finding("FMT_NARROW_MARGIN", "top", top, page.number, True, cfg.min_margin_pt * 0.5)
            )
    # Report each side once per document (first page that shows it).
    unique: dict[tuple[str, str], Finding] = {}
    for f in findings:
        unique.setdefault((f.rule_id, str(f.evidence.get("side"))), f)
    return list(unique.values())


# --------------------------------------------------------------------------- garbled text


def check_garbled(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    for tl in ctx.all_lines():
        visible = [c for c in tl.text if not c.isspace()]
        if len(visible) < 5:
            continue
        cid = len(_CID_RE.findall(tl.text))
        bad = sum(1 for c in visible if is_garbled_char(c)) + cid * 6
        ratio = bad / len(visible)
        if ratio < cfg.garbled_line_ratio:
            continue
        findings.append(
            make_finding(
                rule_id="FMT_GARBLED_TEXT",
                category=FindingCategory.FORMATTING,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM,
                rubric=SF,
                ats_impact=True,
                explanation=(
                    "This text cannot be read correctly from the file (font encoding "
                    f"problem: {ratio:.0%} unreadable characters). ATS software will see "
                    "the same garbled text."
                ),
                correction=(
                    "Re-export the resume to PDF with fonts embedded (e.g. 'Save as PDF' from "
                    "Word), or use a standard font for this text."
                ),
                method=DetectionMethod.FORMAT_METADATA,
                confidence=0.85,
                review_required=True,
                detected_text=tl.text,
                location=locate(tl),
                section=tl.section,
                evidence={"garbled_ratio": round(ratio, 3)},
            )
        )
    return findings


def check_section_alignment(ctx: AnalysisContext) -> list[Finding]:
    """A single line inside a section that starts a few points off the left edge shared by
    the section's other lines. Measured from line boxes; deliberate indents (larger offsets,
    bullet text, wrapped bullet lines, centred or right-hand text) are not reported."""
    cfg = ctx.settings.analysis.formatting
    if not ctx.has_coordinates:
        return []
    pages = {p.number: p for p in ctx.content.pages}
    tol, max_off = cfg.misaligned_tolerance_pt, cfg.misaligned_max_offset_pt
    groups: dict[tuple, list[TextLine]] = defaultdict(list)
    bullet_text_x: dict[tuple, list[float]] = defaultdict(list)
    previous: dict[tuple, TextLine] = {}
    continuation: set[int] = set()
    for tl in _body_lines(ctx):
        if tl.bbox is None or tl.page not in pages or tl.section is None or tl.is_heading:
            continue
        key = (tl.section, tl.page, tl.block.column)
        prev = previous.get(key)
        if not _has_text(tl.text):
            previous[key] = tl
            continue
        width = pages[tl.page].width_pt
        if tl.bbox[0] > 0.5 * width or (
            abs((tl.bbox[0] + tl.bbox[2]) / 2 - width / 2) < 10 and tl.bbox[0] > 0.2 * width
        ):
            continue  # right-hand (dates) or centred text
        spans = [sp for sp in tl.line.spans if sp.bbox is not None]
        if tl.is_bullet and len(spans) > 1:
            bullet_text_x[key].append(spans[1].bbox[0])
        if prev is not None and (prev.is_bullet or not _has_text(prev.text)) and not tl.is_bullet:
            continuation.add(id(tl))  # wrapped bullet text or text after a bullet glyph line
        groups[key].append(tl)
        previous[key] = tl
    findings: list[Finding] = []
    for (section, page, column), lines in groups.items():
        clusters: list[list[TextLine]] = []
        for tl in sorted(lines, key=lambda t: t.bbox[0]):  # type: ignore[index]
            if clusters and tl.bbox[0] - clusters[-1][-1].bbox[0] <= tol:  # type: ignore[index]
                clusters[-1].append(tl)
            else:
                clusters.append([tl])
        edges = [
            (statistics.median(t.bbox[0] for t in c), len(c))  # type: ignore[index]
            for c in clusters
            if len(c) >= cfg.misaligned_min_lines
        ]
        if not edges:
            continue
        for c in clusters:
            if len(c) != 1 or id(c[0]) in continuation:
                continue
            tl = c[0]
            x = tl.bbox[0]  # type: ignore[index]
            if any(abs(x - bx) <= tol for bx in bullet_text_x[(section, page, column)]):
                continue
            edge, count = min(edges, key=lambda e: abs(e[0] - x))
            offset = abs(x - edge)
            if not tol < offset <= max_off:
                continue
            findings.append(
                make_finding(
                    rule_id="FMT_MISALIGNED_LINE",
                    category=FindingCategory.FORMATTING,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.LOW,
                    rubric=SF,
                    explanation=(
                        f"In the {section} section this line starts {offset:.1f} pt away from "
                        f"the left edge shared by {count} other lines."
                    ),
                    correction="Align this line with the other lines of the section.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.6,
                    detected_text=tl.text,
                    location=locate(tl),
                    section=section,
                    evidence={
                        "line_x_pt": round(x, 1),
                        "shared_edge_pt": round(edge, 1),
                        "offset_pt": round(offset, 1),
                        "lines_at_shared_edge": count,
                        "tolerance_pt": tol,
                        "max_offset_pt": max_off,
                    },
                )
            )
            break  # one example per section and page
    return findings


def check_formatting(ctx: AnalysisContext) -> list[Finding]:
    findings: list[Finding] = []
    for check in (
        check_fonts,
        check_sizes,
        check_spacing,
        check_alignment,
        check_section_alignment,
        check_margins,
        check_garbled,
    ):
        findings.extend(check(ctx))
    return findings
