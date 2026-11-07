"""Milestone 4: annotated PDF copies and findings reports."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pps_ats.devtools import synthetic_analysis as sa
from pps_ats.reports.annotate import annotate_pdf, sha256_file
from pps_ats.reports.findings_report import write_findings_report
from pps_ats.schemas.batch import AnnotationStatus
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    LocationPrecision,
    RubricCriterion,
)
from pps_ats.scoring.engine import score_file


def _finding(issue_id: str, text: str | None, **kw) -> Finding:  # type: ignore[no-untyped-def]
    base = dict(
        issue_id=issue_id,
        rule_id=kw.pop("rule_id", "LANG_SPELLING"),
        category=FindingCategory.SPELLING,
        finding_class=kw.pop("finding_class", FindingClass.DEFINITE_DEFECT),
        severity=FindingSeverity.MEDIUM,
        rubric_criterion=RubricCriterion.LANGUAGE_TONE,
        detected_text=text,
        explanation="Possible misspelling.",
        suggested_correction="Check the spelling.",
        confidence=0.9,
        detection_method=DetectionMethod.DICTIONARY,
    )
    base.update(kw)
    return Finding(**base)


def _annots(path: Path) -> list[tuple[int, str, pymupdf.Rect, str]]:
    doc = pymupdf.open(path)
    out = [
        (p.number + 1, a.type[1], pymupdf.Rect(a.rect), a.info.get("subject", ""))
        for p in doc
        for a in p.annots()
    ]
    doc.close()
    return out


@pytest.fixture()
def repeated_pdf(tmp_path: Path) -> tuple[Path, list[pymupdf.Rect]]:
    """'Pyhton' on three lines; returns the line rects as PyMuPDF reports them."""
    path = sa.write_pdf(
        tmp_path / "repeated.pdf",
        [
            ("MEERA TESTCASE", "name"),
            ("SKILLS", "heading"),
            ("Languages: Pyhton, Java, SQL", "body"),
            ("Projects written in Pyhton and Java for coursework", "body"),
            ("Automation scripts in Pyhton for the lab", "body"),
        ],
    )
    doc = pymupdf.open(path)
    rects = doc[0].search_for("Pyhton")
    doc.close()
    assert len(rects) == 3
    return path, rects


def test_highlight_lands_on_the_detected_word(tmp_path, settings):
    src = sa.spelling_resume_pdf(tmp_path / "spelling.pdf")
    analysis, scoring = score_file(src, settings)
    out = tmp_path / "out" / "spelling.annotated.pdf"
    report = annotate_pdf(src, analysis, out, scoring, settings.reports)
    doc = pymupdf.open(src)
    page = doc[0]
    spelling = [f for f in analysis.findings if f.rule_id == "LANG_SPELLING"]
    assert spelling
    by_id = {o.issue_id: o for o in report.findings}
    for f in spelling:
        outcome = by_id[f.issue_id]
        assert outcome.status is AnnotationStatus.WORD and outcome.verified
        (rect,) = outcome.rects
        # the marked rectangle contains exactly the misspelled word
        assert page.get_textbox(pymupdf.Rect(rect)).strip() == f.detected_text
        # and lies inside the span reported by the parser
        span = pymupdf.Rect(f.bounding_box)
        assert (span & pymupdf.Rect(rect)).get_area() > 0.9 * pymupdf.Rect(rect).get_area()
    doc.close()
    subjects = {s.split()[-1] for *_, s in _annots(out)}
    assert {f.issue_id for f in spelling} <= subjects


def test_repeated_word_marks_only_the_finding_occurrence(tmp_path, repeated_pdf, settings):
    path, rects = repeated_pdf
    analysis, scoring = score_file(path, settings)
    target = rects[1]  # second occurrence: the finding's span is this line only
    line_box = (50.0, target.y0 - 0.5, 400.0, target.y1 + 0.5)
    finding = _finding(
        "LANG_SPELLING-x1",
        "Pyhton",
        page_number=1,
        bounding_box=line_box,
        location_precision=LocationPrecision.LINE,
    )
    analysis = analysis.model_copy(update={"findings": [finding]})
    out = tmp_path / "repeated.annotated.pdf"
    report = annotate_pdf(path, analysis, out, scoring, settings.reports)
    (outcome,) = report.findings
    assert outcome.status is AnnotationStatus.WORD
    marked = pymupdf.Rect(outcome.rects[0])
    assert abs(marked.y0 - target.y0) < 1 and abs(marked.x0 - target.x0) < 1
    assert not marked.intersects(rects[0]) and not marked.intersects(rects[2])


def test_repeated_word_without_context_is_not_guessed(tmp_path, repeated_pdf, settings):
    path, _ = repeated_pdf
    analysis, scoring = score_file(path, settings)
    finding = _finding("LANG_SPELLING-x2", "Pyhton", page_number=1)  # no bbox: ambiguous
    analysis = analysis.model_copy(update={"findings": [finding]})
    report = annotate_pdf(path, analysis, tmp_path / "a.pdf", scoring, settings.reports)
    assert report.findings[0].status is AnnotationStatus.NOT_LOCATED
    assert [s for *_, s in _annots(tmp_path / "a.pdf")] == ["legend"]


def test_missing_coordinates_are_listed_not_invented(tmp_path, settings):
    src = sa.template_resume_pdf(tmp_path / "t.pdf")
    analysis, scoring = score_file(src, settings)
    doc_level = _finding(
        "SEC_REQUIRED_MISSING-1",
        None,
        rule_id="SEC_REQUIRED_MISSING",
        finding_class=FindingClass.POTENTIAL_ISSUE,
    )
    analysis = analysis.model_copy(update={"findings": [*analysis.findings, doc_level]})
    out = tmp_path / "t.annotated.pdf"
    report = annotate_pdf(src, analysis, out, scoring, settings.reports)
    assert report.status_for(doc_level.issue_id) is AnnotationStatus.NOT_LOCATED
    doc = pymupdf.open(out)
    legend = next(a for a in doc[0].annots() if a.info["subject"] == "legend")
    assert "SEC_REQUIRED_MISSING" in legend.info["content"]
    # no annotation anywhere claims to locate the document-level finding
    assert all(doc_level.issue_id not in a.info["subject"] for p in doc for a in p.annots())
    doc.close()


def test_multiple_findings_on_one_page_use_distinct_types(tmp_path, settings):
    src = sa.template_resume_pdf(tmp_path / "t.pdf")
    analysis, scoring = score_file(src, settings)
    page_one = [f for f in analysis.findings if f.page_number == 1]
    assert len(page_one) >= 3
    out = tmp_path / "t.annotated.pdf"
    report = annotate_pdf(src, analysis, out, scoring, settings.reports)
    annots = [a for a in _annots(out) if a[3] != "legend"]
    located = [o for o in report.findings if o.status is not AnnotationStatus.NOT_LOCATED]
    assert len(annots) == len(located) == len(page_one)
    assert "Highlight" in {t for _, t, _, _ in annots}  # definite defects
    doc = pymupdf.open(out)
    for a in doc[0].annots():
        if a.info["subject"] == "legend":
            continue
        f = next(x for x in analysis.findings if a.info["subject"].endswith(x.issue_id))
        expected = {
            FindingClass.DEFINITE_DEFECT: {"Highlight", "Square"},
            FindingClass.POTENTIAL_ISSUE: {"Squiggly", "Square"},
            FindingClass.RECOMMENDATION: {"Underline", "Square"},
        }[f.finding_class]
        assert a.type[1] in expected
        assert f.explanation in a.info["content"]
    doc.close()


def test_original_is_never_modified(tmp_path, settings):
    src = sa.template_resume_pdf(tmp_path / "t.pdf")
    before = sha256_file(src)
    analysis, scoring = score_file(src, settings)
    report = annotate_pdf(src, analysis, tmp_path / "copy.pdf", scoring, settings.reports)
    assert report.source_unchanged and sha256_file(src) == before
    with pytest.raises(ValueError):
        annotate_pdf(src, analysis, src, scoring, settings.reports)
    assert sha256_file(src) == before


def test_docx_findings_report(tmp_path, settings):
    src = sa.template_resume_docx(tmp_path / "t.docx")
    analysis, scoring = score_file(src, settings)
    out = write_findings_report(analysis, scoring, tmp_path / "t.findings.pdf")
    doc = pymupdf.open(out)
    text = "".join(p.get_text() for p in doc)
    doc.close()
    assert "not a copy of the submission" in text
    assert "DOCX" in text
    for f in analysis.findings[:3]:
        assert f.rule_id in text
