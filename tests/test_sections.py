from __future__ import annotations

from pps_ats.devtools import synthetic
from pps_ats.pipeline import scan_file
from pps_ats.schemas import SectionStatus

D, E, U, M = (
    SectionStatus.DETECTED,
    SectionStatus.EMPTY,
    SectionStatus.UNCERTAIN,
    SectionStatus.MISSING,
)


def _sections(result):
    return {s.name: s for s in result.sections}


def test_docx_section_statuses(samples, settings):
    sections = _sections(scan_file(samples["resume.docx"], settings))
    for name in ("contact", "summary", "education", "skills", "projects", "experience"):
        assert sections[name].status is D, (name, sections[name].evidence)
    assert sections["certifications"].status is E  # heading, nothing under it
    achievements = sections["achievements"]
    assert achievements.status is E and achievements.placeholder_hits >= 1
    assert sections["interests"].status is M
    assert sections["skills"].heading_text == "Technical Skills"
    assert "Git, Docker" in sections["skills"].content_text  # table content attached
    assert sections["education"].required and not sections["interests"].required


def test_pdf_sections_carry_heading_location(samples, settings):
    sections = _sections(scan_file(samples["resume_two_column.pdf"], settings))
    for name in ("skills", "education", "summary", "projects", "experience"):
        assert sections[name].status is D
        assert sections[name].page == 1 and sections[name].bbox is not None
    # Two-column reading order keeps column content with the right heading.
    assert "Python, Java" in sections["skills"].content_text
    assert "Campus Library" in sections["projects"].content_text


def test_inline_sublabel_does_not_create_new_section(samples, settings):
    sections = _sections(scan_file(samples["resume.pdf"], settings))
    assert sections["languages"].status is M  # "Languages: Python, ..." inside SKILLS
    assert "Languages: Python" in sections["skills"].content_text


def test_weak_heading_and_short_content_are_uncertain(tmp_path, settings):
    path = synthetic.resume_pdf(
        tmp_path / "r.pdf",
        extra_lines=[
            ("Leadership", "body"),  # plain single word, no heading formatting
            ("Led the college coding club for two semesters.", "body"),
            ("INTERESTS", "heading"),
            ("Chess", "body"),  # too little content
        ],
    )
    sections = _sections(scan_file(path, settings))
    assert sections["activities"].status is U
    assert sections["interests"].status is U


def test_contact_detection_without_heading(samples, settings):
    contact = _sections(scan_file(samples["resume.pdf"], settings))["contact"]
    assert contact.status is D
    assert any("emails" in e for e in contact.evidence)
    assert any("phone" in e for e in contact.evidence)


def test_sections_not_produced_for_invalid_documents(samples, settings):
    assert scan_file(samples["resume_scanned_image_only.pdf"], settings).sections == []
