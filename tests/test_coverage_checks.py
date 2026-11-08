"""Coverage checks added after the scoring study (synthetic PDFs only).

Every new rule is reported with evidence and is worth 0 CA1 marks; the optional contextual
pathway is advisory only by default.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pps_ats.analysis.orchestrator import analyze_file
from pps_ats.config import load_settings
from pps_ats.devtools import synthetic
from pps_ats.schemas.scoring import ContributionStatus
from pps_ats.scoring.ai import AIEvaluationResponse, CriterionProposal
from pps_ats.scoring.engine import score_analysis, score_file

NEW_RULES = {
    "CQ_OBJECTIVE_GENERIC",
    "CQ_OBJECTIVE_NO_ROLE",
    "CQ_PROJECT_UNSUPPORTED_CLAIM",
    "CQ_PROJECTS_THIN",
    "FMT_MISALIGNED_LINE",
}
CONTACT = "asha.synthetic@example.com | +91 90000 55555 | linkedin.com/in/asha-synthetic"
SPECIFIC = (
    "Seeking a backend developer internship where I can build reliable web services with "
    "Python and PostgreSQL."
)
GENERIC = (
    "To obtain a challenging position in a reputed organization where I can utilize my skills "
    "and contribute to the growth of the organization."
)
FIELDS_ONLY = (
    "Passionate about data science and machine learning, with a strong interest in cloud "
    "computing and building useful applications."
)
GOOD_PROJECT = (
    "Library Tracker",
    [
        "Built a Flask web app that lets students reserve library books online.",
        "Designed the PostgreSQL schema and wrote the REST API for reservations.",
    ],
)


def _line(page, y, text, size=10, bold=False, x=72.0):  # type: ignore[no-untyped-def]
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")


def resume(
    path: Path,
    objective: str = SPECIFIC,
    projects: list[tuple[str, list[str]]] | None = None,
    education_x: list[float] | None = None,
) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60.0
    _line(page, y, "ASHA SYNTHETIC", 18, True)
    _line(page, y + 22, CONTACT)
    y += 52
    _line(page, y, "CAREER OBJECTIVE", 12.5, True)
    for chunk in _wrap(objective, 95):
        y += 15
        _line(page, y, chunk)
    y += 28
    _line(page, y, "EDUCATION", 12.5, True)
    rows = [
        "B.Tech in Computer Science, Example Institute of Technology, 2022 - 2026",
        "CGPA: 8.4 / 10",
        "Class XII, Example Public School, 2022",
        "Percentage: 91%",
        "Class X, Example Public School, 2020",
    ]
    for i, text in enumerate(rows):
        y += 15
        _line(page, y, text, x=(education_x or [72.0] * len(rows))[i])
    y += 28
    _line(page, y, "SKILLS", 12.5, True)
    y += 15
    _line(page, y, "Python, Java, SQL, Flask, PostgreSQL, Git, Docker")
    y += 28
    _line(page, y, "PROJECTS", 12.5, True)
    for title, lines in projects if projects is not None else [GOOD_PROJECT]:
        y += 17
        _line(page, y, title, 10.5, True)
        for text in lines:
            y += 14
            _line(page, y, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return [*out, line]


def _rules(path: Path) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in analyze_file(path, load_settings()).findings:
        out.setdefault(f.rule_id, []).append(f)
    return out


# --------------------------------------------------------------------------- objective


def test_specific_objective_is_not_flagged(tmp_path):
    rules = _rules(resume(tmp_path / "r.pdf"))
    assert not {"CQ_OBJECTIVE_GENERIC", "CQ_OBJECTIVE_NO_ROLE", "CQ_OBJECTIVE_NO_TARGET"} & set(
        rules
    )


def test_stock_phrase_objective_is_generic_and_has_no_target(tmp_path):
    rules = _rules(resume(tmp_path / "r.pdf", objective=GENERIC))
    generic = rules["CQ_OBJECTIVE_GENERIC"][0]
    assert len(generic.evidence["generic_phrases"]) >= 2
    assert "reputed organization" in generic.evidence["generic_phrases"]
    # "position" alone is no longer taken as a named target
    assert "CQ_OBJECTIVE_NO_TARGET" in rules


def test_fields_without_a_role_is_a_recommendation(tmp_path):
    rules = _rules(resume(tmp_path / "r.pdf", objective=FIELDS_ONLY))
    no_role = rules["CQ_OBJECTIVE_NO_ROLE"][0]
    assert no_role.finding_class.value == "recommendation"
    assert "data" in no_role.evidence["fields_named"]
    assert "CQ_OBJECTIVE_NO_TARGET" not in rules and "CQ_OBJECTIVE_GENERIC" not in rules


# --------------------------------------------------------------------------- projects


def test_thin_project_section_is_reported_once(tmp_path):
    thin = [("Chat App", ["Made a chat app."]), ("Weather App", ["Weather using API."])]
    rules = _rules(resume(tmp_path / "r.pdf", projects=thin))
    assert len(rules["CQ_PROJECTS_THIN"]) == 1
    assert rules["CQ_PROJECTS_THIN"][0].evidence["projects"] == 2


def test_concise_complete_resume_is_not_thin(tmp_path):
    # a short resume is fine when its project is actually described
    rules = _rules(resume(tmp_path / "r.pdf", projects=[GOOD_PROJECT]))
    assert "CQ_PROJECTS_THIN" not in rules


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("The model improved accuracy significantly over the baseline approach we started.", True),
        ("The model improved accuracy from 81% to 92% on the held-out test images we used.", False),
    ],
)
def test_unsupported_claim_needs_a_measure(tmp_path, line, flagged):
    project = (
        "Leaf Classifier",
        ["Trained a CNN in PyTorch to classify crop leaf diseases.", line],
    )
    rules = _rules(resume(tmp_path / "r.pdf", projects=[project]))
    assert ("CQ_PROJECT_UNSUPPORTED_CLAIM" in rules) is flagged


# --------------------------------------------------------------------------- formatting


def test_single_misaligned_line_is_measured(tmp_path):
    rules = _rules(resume(tmp_path / "r.pdf", education_x=[72, 72, 78, 72, 72]))
    f = rules["FMT_MISALIGNED_LINE"][0]
    assert f.section == "education" and f.evidence["offset_pt"] == pytest.approx(6.0, abs=0.6)
    assert f.evidence["lines_at_shared_edge"] >= 3 and f.bounding_box is not None
    assert f.detection_method.value == "geometry"


@pytest.mark.parametrize(
    "xs", [[72, 72, 72, 72, 72], [72, 100, 72, 100, 72], [72, 73.5, 72, 72, 72]]
)
def test_consistent_or_deliberate_indents_are_not_misaligned(tmp_path, xs):
    rules = _rules(resume(tmp_path / "r.pdf", education_x=xs))
    assert "FMT_MISALIGNED_LINE" not in rules


def test_formatting_findings_carry_measured_thresholds(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for i, text in enumerate(["NARROW MARGIN SYNTHETIC", CONTACT, "EDUCATION", "B.Tech, 2026"]):
        page.insert_text((8, 60 + 16 * i), text, fontsize=10, fontname="helv")
    path = tmp_path / "m.pdf"
    doc.save(path)
    doc.close()
    margins = _rules(path)["FMT_NARROW_MARGIN"]
    assert all("threshold_pt" in f.evidence and "margin_pt" in f.evidence for f in margins)
    assert margins[0].evidence["margin_pt"] < margins[0].evidence["threshold_pt"]


# --------------------------------------------------------------------------- marks unchanged


def test_new_rules_never_change_marks(tmp_path):
    settings = load_settings()
    thin = [("Chat App", ["Made a chat app."]), ("Weather App", ["Weather using API."])]
    path = resume(
        tmp_path / "r.pdf", objective=GENERIC, projects=thin, education_x=[72, 72, 78, 72, 72]
    )
    analysis, result = score_file(path, settings)
    fired = {f.rule_id for f in analysis.findings} & NEW_RULES
    assert {"CQ_OBJECTIVE_GENERIC", "CQ_PROJECTS_THIN", "FMT_MISALIGNED_LINE"} <= fired
    for c in result.contributions:
        if c.rule_id in NEW_RULES:
            assert c.applied_points == 0 and c.status is ContributionStatus.ZERO
    stripped = analysis.model_copy(
        update={"findings": [f for f in analysis.findings if f.rule_id not in NEW_RULES]}
    )
    without = score_analysis(stripped, settings)
    assert without.ca1.total == result.ca1.total
    assert without.ats.score == result.ats.score and without.status == result.status


# --------------------------------------------------------------------------- contextual pathway


class _Evaluator:
    model_id = "fake-local"

    def __init__(self) -> None:
        self.requests = []

    def evaluate(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return AIEvaluationResponse(
            model_id=self.model_id,
            proposals=[
                CriterionProposal(
                    criterion="content_relevance",
                    adjustment=-1.0,
                    confidence=0.95,
                    rationale="objective is not tailored",
                    evidence=["seeking a software engineering internship"],
                )
            ],
        )


def test_contextual_review_is_advisory_by_default(tmp_path):
    path = synthetic.resume_pdf(tmp_path / "resume.pdf")
    settings = load_settings()
    assert settings.scoring.ai.advisory_only
    _, base = score_file(path, settings)
    settings.scoring.ai = settings.scoring.ai.model_copy(update={"enabled": True})
    evaluator = _Evaluator()
    _, result = score_file(path, settings, evaluator=evaluator)
    assert evaluator.requests  # the evaluator ran ...
    content = result.ca1.criteria[0]
    assert content.ai is not None and not content.ai.accepted  # ... but nothing was applied
    assert "advisory" in (content.ai.reason or "")
    assert content.ai.confidence == 0.95 and content.ai.evidence
    assert result.ca1.total == base.ca1.total
