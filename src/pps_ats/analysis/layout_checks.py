"""ATS layout-risk checks: columns, tables, text boxes, headers/footers, text in images.

A two-column layout, a table or an image is not penalised by itself. A finding is raised
only for an identifiable extraction risk measured in the document (e.g. side-by-side text
with a gutter too narrow to separate, contact details only inside a header, a section
whose content is an image). These findings are ATS-only (``rubric_criterion = none``)
unless stated otherwise.
"""

from __future__ import annotations

import re
from collections import defaultdict

from pps_ats.analysis.context import (
    AnalysisContext,
    Location,
    TextLine,
    locate,
    make_finding,
)
from pps_ats.analysis.sections import EMAIL_RE, find_phone_numbers
from pps_ats.parsers.text_utils import normalize_for_match
from pps_ats.schemas.common import SectionStatus
from pps_ats.schemas.content import BlockKind, Region
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    LocationPrecision,
    RubricCriterion,
)

_PAGE_NO_RE = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]{2,}")
NONE = RubricCriterion.NONE


def _has_contact(text: str) -> bool:
    return bool(EMAIL_RE.search(text) or find_phone_numbers(text))


def check_columns(ctx: AnalysisContext) -> list[Finding]:
    if not ctx.has_coordinates:
        return []
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    for page in ctx.content.pages:
        if page.layout.column_count > cfg.max_columns:
            findings.append(
                make_finding(
                    rule_id="LAYOUT_MANY_COLUMNS",
                    category=FindingCategory.LAYOUT,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.MEDIUM,
                    rubric=NONE,
                    ats_impact=True,
                    explanation=(
                        f"Page {page.number} has {page.layout.column_count} text columns; "
                        "many ATS parsers read across columns and mix the content."
                    ),
                    correction="Use a single-column (or simple two-column) layout.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.7,
                    location=Location(page.number, None, LocationPrecision.PAGE),
                )
            )

    # Side-by-side text with a narrow gutter that the parser could not separate.
    by_page: dict[int, list[TextLine]] = defaultdict(list)
    for tl in ctx.lines:
        if tl.page is not None and tl.bbox is not None:
            by_page[tl.page].append(tl)
    pages = {p.number: p for p in ctx.content.pages}
    for page_no, lines in by_page.items():
        if pages.get(page_no) and pages[page_no].layout.column_count > 1:
            continue  # columns were detected and read column-by-column
        pairs: list[tuple[TextLine, TextLine, float]] = []
        for i, a in enumerate(lines):
            for b in lines[i + 1 :]:
                if a.block_id == b.block_id:
                    continue
                left, right = (a, b) if a.bbox[0] <= b.bbox[0] else (b, a)
                overlap = min(left.bbox[3], right.bbox[3]) - max(left.bbox[1], right.bbox[1])
                height = min(left.bbox[3] - left.bbox[1], right.bbox[3] - right.bbox[1])
                if height <= 0 or overlap < 0.5 * height:
                    continue
                gap = right.bbox[0] - left.bbox[2]
                if not 0 < gap < cfg.narrow_gutter_pt:
                    continue
                if (
                    min(
                        len(_ALPHA_WORD_RE.findall(left.text)),
                        len(_ALPHA_WORD_RE.findall(right.text)),
                    )
                    < 3
                ):
                    continue  # e.g. right-aligned dates
                pairs.append((left, right, gap))
        if len(pairs) >= 3:
            left, right, _ = pairs[0]
            region = (
                round(min(p[0].bbox[0] for p in pairs), 2),
                round(min(p[0].bbox[1] for p in pairs), 2),
                round(max(p[1].bbox[2] for p in pairs), 2),
                round(max(p[1].bbox[3] for p in pairs), 2),
            )
            findings.append(
                make_finding(
                    rule_id="LAYOUT_COLUMN_MERGE_RISK",
                    category=FindingCategory.LAYOUT,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.MEDIUM,
                    rubric=NONE,
                    ats_impact=True,
                    explanation=(
                        f"{len(pairs)} lines sit side by side with only "
                        f"{min(p[2] for p in pairs):.0f} pt between them; ATS software may "
                        "merge the two columns into the same lines."
                    ),
                    correction="Widen the space between columns or use a single-column layout.",
                    method=DetectionMethod.GEOMETRY,
                    confidence=0.65,
                    detected_text=f"{left.text} | {right.text}",
                    location=Location(page_no, region, LocationPrecision.REGION, left.block_id),
                    evidence={"side_by_side_pairs": len(pairs)},
                )
            )
    return findings


def check_tables_and_textboxes(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []
    headings_by_table: dict[str, set[str]] = defaultdict(set)
    first_line: dict[str, TextLine] = {}
    for tl in ctx.lines:
        if tl.block.table_id:
            first_line.setdefault(tl.block.table_id, tl)
            if tl.is_heading and tl.section:
                headings_by_table[tl.block.table_id].add(tl.section)

    for table in ctx.content.tables:
        headings = headings_by_table.get(table.id, set())
        if table.cols < 2:
            continue
        if len(headings) >= 2:
            cls, sev = FindingClass.POTENTIAL_ISSUE, FindingSeverity.MEDIUM
            why = (
                f"Sections ({', '.join(sorted(headings))}) are laid out inside a "
                f"{table.rows}x{table.cols} table"
            )
        elif table.rows >= cfg.table_min_rows:
            cls, sev = FindingClass.RECOMMENDATION, FindingSeverity.LOW
            why = f"Content is arranged in a {table.rows}x{table.cols} table"
        else:
            continue
        tl = first_line.get(table.id)
        location = (
            Location(table.page, table.bbox, LocationPrecision.BLOCK)
            if table.bbox is not None
            else (locate(tl) if tl else Location(table.page))
        )
        findings.append(
            make_finding(
                rule_id="LAYOUT_TABLE",
                category=FindingCategory.LAYOUT,
                finding_class=cls,
                severity=sev,
                rubric=NONE,
                ats_impact=True,
                explanation=f"{why}; some ATS parsers read tables cell by cell out of order.",
                correction="Prefer plain paragraphs and bullet lists for resume content.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.6,
                detected_text=tl.text if tl else None,
                location=location,
                evidence={"table_id": table.id, "rows": table.rows, "cols": table.cols},
            )
        )

    boxes: dict[str, list[TextLine]] = defaultdict(list)
    for tl in ctx.all_lines():
        if tl.block.kind is BlockKind.TEXTBOX:
            boxes[tl.block_id].append(tl)
    for lines in boxes.values():
        text = " ".join(t.text for t in lines)
        contact = _has_contact(text)
        findings.append(
            make_finding(
                rule_id="LAYOUT_TEXT_BOX",
                category=FindingCategory.LAYOUT,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM if contact else FindingSeverity.LOW,
                rubric=NONE,
                ats_impact=True,
                explanation=(
                    "This text is inside a floating text box"
                    + (" and contains contact details" if contact else "")
                    + "; many ATS parsers skip text boxes."
                ),
                correction="Move the text out of the text box into the normal document body.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.75,
                detected_text=text,
                location=locate(lines[0]),
            )
        )
    return findings


def check_headers_footers(ctx: AnalysisContext) -> list[Finding]:
    findings: list[Finding] = []
    body_norm = normalize_for_match(ctx.content.full_text)
    by_block: dict[str, list[TextLine]] = defaultdict(list)
    for tl in ctx.hf_lines:
        by_block[tl.block_id].append(tl)

    signatures: dict[str, set[int]] = defaultdict(set)
    for lines in by_block.values():
        text = " ".join(t.text for t in lines)
        if lines[0].page is not None:
            signatures[re.sub(r"\d+", "#", normalize_for_match(text))].add(lines[0].page)

        emails = EMAIL_RE.findall(text)
        phones = find_phone_numbers(text)
        missing_in_body = [v for v in emails + phones if normalize_for_match(v) not in body_norm]
        if missing_in_body:
            findings.append(
                make_finding(
                    rule_id="LAYOUT_CONTACT_IN_HEADER",
                    category=FindingCategory.LAYOUT,
                    finding_class=FindingClass.POTENTIAL_ISSUE,
                    severity=FindingSeverity.MEDIUM,
                    rubric=NONE,
                    ats_impact=True,
                    explanation=(
                        "Contact details appear only in the page header/footer; many ATS "
                        "parsers ignore headers and footers."
                    ),
                    correction="Put your e-mail and phone number in the main body near the top.",
                    method=DetectionMethod.STRUCTURE,
                    confidence=0.8,
                    detected_text=text,
                    location=locate(lines[0]),
                )
            )

    for signature, page_set in signatures.items():
        if len(page_set) < 2 or _PAGE_NO_RE.match(signature.replace("#", "1")):
            continue
        if len(_ALPHA_WORD_RE.findall(signature)) < 3:
            continue
        example = next(
            t
            for lines in by_block.values()
            for t in lines
            if re.sub(r"\d+", "#", normalize_for_match(" ".join(x.text for x in lines)))
            == signature
        )
        findings.append(
            make_finding(
                rule_id="LAYOUT_REPEATED_HEADER",
                category=FindingCategory.LAYOUT,
                finding_class=FindingClass.RECOMMENDATION,
                severity=FindingSeverity.LOW,
                rubric=NONE,
                ats_impact=True,
                explanation=(
                    f"The same header/footer text repeats on {len(page_set)} pages; ATS "
                    "software may insert it into the middle of your content."
                ),
                correction="Keep headers/footers minimal (or remove them) on a resume.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.6,
                detected_text=example.text,
                location=locate(example),
                evidence={"pages": [str(p) for p in sorted(page_set)]},
            )
        )
    return findings


def check_images(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.formatting
    findings: list[Finding] = []

    if ctx.has_coordinates:
        # (a) a heading with no text under it, but an image directly below it.
        for section in ctx.scan.sections:
            if section.status is not SectionStatus.EMPTY or section.bbox is None:
                continue
            next_heading_y = min(
                (
                    s.bbox[1]
                    for s in ctx.scan.sections
                    if s.page == section.page and s.bbox is not None and s.bbox[1] > section.bbox[3]
                ),
                default=float("inf"),
            )
            for image in ctx.content.images:
                if image.page != section.page or image.bbox is None:
                    continue
                if not (section.bbox[3] - 2 <= image.bbox[1] < next_heading_y):
                    continue
                if image.bbox[2] < section.bbox[0] or image.bbox[0] > section.bbox[2] + 200:
                    continue
                findings.append(
                    make_finding(
                        rule_id="LAYOUT_TEXT_IN_IMAGE",
                        category=FindingCategory.LAYOUT,
                        finding_class=FindingClass.POTENTIAL_ISSUE,
                        severity=FindingSeverity.MEDIUM,
                        rubric=RubricCriterion.STRUCTURE_FORMATTING,
                        ats_impact=True,
                        explanation=(
                            f"The '{section.heading_text}' section has no readable text, but "
                            "an image sits directly below the heading; its content is "
                            "probably an image that ATS software cannot read."
                        ),
                        correction="Type this content as text instead of inserting an image.",
                        method=DetectionMethod.GEOMETRY,
                        confidence=0.7,
                        review_required=True,
                        detected_text=section.heading_text,
                        location=Location(image.page, image.bbox, LocationPrecision.BLOCK),
                        section=section.name,
                    )
                )
                break

        # (b) large images that no text overlaps (banners, screenshots of text).
        text_boxes = [(b.page, b.bbox) for b in ctx.content.blocks if b.bbox is not None]
        pages = {p.number: p for p in ctx.content.pages}
        for image in ctx.content.images:
            page = pages.get(image.page or 0)
            if page is None or image.bbox is None or image.region is not Region.BODY:
                continue
            area = (image.bbox[2] - image.bbox[0]) * (image.bbox[3] - image.bbox[1])
            if area / (page.width_pt * page.height_pt) < cfg.large_image_area_ratio:
                continue
            overlapped = any(
                p == image.page
                and bb[0] < image.bbox[2]
                and bb[2] > image.bbox[0]
                and bb[1] < image.bbox[3]
                and bb[3] > image.bbox[1]
                for p, bb in text_boxes
            )
            if overlapped:
                continue  # background artwork behind real text
            findings.append(
                _large_image_finding(Location(image.page, image.bbox, LocationPrecision.BLOCK))
            )
    else:
        area_ref = None
        if ctx.content.docx_sections:
            sec = ctx.content.docx_sections[0]
            if sec.page_width_pt and sec.page_height_pt:
                area_ref = sec.page_width_pt * sec.page_height_pt
        for image in ctx.content.images:
            if image.region is not Region.BODY or area_ref is None:
                continue
            area = (image.display_width_pt or 0) * (image.display_height_pt or 0)
            if area / area_ref >= cfg.large_image_area_ratio:
                findings.append(_large_image_finding(Location()))
    return findings


def _large_image_finding(location: Location) -> Finding:
    return make_finding(
        rule_id="LAYOUT_LARGE_IMAGE",
        category=FindingCategory.LAYOUT,
        finding_class=FindingClass.RECOMMENDATION,
        severity=FindingSeverity.LOW,
        rubric=NONE,
        ats_impact=True,
        explanation=(
            "A large image is placed in the body. If it contains text (e.g. skills or a "
            "certificate), ATS software cannot read it."
        ),
        correction="Make sure no important information exists only inside images.",
        method=DetectionMethod.GEOMETRY if location.bbox else DetectionMethod.FORMAT_METADATA,
        confidence=0.5,
        location=location,
    )


def check_layout(ctx: AnalysisContext) -> list[Finding]:
    findings: list[Finding] = []
    for check in (check_columns, check_tables_and_textboxes, check_headers_footers, check_images):
        findings.extend(check(ctx))
    return findings
