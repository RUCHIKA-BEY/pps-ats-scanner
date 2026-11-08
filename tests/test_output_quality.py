"""Regression tests for bugs found in the real-output quality audit (synthetic PDFs only)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from pps_ats.analysis.orchestrator import analyze_file
from pps_ats.config import load_settings
from pps_ats.devtools import synthetic
from pps_ats.devtools.synthetic_analysis import write_pdf
from pps_ats.schemas.scoring import ScoreStatus
from pps_ats.scoring.engine import score_file

CONTACT = "riya.synthetic@example.com | +91 90000 44444 | linkedin.com/in/riya-synthetic"


def _line(page: pymupdf.Page, y: float, text: str, size: float = 10, bold: bool = False,
          x: float = 72) -> None:  # fmt: skip
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")


def bullet_resume(path: Path) -> Path:
    """Bullets drawn as separate text lines (as in several real PDFs)."""
    doc = pymupdf.open()
    page = doc.new_page()
    _line(page, 60, "RIYA SYNTHETIC", 18, True)
    _line(page, 84, CONTACT)
    _line(page, 110, "EDUCATION", 12.5, True)
    _line(page, 126, "B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026")
    _line(page, 152, "SKILLS", 12.5, True)
    _line(page, 168, "Python, Java, SQL, Git, Docker")
    _line(page, 194, "EXPERIENCE", 12.5, True)
    _line(page, 210, "•", 8)
    _line(page, 210, "Worked on the billing dashboard for the finance team in 2025", x=86)
    _line(page, 236, "CERTIFICATIONS", 12.5, True)
    for i, text in enumerate(["Python for Everybody", "Cloud Computing Basics"]):
        _line(page, 252 + 16 * i, "•", 8)
        _line(page, 252 + 16 * i, text, x=86)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def test_bullet_only_lines_do_not_capture_locations_or_hide_openers(tmp_path):
    path = bullet_resume(tmp_path / "b.pdf")
    analysis = analyze_file(path, load_settings())
    doc = pymupdf.open(path)
    certs = [f for f in analysis.findings if f.rule_id == "CQ_CERTIFICATION_DETAILS"]
    assert len(certs) == 2
    for f in certs:
        under = doc[0].get_textbox(pymupdf.Rect(f.bounding_box) + (-1, -1, 1, 1))
        assert f.detected_text.split()[-1] in under  # the text, not the bullet glyph
        assert pymupdf.Rect(f.bounding_box).width > 40
    # the weak opener sits after a bullet-only line and is still found
    assert any(f.rule_id == "CQ_WEAK_VERB" for f in analysis.findings)
    doc.close()


def test_vague_project_flags_the_title_without_inviting_invented_results(tmp_path):
    path = write_pdf(
        tmp_path / "v.pdf",
        [("RIYA SYNTHETIC", "name"), (CONTACT, "body"), ("PROJECTS", "heading"),
         ("Library Tracker", "entry"), ("Tracks books with Flask.", "body")],
    )  # fmt: skip
    f = next(x for x in analyze_file(path, load_settings()).findings
             if x.rule_id == "CQ_PROJECT_VAGUE")  # fmt: skip
    assert f.detected_text == "Library Tracker"
    with pymupdf.open(path) as doc:
        assert "Library Tracker" in doc[0].get_textbox(
            pymupdf.Rect(f.bounding_box) + (-1, -1, 1, 1)
        )
    assert "measurable" not in f.suggested_correction
    assert "only if it is real" in f.suggested_correction


def test_singular_skill_heading_is_recognised(tmp_path):
    lines = [("RIYA SYNTHETIC", "name"), (CONTACT, "body"), ("EDUCATION", "heading"),
             ("B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026",
              "body"), ("SKILL", "heading"), ("Python, Java, SQL, Git, Docker", "body"),
             ("PROJECTS", "heading"), ("Library Tracker", "entry"),
             ("Developed a Flask app with SQLite used by 200 students.", "body")]  # fmt: skip
    path = write_pdf(tmp_path / "s.pdf", lines)
    rules = [(f.rule_id, f.section) for f in analyze_file(path, load_settings()).findings]
    assert ("SEC_REQUIRED_MISSING", "skills") not in rules


def test_name_found_when_resume_opens_with_a_heading(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    _line(page, 60, "EDUCATION", 12.5, True)  # first in reading order
    _line(page, 76, "B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026")
    _line(page, 100, "SKILLS", 12.5, True)
    _line(page, 116, "Python, Java, SQL")
    _line(page, 30, "Riya Synthetic", 22, True, x=300)  # larger name drawn later (sidebar style)
    _line(page, 46, CONTACT, 8, x=300)
    path = tmp_path / "n.pdf"
    doc.save(path)
    doc.close()
    analysis = analyze_file(path, load_settings())
    assert analysis.identity.extracted.candidate_name == "Riya Synthetic"
    assert "IDENTITY_NAME_NOT_FOUND" not in [f.rule_id for f in analysis.findings]


def test_missing_contact_with_header_image_needs_human_review(tmp_path):
    def build(path: Path, image: bool) -> Path:
        doc = pymupdf.open()
        page = doc.new_page()
        if image:
            page.insert_image(pymupdf.Rect(50, 20, 300, 80), stream=synthetic._png_bytes(200, 40))
        y = 120
        for text, size in (("RIYA SYNTHETIC", 18), ("EDUCATION", 12.5),
                           ("B.Tech in Computer Engineering, Example Institute, 2022 - 2026", 10),
                           ("SKILLS", 12.5), ("Python, Java, SQL, Git", 10),
                           ("PROJECTS", 12.5), ("Library Tracker - Flask app", 10)):  # fmt: skip
            _line(page, y, text, size, size > 11)
            y += 22
        doc.save(path)
        doc.close()
        return path

    settings = load_settings()
    with_image, scoring = score_file(build(tmp_path / "i.pdf", True), settings)
    f = next(x for x in with_image.findings if x.rule_id == "SEC_CONTACT_MISSING")
    assert f.review_required and f.evidence["images_at_top_of_page_1"] is True
    assert scoring.status is ScoreStatus.NEEDS_REVIEW and not scoring.releasable
    plain, _ = score_file(build(tmp_path / "p.pdf", False), settings)
    g = next(x for x in plain.findings if x.rule_id == "SEC_CONTACT_MISSING")
    assert not g.review_required


def test_alignment_finding_has_a_location(tmp_path):
    items = [("RIYA SYNTHETIC", "name"), (CONTACT, "body"), ("EXPERIENCE", "heading")]
    for i, x in enumerate([50, 50, 70, 70, 90, 90, 110, 110, 130, 130, 150, 150]):
        items.append((f"Line {i} of experience details written at an indent", "body", {"x": x}))
    analysis = analyze_file(write_pdf(tmp_path / "a.pdf", items), load_settings())
    found = [f for f in analysis.findings if f.rule_id == "FMT_INCONSISTENT_ALIGNMENT"]
    assert found and found[0].page_number == 1 and found[0].bounding_box is not None


def glyph_bullet_resume(path: Path) -> Path:
    """Body in Helvetica; bullet glyphs drawn as separate lines in another font and size
    (as in several real resumes)."""
    doc = pymupdf.open()
    page = doc.new_page()
    _line(page, 60, "RIYA SYNTHETIC", 18, True)
    _line(page, 84, CONTACT)
    y = 112
    for heading, rows in (
        ("EDUCATION", ["B.Tech in Computer Engineering, Example Institute of Technology,",
                       "2022 - 2026, CGPA 8.4 / 10"]),
        ("EXPERIENCE", ["Built REST endpoints for the billing service in Python and Flask",
                        "Automated weekly reports with pandas, saving the team two hours",
                        "Wrote integration tests for the payments module with pytest"]),
    ):  # fmt: skip
        _line(page, y, heading, 12.5, True)
        y += 18
        for row in rows:
            page.insert_text((72, y), "•", fontsize=7, fontname="tiro")  # glyph-only line
            _line(page, y, row, x=86)
            y += 14
        y += 8
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def test_bullet_glyph_lines_are_not_formatting_problems(tmp_path):
    path = glyph_bullet_resume(tmp_path / "g.pdf")
    rules = [f.rule_id for f in analyze_file(path, load_settings()).findings]
    for rule in ("FMT_MIXED_BODY_FONTS", "FMT_LINE_SPACING", "FMT_INCONSISTENT_ALIGNMENT",
                 "FMT_INCONSISTENT_BODY_SIZE"):  # fmt: skip
        assert rule not in rules, rule


def test_education_location_skips_bullet_line(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    _line(page, 60, "RIYA SYNTHETIC", 18, True)
    _line(page, 84, CONTACT)
    _line(page, 112, "EDUCATION", 12.5, True)
    page.insert_text((72, 130), "•", fontsize=7, fontname="tiro")
    _line(page, 130, "B.Tech in Computer Engineering, Example Institute of Technology", x=86)
    path = tmp_path / "e.pdf"
    doc.save(path)
    doc.close()
    f = next(x for x in analyze_file(path, load_settings()).findings
             if x.rule_id == "CQ_EDUCATION_INCOMPLETE")  # fmt: skip
    assert pymupdf.Rect(f.bounding_box).width > 40
