"""Regression tests for the scanner improvements derived from the faculty calibration pilot.

Synthetic documents only. Structural patterns (compound / modified section headings) were
observed in calibration documents; no held-out document or annotation was used.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pps_ats.analysis.orchestrator import analyze_file
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_analysis import write_pdf
from pps_ats.evaluation.compare import Detection, match_document
from pps_ats.evaluation.dataset import ReferenceAnnotation, ReferenceDocument
from pps_ats.schemas.scoring import ContributionStatus
from pps_ats.scoring.engine import score_file

NAME = ("RIYA SYNTHETIC", "name")
CONTACT = ("riya.synthetic@example.com | +91 90000 44444 | linkedin.com/in/riya-synthetic "
           "| github.com/riya-synthetic", "body")  # fmt: skip
EDU = [("EDUCATION", "heading"),
       ("B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026", "body"),
       ("CGPA 8.4 / 10", "body")]  # fmt: skip
SKILLS = [("SKILLS", "heading"), ("Python, Java, SQL, Git, Docker, Flask", "body")]
SUMMARY = [("CAREER OBJECTIVE", "heading"),
           ("Computer engineering student seeking a software engineering internship in "
            "backend development.", "body")]  # fmt: skip
PROJECT = [("Library Tracker", "entry"),
           ("Developed a Flask web application with SQLite that tracks book loans; used by "
            "200 students in the department library.", "body")]  # fmt: skip
NEW_RULES = {
    "CONTACT_LINKEDIN_MISSING", "CONTACT_CODE_PROFILE_MISSING", "CONTACT_LINK_TEXT_ONLY",
    "CONTACT_PLACEHOLDER_LINK", "CONTACT_EMAIL_MALFORMED", "CONTACT_PHONE_INCOMPLETE",
    "CONTACT_DETAILS_MULTILINE", "CQ_PROJECT_TEAM_ROLE_UNCLEAR", "CQ_PROJECT_NO_OUTCOME",
    "CQ_OBJECTIVE_NO_TARGET",
}  # fmt: skip


PROJECTS_HEADING = ("PROJECTS", "heading")


def resume(tmp: Path, name: str, *, contact=None, summary=None, projects_heading=PROJECTS_HEADING,
           projects=None, extra=None) -> Path:  # type: ignore[no-untyped-def]  # fmt: skip
    items = [NAME, *(contact or [CONTACT]), *(summary or SUMMARY), *EDU, *SKILLS]
    if projects_heading is not None:
        items.append(projects_heading)
    items += projects or PROJECT
    items += extra or []
    return write_pdf(tmp / name, items)


def findings(path: Path):  # type: ignore[no-untyped-def]
    return analyze_file(path, load_settings())


def rules(path: Path) -> list[str]:
    return [f.rule_id for f in findings(path).findings]


def section_status(path: Path, name: str) -> str:
    from pps_ats.pipeline import scan_file

    return {s.name: s.status.value for s in scan_file(path).sections}[name]


# --------------------------------------------------------------------------- sections


@pytest.mark.parametrize(
    "heading",
    [
        ("PROJECTS & ACHIEVEMENTS", "heading"),
        ("PROJECTS/INTERNSHIPS", "heading"),
        ("MAJOR PROJECT", "heading"),
        ("Projects Undertaken", "heading", {"size": 14.0}),
    ],
)
def test_unusual_project_headings_are_recognised(tmp_path, heading):
    path = resume(tmp_path, "r.pdf", projects_heading=heading)
    assert section_status(path, "projects") == "detected"


def test_compound_heading_evidences_both_sections(tmp_path):
    path = resume(tmp_path, "r.pdf", projects_heading=("PROJECTS & CERTIFICATIONS", "heading"))
    assert section_status(path, "projects") == "detected"
    assert section_status(path, "certifications") == "detected"


def test_inline_compound_certification_heading(tmp_path):
    path = resume(tmp_path, "r.pdf", extra=[
        ("Courses / Certifications: Python for Everybody (Coursera, 2024)", "body")])  # fmt: skip
    assert section_status(path, "certifications") in ("detected", "uncertain")


def test_project_titles_and_job_titles_are_not_headings(tmp_path):
    path = resume(
        tmp_path,
        "r.pdf",
        projects=[("Final Year Project", "entry"), *PROJECT[1:]],
        extra=[("EXPERIENCE", "heading"), ("PROJECT MANAGER INTERN", "entry"),
               ("Example Analytics, May 2025 - July 2025: coordinated sprint planning.", "body")],
    )  # fmt: skip
    from pps_ats.pipeline import scan_file

    sections = {s.name: s for s in scan_file(path).sections}
    assert "Final Year Project" in sections["projects"].content_text
    assert "PROJECT MANAGER INTERN" in sections["experience"].content_text
    assert sections["projects"].occurrences == 1


# --------------------------------------------------------------------------- contact


def add_link(path: Path, text: str, uri: str) -> Path:
    doc = pymupdf.open(path)
    page = doc[0]
    rect = page.search_for(text)[0]
    page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": uri})
    out = path.with_name("linked_" + path.name)
    doc.save(out)
    doc.close()
    return out


def test_profile_links_missing_are_recommendations_with_location(tmp_path):
    path = resume(tmp_path, "r.pdf", contact=[("riya.synthetic@example.com | +91 90000 44444",
                                                "body")])  # fmt: skip
    found = {f.rule_id: f for f in findings(path).findings}
    for rule in ("CONTACT_LINKEDIN_MISSING", "CONTACT_CODE_PROFILE_MISSING"):
        f = found[rule]
        assert f.finding_class.value == "recommendation" and f.severity.value == "low"
        assert f.page_number == 1 and f.bounding_box and f.suggested_correction
    assert not {"CONTACT_LINKEDIN_MISSING", "CONTACT_CODE_PROFILE_MISSING"} & set(
        rules(resume(tmp_path, "ok.pdf"))
    )


def test_no_code_profile_recommendation_without_projects(tmp_path):
    path = resume(tmp_path, "r.pdf", contact=[("riya.synthetic@example.com | +91 90000 44444 | "
                                                "linkedin.com/in/riya", "body")],
                  projects_heading=None, projects=[])  # fmt: skip
    assert "CONTACT_CODE_PROFILE_MISSING" not in rules(path)


def test_profile_named_without_url_or_hyperlink(tmp_path):
    contact = [("riya.synthetic@example.com | +91 90000 44444 | LinkedIn | GitHub", "body")]
    path = resume(tmp_path, "r.pdf", contact=contact)
    hits = [f for f in findings(path).findings if f.rule_id == "CONTACT_LINK_TEXT_ONLY"]
    assert sorted(f.detected_text for f in hits) == ["GitHub", "LinkedIn"]
    assert all(f.location_precision.value == "span" for f in hits)
    linked = add_link(path, "LinkedIn", "https://www.linkedin.com/in/riya-synthetic")
    remaining = [f.detected_text for f in findings(linked).findings
                 if f.rule_id == "CONTACT_LINK_TEXT_ONLY"]  # fmt: skip
    assert remaining == ["GitHub"]


def test_placeholder_hyperlinks_and_handle_less_urls(tmp_path):
    contact = [("riya.synthetic@example.com | +91 90000 44444 | LinkedIn Profile", "body")]
    path = add_link(resume(tmp_path, "r.pdf", contact=contact), "LinkedIn Profile",
                    "https://www.linkedin.com/in/yourname")  # fmt: skip
    hit = [f for f in findings(path).findings if f.rule_id == "CONTACT_PLACEHOLDER_LINK"]
    assert len(hit) == 1 and hit[0].finding_class.value == "definite_defect"
    assert hit[0].bounding_box is not None and "template handle" in hit[0].evidence["reason"]
    written = resume(tmp_path, "w.pdf", contact=[
        ("riya.synthetic@example.com | +91 90000 44444 | linkedin.com/in/ | github.com/riya",
         "body")])  # fmt: skip
    assert rules(written).count("CONTACT_PLACEHOLDER_LINK") == 1
    real = add_link(resume(tmp_path, "real.pdf", contact=contact), "LinkedIn Profile",
                    "https://www.linkedin.com/in/riya-synthetic")  # fmt: skip
    assert "CONTACT_PLACEHOLDER_LINK" not in rules(real)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("riya.synthetic@gmail | +91 90000 44444", True),
        ("riya.synthetic @gmail.com | +91 90000 44444", True),
        ("riya.synthetic@example.com | +91 90000 44444 | follow @riya_dev", False),
        ("riya.synthetic@example.com | +91 90000 44444", False),
    ],
)
def test_malformed_email(tmp_path, line, expected):
    path = resume(tmp_path, "r.pdf", contact=[(line + " | linkedin.com/in/riya-s", "body")])
    assert ("CONTACT_EMAIL_MALFORMED" in rules(path)) is expected


def test_incomplete_phone_but_not_years(tmp_path):
    short = resume(tmp_path, "s.pdf", contact=[
        ("riya.synthetic@example.com | Mobile: 98765 432 | linkedin.com/in/riya-s",
         "body")])  # fmt: skip
    hit = [f for f in findings(short).findings if f.rule_id == "CONTACT_PHONE_INCOMPLETE"]
    assert len(hit) == 1 and hit[0].evidence["digits"] == 8
    assert "CONTACT_PHONE_INCOMPLETE" not in rules(resume(tmp_path, "ok.pdf"))


def test_multiline_contact_is_an_optional_style_note(tmp_path):
    contact = [("riya.synthetic@example.com", "body"), ("+91 90000 44444", "body"),
               ("linkedin.com/in/riya-synthetic", "body"),
               ("github.com/riya-synthetic", "body")]  # fmt: skip
    f = next(x for x in findings(resume(tmp_path, "r.pdf", contact=contact)).findings
             if x.rule_id == "CONTACT_DETAILS_MULTILINE")  # fmt: skip
    assert f.finding_class.value == "recommendation" and f.rubric_criterion.value == "none"
    assert f.evidence["style_preference"] is True
    assert "CONTACT_DETAILS_MULTILINE" not in rules(resume(tmp_path, "one.pdf"))


# --------------------------------------------------------------------------- projects


def test_team_project_without_individual_role(tmp_path):
    team = [("Attendance System", "entry"),
            ("We developed a face recognition attendance system in a team of four", "body"),
            ("using OpenCV and Flask; it was used by 120 students.", "body")]  # fmt: skip
    hit = [f for f in findings(resume(tmp_path, "t.pdf", projects=team)).findings
           if f.rule_id == "CQ_PROJECT_TEAM_ROLE_UNCLEAR"]  # fmt: skip
    assert len(hit) == 1 and hit[0].severity.value == "medium"
    assert "we" in hit[0].evidence["team_markers"]
    mine = [*team, ("My role: built the recognition pipeline.", "body")]
    assert "CQ_PROJECT_TEAM_ROLE_UNCLEAR" not in rules(resume(tmp_path, "m.pdf", projects=mine))


def test_missing_outcome_is_a_recommendation_without_invented_results(tmp_path):
    long_desc = [("Expense Tracker", "entry"),
                 ("Designed and implemented an expense tracking web application with", "body"),
                 ("React and Node.js that lets students record daily spending, group", "body"),
                 ("expenses into categories and export monthly summaries.", "body")]  # fmt: skip
    f = next(x for x in findings(resume(tmp_path, "o.pdf", projects=long_desc)).findings
             if x.rule_id == "CQ_PROJECT_NO_OUTCOME")  # fmt: skip
    assert f.finding_class.value == "recommendation"
    assert "Do not add figures" in f.suggested_correction
    assert "CQ_PROJECT_NO_OUTCOME" not in rules(resume(tmp_path, "ok.pdf"))  # "used by 200"


def test_generic_tech_words_need_a_named_technology(tmp_path):
    vague = [("Club Notice Board", "entry"),
             ("Built a notice board for the college club using our knowledge and", "body"),
             ("the right tools to share event updates with members.", "body")]  # fmt: skip
    assert "CQ_PROJECT_NO_TECH" in rules(resume(tmp_path, "v.pdf", projects=vague))
    named = [vague[0], ("Built a notice board for the college club using Flask and", "body"),
             ("SQLite to share event updates with members.", "body")]  # fmt: skip
    assert "CQ_PROJECT_NO_TECH" not in rules(resume(tmp_path, "n.pdf", projects=named))


# --------------------------------------------------------------------------- objective


def test_objective_without_target_role(tmp_path):
    vague = [("CAREER OBJECTIVE", "heading"),
             ("To work in a reputed organization where I can utilize my skills and grow along "
              "with the organization.", "body")]  # fmt: skip
    f = next(x for x in findings(resume(tmp_path, "v.pdf", summary=vague)).findings
             if x.rule_id == "CQ_OBJECTIVE_NO_TARGET")  # fmt: skip
    assert f.section == "summary" and f.bounding_box is not None
    assert "CQ_OBJECTIVE_NO_TARGET" not in rules(resume(tmp_path, "ok.pdf"))


# --------------------------------------------------------------------------- scoring


def test_new_rules_carry_no_ca1_points(tmp_path):
    path = resume(tmp_path, "r.pdf", contact=[("riya.synthetic@gmail | Mobile: 98765 432 | "
                                                "LinkedIn", "body")])  # fmt: skip
    _, scoring = score_file(path, load_settings())
    new = [c for c in scoring.contributions if c.rule_id in NEW_RULES]
    assert new and all(c.applied_points == 0 and c.status is ContributionStatus.ZERO for c in new)


# --------------------------------------------------------------------------- evaluation


def _doc(*anns: ReferenceAnnotation) -> ReferenceDocument:
    return ReferenceDocument("X", "x.pdf", "calibration", Path("x.pdf"), annotations=list(anns))


def _ann(label, topics=(), section=None):  # type: ignore[no-untyped-def]
    return ReferenceAnnotation("X", label, "medium", None, 1, intent="correction_required",
                               section=section, topics=frozenset(topics))  # fmt: skip


def _det(label, rule, section=None):  # type: ignore[no-untyped-def]
    return Detection(label, rule, None, 1, "medium", False, section)


def outcomes(doc, dets):  # type: ignore[no-untyped-def]
    return [
        (r["outcome"], r.get("note"))
        for r in match_document(doc, dets, 0.5)
        if r["outcome"] in ("tp", "fn")
    ]


def test_formatting_needs_topic_agreement():
    fonts = _ann("formatting", topics={"fonts"})
    assert outcomes(_doc(fonts), [_det("formatting", "FMT_LINE_SPACING")]) == [
        ("fn", "same-label finding not credited: different topic")
    ]
    assert outcomes(_doc(fonts), [_det("formatting", "FMT_MIXED_BODY_FONTS")]) == [("tp", None)]
    unknown = _ann("formatting")
    assert outcomes(_doc(unknown), [_det("formatting", "FMT_MIXED_BODY_FONTS")]) == [
        ("fn", "same-label finding not credited: no topic information")
    ]


def test_unrelated_section_is_not_credited_but_natural_section_is():
    spacing = _ann("formatting", topics={"spacing"}, section="projects")
    assert outcomes(_doc(spacing), [_det("formatting", "FMT_LINE_SPACING", "skills")]) == [
        ("fn", "same-label finding not credited: different section")
    ]
    edu = _ann("education_details", section="skills")
    assert outcomes(_doc(edu), [_det("education_details", "CQ_EDUCATION_INCOMPLETE",
                                     "education")]) == [("tp", None)]  # fmt: skip
