"""Annotated PDF copies of analysed submissions (Milestone 4).

Rules:
* The original file is opened read-only and never saved; the copy goes to a different path
  and the original's SHA-256 is re-checked afterwards.
* A word-level mark is drawn only when the finding's detected text is found *inside the
  finding's own span/line box* (so a repeated word elsewhere is never marked) and the text
  under the mark is read back from the page and matches.
* Otherwise the parser's own box is used (``area``), or, without coordinates, the finding is
  listed in a page-1 summary note and in the findings report (``not_located``). No
  coordinates are ever estimated.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pymupdf

from pps_ats.config import ReportSettings
from pps_ats.schemas.batch import AnnotationReport, AnnotationStatus, FindingAnnotation
from pps_ats.schemas.findings import AnalysisResult, Finding, FindingClass, LocationPrecision
from pps_ats.schemas.scoring import ContributionStatus, ScoringResult

# finding class -> (markup type, RGB colour, label)
STYLES: dict[FindingClass, tuple[str, tuple[float, float, float], str]] = {
    FindingClass.DEFINITE_DEFECT: ("highlight", (1.0, 0.42, 0.42), "Definite defect"),
    FindingClass.POTENTIAL_ISSUE: ("squiggly", (0.95, 0.5, 0.0), "Potential issue"),
    FindingClass.RECOMMENDATION: ("underline", (0.1, 0.4, 0.95), "Recommendation"),
}
ANNOT_TITLE = "PPS ATS (provisional)"
LEGEND = (
    "PPS ATS annotations (provisional; faculty review is final).\n"
    "Red highlight = definite defect; orange squiggly underline = potential issue; "
    "blue underline = recommendation. A box means the location is the parser's text "
    "span/line, not an exact word."
)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _needle(finding: Finding) -> str | None:
    text = (finding.detected_text or "").strip()
    if not text or text.endswith("…"):  # truncated by the analysis: cannot match exactly
        return None
    return text


def score_effects(scoring: ScoringResult | None) -> dict[str, str]:
    """issue_id -> short human description of the finding's effect on CA1 marks."""
    out: dict[str, str] = {}
    if scoring is None:
        return out
    for c in scoring.contributions:
        if c.status is ContributionStatus.APPLIED and c.applied_points > 0:
            out[c.issue_id] = f"-{c.applied_points:g} {c.criterion}"
        else:
            out[c.issue_id] = f"no deduction ({c.reason or c.status.value})"
    return out


def comment_text(finding: Finding, effect: str | None) -> str:
    label = STYLES.get(finding.finding_class, ("", (0, 0, 0), finding.finding_class.value))[2]
    parts = [f"[{label}] {finding.rule_id} ({finding.severity.value})", finding.explanation]
    if finding.suggested_correction:
        parts.append(f"Suggestion: {finding.suggested_correction}")
    parts.append(f"CA1 effect: {effect or 'not part of CA1 marks'}")
    return "\n".join(parts)


def _verified(page: pymupdf.Page, rect: pymupdf.Rect, needle: str) -> bool:
    found = page.get_textbox(rect + (-0.5, -0.5, 0.5, 0.5))
    return _norm(needle) in _norm(found)


def _overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = pymupdf.Rect(a) & b
    return 0.0 if inter.is_empty else inter.get_area() / max(a.get_area(), 1e-6)


def _word_hits(
    page: pymupdf.Page, needle: str, box: pymupdf.Rect | None, cfg: ReportSettings
) -> tuple[list[pymupdf.Quad], str] | None:
    """Exactly one occurrence of ``needle``: inside ``box`` if given, else unique on the page.
    Returns ``None`` when there is no hit or the hit is ambiguous."""
    if box is not None:
        clip = box + (-cfg.clip_margin_x, -cfg.clip_margin_y, cfg.clip_margin_x, cfg.clip_margin_y)
        quads = [
            q for q in page.search_for(needle, clip=clip, quads=True) if _overlap(q.rect, box) > 0.5
        ]
        method = "span_clip_search"
    else:
        quads = page.search_for(needle, quads=True)
        method = "unique_on_page"
    if not quads:
        return None
    if len(quads) > 1:
        # Several matches (search is case-insensitive): keep the case-exact ones.
        first = needle.split()[0]
        exact = [q for q in quads if first in page.get_textbox(q.rect + (-1, -1, 1, 1))]
        quads = exact or quads
    if len(quads) == 1:
        return (quads, method) if _verified(page, quads[0].rect, needle) else None
    # Still several quads: only a phrase wrapped over lines inside its own box is accepted,
    # and only if the marked text is that phrase once (not two separate occurrences).
    if box is None or " " not in needle:
        return None
    marked = _norm(" ".join(page.get_textbox(q.rect + (-0.5, -0.5, 0.5, 0.5)) for q in quads))
    if _norm(needle) in marked and len(marked) <= 1.3 * len(_norm(needle)):
        return quads, method
    return None


def _add_markup(page: pymupdf.Page, quads: list[pymupdf.Quad], cls: FindingClass) -> pymupdf.Annot:
    kind, color, _ = STYLES[cls]
    if kind == "highlight":
        annot = page.add_highlight_annot(quads)
    elif kind == "squiggly":
        annot = page.add_squiggly_annot(quads)
    else:
        annot = page.add_underline_annot(quads)
    annot.set_colors(stroke=color)
    return annot


def _add_box(page: pymupdf.Page, rect: pymupdf.Rect, cls: FindingClass) -> pymupdf.Annot:
    _, color, _ = STYLES[cls]
    annot = page.add_rect_annot(rect + (-1, -1, 1, 1))
    annot.set_colors(stroke=color)
    annot.set_border(width=0.8, dashes=[2, 1] if cls is not FindingClass.DEFINITE_DEFECT else None)
    return annot


def _finish(annot: pymupdf.Annot, finding: Finding, effect: str | None) -> None:
    annot.set_info(
        title=ANNOT_TITLE,
        subject=f"{finding.rule_id} {finding.issue_id}",
        content=comment_text(finding, effect),
    )
    annot.update()


def _annotate_one(
    doc: pymupdf.Document, finding: Finding, effect: str | None, cfg: ReportSettings
) -> FindingAnnotation:
    base = {"issue_id": finding.issue_id, "rule_id": finding.rule_id, "page": finding.page_number}
    page_no = finding.page_number
    page = doc[page_no - 1] if page_no and 1 <= page_no <= doc.page_count else None
    box = pymupdf.Rect(finding.bounding_box) if finding.bounding_box else None
    needle = _needle(finding)
    textual = finding.location_precision in (
        LocationPrecision.SPAN,
        LocationPrecision.LINE,
        LocationPrecision.BLOCK,
    )

    # 1. word-level: the finding's text inside its own box (or unique on its page).
    if page is not None and needle and (textual or box is None):
        hit = _word_hits(page, needle, box if textual else None, cfg)
        if hit is not None:
            quads, method = hit
            annot = _add_markup(page, quads, finding.finding_class)
            _finish(annot, finding, effect)
            return FindingAnnotation(
                **base,
                status=AnnotationStatus.WORD,
                method=method,
                rects=[tuple(round(v, 2) for v in q.rect) for q in quads],  # type: ignore[misc]
                verified=True,
            )
    # 2. the parser's own coordinates.
    if page is not None and box is not None and not box.is_empty:
        if finding.location_precision is LocationPrecision.PAGE:
            return _page_note(page, finding, effect, base)
        annot = _add_box(page, box, finding.finding_class)
        _finish(annot, finding, effect)
        return FindingAnnotation(
            **base,
            status=AnnotationStatus.AREA,
            method=f"parser_bbox:{finding.location_precision.value}",
            rects=[tuple(round(v, 2) for v in box)],  # type: ignore[misc]
            note="text not matched exactly inside the span; parser box used",
        )
    if page is not None and finding.location_precision is LocationPrecision.PAGE:
        return _page_note(page, finding, effect, base)
    # 3. document-level text with no page: only a unique match anywhere is acceptable.
    if page is None and needle and finding.location_precision is LocationPrecision.NONE:
        hits = [(p, _word_hits(p, needle, None, cfg)) for p in doc]
        unique = [(p, h) for p, h in hits if h is not None]
        total = sum(len(p.search_for(needle)) for p in doc)
        if len(unique) == 1 and total == len(unique[0][1][0]):
            p, (quads, _) = unique[0]
            annot = _add_markup(p, quads, finding.finding_class)
            _finish(annot, finding, effect)
            return FindingAnnotation(
                issue_id=finding.issue_id,
                rule_id=finding.rule_id,
                page=p.number + 1,
                status=AnnotationStatus.WORD,
                method="unique_in_document",
                rects=[tuple(round(v, 2) for v in q.rect) for q in quads],  # type: ignore[misc]
                verified=True,
            )
    return FindingAnnotation(
        **base, status=AnnotationStatus.NOT_LOCATED, note="no reliable coordinates"
    )


def _page_note(
    page: pymupdf.Page, finding: Finding, effect: str | None, base: dict
) -> FindingAnnotation:
    _, color, _ = STYLES[finding.finding_class]
    annot = page.add_text_annot(pymupdf.Point(page.rect.width - 24, 24), "", icon="Note")
    annot.set_colors(stroke=color)
    _finish(annot, finding, effect)
    return FindingAnnotation(**base, status=AnnotationStatus.PAGE_NOTE, method="page_note")


def annotate_pdf(
    source: str | Path,
    analysis: AnalysisResult,
    output: str | Path,
    scoring: ScoringResult | None = None,
    cfg: ReportSettings | None = None,
) -> AnnotationReport:
    """Write an annotated copy of ``source`` to ``output``. The source is never written."""
    cfg = cfg or ReportSettings()
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError("annotated output must not overwrite the original submission")
    before = sha256_file(source)
    effects = score_effects(scoring)
    outcomes: list[FindingAnnotation] = []
    doc = pymupdf.open(source)
    try:
        if doc.is_encrypted or doc.page_count == 0:
            raise ValueError("PDF cannot be annotated (encrypted or empty)")
        for finding in analysis.findings:
            outcomes.append(_annotate_one(doc, finding, effects.get(finding.issue_id), cfg))
        unlocated = [
            f
            for f, o in zip(analysis.findings, outcomes, strict=True)
            if o.status is AnnotationStatus.NOT_LOCATED
        ]
        summary = LEGEND
        if unlocated:
            listed = "\n".join(f"- {f.rule_id}: {f.explanation}" for f in unlocated)
            summary += f"\n\n{len(unlocated)} finding(s) without a reliable location:\n{listed}"
        note = doc[0].add_text_annot(pymupdf.Point(8, 8), summary, icon="Help")
        note.set_info(title=ANNOT_TITLE, subject="legend")
        note.update()
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_name(output.name + ".part")
        doc.save(tmp, garbage=1, deflate=True)
    finally:
        doc.close()
    os.replace(tmp, output)
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status.value] = counts.get(o.status.value, 0) + 1
    return AnnotationReport(
        kind="annotated_pdf",
        output_file=str(output),
        source_unchanged=sha256_file(source) == before,
        findings=outcomes,
        counts=counts,
    )


def report_only(analysis: AnalysisResult, status: AnnotationStatus) -> AnnotationReport:
    outcomes = [
        FindingAnnotation(issue_id=f.issue_id, rule_id=f.rule_id, page=f.page_number, status=status)
        for f in analysis.findings
    ]
    counts = {status.value: len(outcomes)} if outcomes else {}
    return AnnotationReport(kind="none", findings=outcomes, counts=counts)
